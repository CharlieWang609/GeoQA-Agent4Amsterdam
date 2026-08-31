// SPDX-License-Identifier: GPL-3.0-only

import type { ExecutionJob, QuestionSession } from "./types";

export function hasActiveJob(
  session: QuestionSession,
  job: ExecutionJob | null,
) {
  const status = job?.status ?? session.job_reference?.status;
  return status === "queued" || status === "running";
}
