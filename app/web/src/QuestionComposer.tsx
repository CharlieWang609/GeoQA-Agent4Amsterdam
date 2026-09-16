// SPDX-License-Identifier: GPL-3.0-only

import { FormEvent, useRef, useState } from "react";

import type { ModelChoice } from "./types";

const EXAMPLE_QUESTIONS = [
  "What fraction of each Amsterdam borough is tree cover according to the land cover map?",
  "What is the mean NDVI of each Amsterdam neighborhood in summer 2025 (June to August), from a cloud-masked median Sentinel-2 composite?",
  "For each Amsterdam district, what is the mean Dynamic World tree-cover probability over June to August 2025 (mean composite of the scenes)?",
  "For each Amsterdam neighborhood, how much did the median summer NDVI change from 2024 to 2025: the per-pixel difference (2025 minus 2024) of the two cloud-masked median Sentinel-2 composites for June to August, averaged over the neighborhood?",
  "Which Amsterdam neighborhoods have no registered public sports locations?",
] as const;

export function QuestionComposer({
  question,
  submitting,
  models,
  model,
  onQuestionChange,
  onModelChange,
  onSubmit,
}: {
  question: string;
  submitting: boolean;
  models: ModelChoice[];
  model: string;
  onQuestionChange: (value: string) => void;
  onModelChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  const [hoveredExample, setHoveredExample] = useState<string | null>(null);
  const [focusedExample, setFocusedExample] = useState<string | null>(null);
  const questionInput = useRef<HTMLTextAreaElement>(null);

  function selectExampleQuestion(exampleQuestion: string) {
    onQuestionChange(exampleQuestion);
    questionInput.current?.focus();
  }

  return (
    <section className="question-composer" aria-labelledby="question-heading">
      <h2 id="question-heading">Ask a geo-analytical question</h2>
      <form onSubmit={onSubmit}>
        <label htmlFor="question">Geo-analytical question</label>
        <textarea ref={questionInput} id="question" rows={5} value={question} onChange={(event) => onQuestionChange(event.target.value)} placeholder={EXAMPLE_QUESTIONS[0]} />
        <div className="example-question-list" aria-label="Example questions">
          {EXAMPLE_QUESTIONS.map((exampleQuestion) => {
            const expanded = hoveredExample === exampleQuestion || focusedExample === exampleQuestion;
            return (
              <button
                key={exampleQuestion}
                type="button"
                className="example-question-chip"
                aria-expanded={expanded}
                onClick={() => selectExampleQuestion(exampleQuestion)}
                onMouseEnter={() => setHoveredExample(exampleQuestion)}
                onMouseLeave={() => setHoveredExample(null)}
                onFocus={() => setFocusedExample(exampleQuestion)}
                onBlur={() => setFocusedExample(null)}
              >
                {exampleQuestion}
              </button>
            );
          })}
        </div>
        <div className="composer-actions">
          <label htmlFor="model">Model</label>
          <select id="model" value={model} onChange={(event) => onModelChange(event.target.value)} disabled={submitting}>
            {models.map((choice) => (
              <option key={choice.model} value={choice.model}>{choice.label}</option>
            ))}
          </select>
          <button disabled={submitting || !question.trim()} type="submit">{submitting ? "Planning…" : "Plan question"}</button>
        </div>
      </form>
    </section>
  );
}
