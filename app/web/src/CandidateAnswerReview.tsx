// SPDX-License-Identifier: GPL-3.0-only

import { useState } from "react";

import { Modal } from "./Modal";
import type { AnswerMapFeatureCollection, QuestionSession } from "./types";

export function CandidateAnswerReview({
  session,
  readOnly = false,
  answerMap,
  feedback,
  mutating,
  onFeedbackChange,
  onDecision,
}: {
  session: QuestionSession;
  // A worked example: the decision is shown, the review controls are not.
  readOnly?: boolean;
  answerMap: AnswerMapFeatureCollection | null;
  feedback: string;
  mutating: boolean;
  onFeedbackChange: (value: string) => void;
  onDecision: (decision: "accepted" | "rejected") => void;
}) {
  const [tableOpen, setTableOpen] = useState(false);
  const answer = session.candidate_answer;
  const failure = session.candidate_answer_failure;
  const decision = session.result_decision;

  if (failure) {
    return (
      <article className="lifecycle-card result-card" aria-labelledby="results-heading">
        <div className="card-heading">
          <h2 id="results-heading">Results</h2>
          <span className="status-badge status-fail">Unavailable</span>
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
    return null;
  }
  const keyColumns = answer.answer_map.key_columns.map((column) => column.column);
  const names = featureNames(answerMap);
  const geometryIndexes = answer.answer_map.key_columns.flatMap((column, index) =>
    column.role === answer.answer_map.geometry_role ? [index] : [],
  );
  const rowName = (keys: string[]) => names.get(geometryIndexes.map((index) => keys[index]).join(" / ")) ?? "";

  return (
    <article className="lifecycle-card result-card" aria-labelledby="results-heading">
      <div className="card-heading">
        <h2 id="results-heading">Results</h2>
        <span className="status-badge status-pass">Candidate Answer</span>
      </div>
      <p className="answer-summary">{answer.summary}</p>
      <p className="source-limitation">
        Values are computed over the records registered in the selected source snapshot
        (planar measurements in {answer.answer_map.crs}); they do not establish complete
        real-world provision.
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
                <table>
                  <thead>
                    <tr>
                      {names.size > 0 && <th>name</th>}
                      {keyColumns.map((column) => <th key={column}>{column}</th>)}
                      <th>{answer.answer_map.value_field}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {answer.result_table.map((row) => (
                      <tr key={row.keys.join("/")}>
                        {names.size > 0 && <td>{rowName(row.keys)}</td>}
                        {row.keys.map((value, index) => <td key={index}>{value}</td>)}
                        <td>{row.value}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
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
            {!readOnly && (
              <>
                <p>Start a New Question to continue.</p>
                <div className="decision-actions">
                  <button type="button" disabled>Approve</button>
                  <button type="button" className="secondary-action" disabled>Reject</button>
                </div>
              </>
            )}
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
}

// The Answer Map names each geometry-role feature when its layer has a name
// field; the table shows that name beside the row's identity keys.
function featureNames(answerMap: AnswerMapFeatureCollection | null) {
  const names = new Map<string, string>();
  for (const feature of answerMap?.features ?? []) {
    if (feature.properties.name) {
      names.set(Object.values(feature.properties.identity).map(String).join(" / "), feature.properties.name);
    }
  }
  return names;
}
