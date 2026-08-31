// SPDX-License-Identifier: GPL-3.0-only

import { memo, useState } from "react";

import { Modal } from "./Modal";
import { hasActiveJob } from "./sessionState";
import type { QuestionSession } from "./types";

export const CandidateAnswerReview = memo(function CandidateAnswerReview({
  session,
  feedback,
  mutating,
  onFeedbackChange,
  onDecision,
}: {
  session: QuestionSession;
  feedback: string;
  mutating: boolean;
  onFeedbackChange: (value: string) => void;
  onDecision: (decision: "accepted" | "rejected") => void;
}) {
  const [tableOpen, setTableOpen] = useState(false);
  const answer = session.candidate_answer;
  const failure = session.candidate_answer_failure;
  const decision = session.result_decision;
  const executedWithWarnings = Boolean(
    session.execution_authorization?.advisory_override ??
      answer?.reproducibility.advisory_override,
  );

  if (failure) {
    return (
      <article className="lifecycle-card result-card" aria-labelledby="results-heading">
        <div className="card-heading">
          <h2 id="results-heading">Results</h2>
          <div className="badge-row">
            {executedWithWarnings && (
              <span className="status-badge status-pass_with_warnings">
                Executed with semantic warnings
              </span>
            )}
            <span className="status-badge status-fail">Unavailable</span>
          </div>
        </div>
        <ul className="diagnostic-list">
          {failure.diagnostics.map((diagnostic) => (
            <li key={`${diagnostic.code}-${diagnostic.ref ?? "result"}`}>
              <strong>{diagnostic.code}</strong>: {diagnostic.message}
              {diagnostic.ref ? ` (${diagnostic.ref})` : ""}
            </li>
          ))}
        </ul>
      </article>
    );
  }

  if (!answer) {
    if (decision?.decision === "rejected") {
      return (
        <article className="lifecycle-card result-card" aria-labelledby="results-heading">
          <div className="card-heading">
            <h2 id="results-heading">Results</h2>
            <span className="status-badge status-fail">Rejected</span>
          </div>
          <p role="status">Result rejected for this Question Session.</p>
          {decision.feedback && <p>{decision.feedback}</p>}
          <p>Start a New Question to continue.</p>
          <div className="decision-actions">
            <button type="button" disabled>Approve</button>
            <button type="button" className="secondary-action" disabled>Reject</button>
          </div>
        </article>
      );
    }
    const executionStatus = session.execution_result?.status;
    if (executionStatus === "succeeded") {
      return <p role="status">Constructing and checking the Candidate Answer…</p>;
    }
    if (hasActiveJob(session, session.execution_result)) {
      return <p role="status">Waiting for the execution to finish.</p>;
    }
    return null;
  }
  const nearest = "answer_kind" in answer;

  return (
    <article className="lifecycle-card result-card" aria-labelledby="results-heading">
      <div className="card-heading">
        <h2 id="results-heading">Results</h2>
        <div className="badge-row">
          {executedWithWarnings && (
            <span className="status-badge status-pass_with_warnings">
              Executed with semantic warnings
            </span>
          )}
          <span className="status-badge status-pass">Candidate Answer</span>
        </div>
      </div>
      <p className="answer-summary">{answer.summary}</p>
      <p className="source-limitation">
        {nearest
          ? "Distances are directional planar Euclidean measurements in EPSG:28992 metres between point Layers in the selected source snapshot."
          : "This presentation describes Registered Public Sports Locations recorded in the selected source snapshot. It does not establish complete real-world provision, outdoor status, or facility footprints."}
      </p>

      <div className="view-toggle" aria-label="Result view">
        <button type="button" aria-pressed={!tableOpen} onClick={() => setTableOpen(false)}>Map</button>
        <button type="button" aria-pressed={tableOpen} onClick={() => setTableOpen(true)}>Table</button>
      </div>
      <p className="map-pointer">The Candidate Answer overlay is shown on the interactive map.</p>
      {tableOpen && (
        <Modal title="Result table" position="lower-center" onClose={() => setTableOpen(false)}>
          <section className="table-modal" aria-label="Exact result table">
            {answer.result_table.length ? (
              <div className="table-scroll">
                {nearest ? (
                  <table>
                    <thead><tr><th>Source</th><th>Target</th><th>Distance (m)</th></tr></thead>
                    <tbody>
                      {answer.result_table.map((row) => (
                        <tr key={`${row.source_id}-${row.target_id}`}>
                          <td>{row.source_id}</td><td>{row.target_id}</td><td>{row.distance_m}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <table>
                    <thead><tr><th>Identificatie</th><th>Volgnummer</th><th>Count</th></tr></thead>
                    <tbody>
                      {answer.result_table.map((row) => (
                        <tr key={`${row.identificatie}-${row.volgnummer}`}>
                          <td>{row.identificatie}</td><td>{row.volgnummer}</td><td>{row.count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            ) : <p>No result rows were produced.</p>}
          </section>
        </Modal>
      )}

      <section className="decision-panel" aria-labelledby="decision-heading">
        <h3 id="decision-heading">Result review</h3>
        {decision ? (
          <div className="decision-status" role="status">
            <strong>Result {decision.decision} for this Question Session.</strong>
            {decision.feedback && <p>{decision.feedback}</p>}
            <p>Start a New Question to continue.</p>
            <div className="decision-actions">
              <button type="button" disabled>Approve</button>
              <button type="button" className="secondary-action" disabled>Reject</button>
            </div>
          </div>
        ) : (
          <>
            <label htmlFor="result-feedback">Feedback (optional)</label>
            <textarea
              id="result-feedback"
              rows={3}
              value={feedback}
              onChange={(event) => onFeedbackChange(event.target.value)}
            />
            <div className="decision-actions">
              <button type="button" disabled={mutating} onClick={() => onDecision("accepted")}>Approve</button>
              <button type="button" className="secondary-action" disabled={mutating} onClick={() => onDecision("rejected")}>
                Reject
              </button>
            </div>
          </>
        )}
      </section>
    </article>
  );
});
