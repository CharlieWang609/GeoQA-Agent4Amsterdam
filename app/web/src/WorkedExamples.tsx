// SPDX-License-Identifier: GPL-3.0-only

import { formatSessionDate } from "./sessionLabels";
import type { QuestionSessionSummary } from "./types";

// Accepted answers published for reading without an account.
export function WorkedExamples({
  examples,
  onSelect,
}: {
  examples: QuestionSessionSummary[];
  onSelect: (id: string) => void;
}) {
  if (!examples.length) return null;
  return (
    <section className="history-view worked-examples" aria-labelledby="worked-examples-heading">
      <h2 id="worked-examples-heading">Worked examples</h2>
      <p>Questions answered and reviewed in this sandbox: the plan, the map and the result table.</p>
      <ul>{examples.map((item) => (
        <li key={item.session_id}>
          <button className="history-session" type="button" onClick={() => onSelect(item.session_id)}>
            <strong>{item.question}</strong>
            <time dateTime={item.created_at}>{formatSessionDate(item.created_at)}</time>
          </button>
        </li>
      ))}</ul>
    </section>
  );
}
