// SPDX-License-Identifier: GPL-3.0-only

import type {
  AnswerMapFeatureCollection,
  CatalogLayerListing,
  CurrentIdentity,
  ExecutionJob,
  GeoJsonFeatureCollection,
  QuestionSession,
  QuestionSessionSummary,
} from "./types";

// Thin fetch wrappers around the Live Sandbox API. Session reads return the
// response ETag and every mutation sends it back as If-Match, so a stale
// client fails with 412 instead of clobbering concurrent changes.

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface SessionResponse<T = Record<string, unknown>> {
  session: T;
  etag: string;
}

export async function getCurrentIdentity(): Promise<CurrentIdentity> {
  const response = await fetch("/api/me");
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as CurrentIdentity;
}

export async function createSession(
  question: string,
): Promise<SessionResponse<QuestionSession>> {
  const response = await fetch("/api/question-sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  return readSessionResponse(response);
}

export async function getSession(
  sessionId: string,
): Promise<SessionResponse<QuestionSession>> {
  const response = await fetch(
    `/api/question-sessions/${encodeURIComponent(sessionId)}`,
  );
  return readSessionResponse(response);
}

export async function listSessions(): Promise<QuestionSessionSummary[]> {
  const response = await fetch("/api/question-sessions");
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as QuestionSessionSummary[];
}

export async function deleteSession(
  sessionId: string,
  etag: string,
): Promise<void> {
  const response = await fetch(
    `/api/question-sessions/${encodeURIComponent(sessionId)}`,
    {
      method: "DELETE",
      headers: { "If-Match": `"${etag}"` },
    },
  );
  if (!response.ok) throw await apiError(response);
}

export async function getCatalogLayers(): Promise<CatalogLayerListing> {
  const response = await fetch("/api/catalog-layers");
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as CatalogLayerListing;
}

export async function getCatalogLayerPreview(
  dataset: string,
  featureType: string,
): Promise<GeoJsonFeatureCollection> {
  const response = await fetch(
    `/api/catalog-layers/${encodeURIComponent(dataset)}/${encodeURIComponent(featureType)}/preview`,
  );
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as GeoJsonFeatureCollection;
}

export async function editSession(
  sessionId: string,
  etag: string,
  instruction: string,
): Promise<SessionResponse<QuestionSession>> {
  return mutateSession(sessionId, "edit", etag, { instruction });
}

export async function regenerateSession(
  sessionId: string,
  etag: string,
): Promise<SessionResponse<QuestionSession>> {
  return mutateSession(sessionId, "regenerate", etag, {});
}

export async function getExecutionJob(sessionId: string): Promise<ExecutionJob> {
  const response = await fetch(
    `/api/question-sessions/${encodeURIComponent(sessionId)}/execution-job`,
  );
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as ExecutionJob;
}

export async function getAnswerMap(
  sessionId: string,
): Promise<AnswerMapFeatureCollection> {
  const response = await fetch(
    `/api/question-sessions/${encodeURIComponent(sessionId)}/answer-map`,
  );
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as AnswerMapFeatureCollection;
}

export async function decideResult(
  sessionId: string,
  etag: string,
  decision: "accepted" | "rejected",
  feedback: string | null,
): Promise<SessionResponse<QuestionSession>> {
  const body: Record<string, string> = { decision };
  if (feedback) body.feedback = feedback;
  return mutateSession(sessionId, "result-decision", etag, body);
}

async function mutateSession(
  sessionId: string,
  action: "edit" | "regenerate" | "result-decision",
  etag: string,
  body: Record<string, string | boolean>,
): Promise<SessionResponse<QuestionSession>> {
  const response = await fetch(
    `/api/question-sessions/${encodeURIComponent(sessionId)}/${action}`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "If-Match": `"${etag}"`,
      },
      body: JSON.stringify(body),
    },
  );
  return readSessionResponse(response);
}

async function readSessionResponse(
  response: Response,
): Promise<SessionResponse<QuestionSession>> {
  if (!response.ok) {
    throw await apiError(response);
  }
  const etag = response.headers.get("ETag")?.replace(/^"|"$/g, "");
  if (!etag) {
    throw new Error("GeoQA Agent response did not include a session ETag.");
  }
  return {
    session: (await response.json()) as QuestionSession,
    etag,
  };
}

async function apiError(response: Response): Promise<ApiError> {
  let message = `GeoQA Agent request failed (${response.status}).`;
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") message = body.detail;
    else if (
      typeof body.detail === "object" &&
      body.detail !== null &&
      "message" in body.detail &&
      typeof body.detail.message === "string"
    ) message = body.detail.message;
  } catch {
    // Preserve the status-based message for non-JSON proxy responses.
  }
  return new ApiError(response.status, message);
}
