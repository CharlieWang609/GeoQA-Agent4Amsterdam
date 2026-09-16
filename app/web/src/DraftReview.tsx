// SPDX-License-Identifier: GPL-3.0-only

import { useState, type ReactNode } from "react";

import { readableLabel, recordArray, stringArray } from "./jsonRecords";
import { retrievedData } from "./retrievedData";
import { planningSourceLabel, validationLabel } from "./sessionLabels";
import type { JsonValue, SessionDraftVersion } from "./types";
import { WorkflowModal } from "./WorkflowModal";

export function DraftReview({ draft, versionControl, actions }: {
  draft: SessionDraftVersion;
  versionControl: ReactNode;
  actions: ReactNode;
}) {
  const [workflowOpen, setWorkflowOpen] = useState(false);

  return (
    <article className="plan-card" aria-label="Analysis Plan">
      <details className="plan-details">
        <summary className="card-heading">
          <span>
            <small className="eyebrow">
              Draft {draft.version}{draft.planning_source ? ` · ${planningSourceLabel(draft.planning_source)}` : ""}
            </small>
            <strong className="card-title">Analysis Plan</strong>
          </span>
          <span className="plan-summary-end">
            <span className={`status-badge status-${draft.validation.status}`}>{validationLabel(draft.validation.status)}</span>
            <span className="details-chevron" aria-hidden="true">⌄</span>
          </span>
        </summary>
        <div className="plan-body">
          {versionControl}
          <ReviewSection heading="Question phrases">
            <div className="phrase-list">
              {draft.question_phrases.map((phrase, index) => (
                <span className="phrase-chip" key={`${String(phrase.text ?? "phrase")}-${index}`}>
                  {String(phrase.text ?? "Unlabelled phrase")}
                  <small>{readableLabel(String(phrase.role ?? phrase.functional_role ?? "phrase"))}</small>
                </span>
              ))}
            </div>
          </ReviewSection>
          <ReviewSection heading="Task specification"><DefinitionList value={draft.task_specification} /></ReviewSection>
          <ReviewSection heading="Retrieved Data">
            <div className="binding-list">
              {retrievedData(draft).map((binding, index) => (
                <dl key={`${binding.dataset}-${binding.layer}-${index}`}>
                  <Definition term="Dataset" value={binding.dataset} />
                  <Definition term="Layer" value={binding.layer} />
                  <Definition term="Geometry" value={binding.geometry} />
                  <Definition term="Attributes" value={binding.attributes.join(", ") || "Not specified"} />
                </dl>
              ))}
            </div>
          </ReviewSection>
          <button type="button" className="workflow-entry" onClick={() => setWorkflowOpen(true)}>
            <span>Workflow</span>
            <small>View declared data flow</small>
          </button>
          <ReviewSection heading="Assumptions">
            {draft.assumptions.length ? <ul>{draft.assumptions.map((item) => <li key={item}>{item}</li>)}</ul> : <p>No assumptions.</p>}
            {draft.unresolved_items.length > 0 && <><h4>Unresolved items</h4><ul>{draft.unresolved_items.map((item) => <li key={item}>{item}</li>)}</ul></>}
          </ReviewSection>
          <section>
            <h3>Validation diagnostics</h3>
            {draft.validation.diagnostics.length ? (
              <ul className="diagnostic-list">
                {draft.validation.diagnostics.map((diagnostic, index) => (
                  <li key={`${String(diagnostic.code ?? "diagnostic")}-${index}`}>
                    <strong>{String(diagnostic.code ?? "Diagnostic")}</strong>
                    {`: ${String(diagnostic.message ?? "No message supplied.")}`}
                  </li>
                ))}
              </ul>
            ) : <p>No validation diagnostics.</p>}
            {draft.unsupported_result && (
              <div className="unsupported-result">
                <h4>Why matching stopped</h4>
                {recordArray(draft.unsupported_result.failed_roles).map((failedRole, index) => (
                  <div key={`${String(failedRole.role ?? "role")}-${index}`}>
                    <strong>{readableLabel(String(failedRole.role ?? "Data role"))}</strong>
                    <ul>
                      {recordArray(failedRole.closest_candidates).map((candidate, candidateIndex) => (
                        <li key={candidateIndex}>
                          {stringArray(candidate.rejection_reasons).join(" ") || "No compatible data was found."}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            )}
          </section>
          {actions}
        </div>
      </details>
      {workflowOpen && <WorkflowModal draft={draft} onClose={() => setWorkflowOpen(false)} />}
    </article>
  );
}

function ReviewSection({ heading, children }: { heading: string; children: ReactNode }) {
  return (
    <details className="review-section">
      <summary><span>{heading}</span><span className="details-chevron" aria-hidden="true">⌄</span></summary>
      <div>{children}</div>
    </details>
  );
}

function DefinitionList({ value }: { value: Record<string, JsonValue> }) {
  return <dl className="readable-definition">{Object.entries(value).map(([key, item]) => <Definition key={key} term={readableLabel(key)} value={formatValue(item)} />)}</dl>;
}

function Definition({ term, value }: { term: string; value: string }) {
  return <><dt>{term}</dt><dd>{value}</dd></>;
}

function formatValue(value: JsonValue | undefined): string {
  if (value === undefined || value === null) return "Not specified";
  if (Array.isArray(value)) return value.map((item) => formatValue(item)).join(", ");
  if (typeof value === "object") return Object.entries(value).map(([key, item]) => `${readableLabel(key)}: ${formatValue(item)}`).join(" · ");
  return String(value);
}

