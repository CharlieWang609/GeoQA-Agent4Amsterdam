// SPDX-License-Identifier: GPL-3.0-only

import { formatSessionDate, validationLabel } from "./sessionLabels";
import type { QuestionSessionSummary } from "./types";

export function HistoryList({
  history,
  loading,
  onSelect,
  onDelete,
}: {
  history: QuestionSessionSummary[];
  loading: boolean;
  onSelect: (id: string) => void;
  onDelete: (id: string, question: string) => void;
}) {
  return (
    <section className="history-view" aria-labelledby="history-heading">
      <h2 id="history-heading">Question history</h2>
      {loading ? <p role="status">Loading history…</p> : history.length ? (
        <ul>{history.map((item) => (
          <li key={item.session_id}>
            <button className="history-session" type="button" onClick={() => onSelect(item.session_id)}>
              <strong>{item.question}</strong>
              <time dateTime={item.created_at}>{formatSessionDate(item.created_at)}</time>
              <span className={`status-badge status-${item.latest_validation_status}`}>{historyStatus(item)}</span>
            </button>
            <button
              type="button"
              className="history-delete danger-action"
              aria-label={`Delete ${item.question}`}
              onClick={() => onDelete(item.session_id, item.question)}
            >
              Delete
            </button>
          </li>
        ))}</ul>
      ) : <p>No saved sessions.</p>}
    </section>
  );
}

function historyStatus(item: QuestionSessionSummary) {
  if (item.has_result_decision) return "Reviewed";
  if (item.has_candidate_answer) return "Result ready";
  if (item.has_execution_job) return "Executing";
  return validationLabel(item.latest_validation_status);
}
