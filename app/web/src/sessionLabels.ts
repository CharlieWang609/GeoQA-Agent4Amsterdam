// SPDX-License-Identifier: GPL-3.0-only

import type { PlanningSource, SessionDraftVersion } from "./types";

// How the draft was produced: free LLM composition or the case-base replay.
export function planningSourceLabel(source: PlanningSource): string {
  return { composition: "Composed", retrieval: "Replayed" }[source];
}

export function validationLabel(status: SessionDraftVersion["validation"]["status"]) {
  return status === "pass" ? "Passed" : "Failed";
}

export function formatSessionDate(value: string) {
  return new Intl.DateTimeFormat("en", { dateStyle: "medium", timeZone: "UTC" }).format(new Date(value));
}
