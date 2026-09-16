// SPDX-License-Identifier: GPL-3.0-only

import type {
  ModelListing,
  AnswerMapFeatureCollection,
  CatalogLayerListing,
  CurrentIdentity,
  GeoJsonFeatureCollection,
  ObservationListing,
  QuestionSession,
  QuestionSessionSummary,
  RasterView,
} from "./types";

// Thin fetch wrappers around the Live Sandbox API.

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function getCurrentIdentity(): Promise<CurrentIdentity> {
  const response = await fetch("/api/me");
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as CurrentIdentity;
}

export async function createSession(question: string, model: string): Promise<QuestionSession> {
  const response = await fetch("/api/question-sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, model }),
  });
  return readSessionResponse(response);
}

export async function getModels(): Promise<ModelListing> {
  const response = await fetch("/api/models");
  if (!response.ok) throw new ApiError(response.status, await response.text());
  return (await response.json()) as ModelListing;
}

export async function getSession(sessionId: string): Promise<QuestionSession> {
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

export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(
    `/api/question-sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) throw await apiError(response);
}

// Worked examples: accepted sessions frozen for reading without an account.
// Their documents carry this owner instead of a GitHub principal.
export const SHOWCASE_PRINCIPAL = "showcase";

export function isShowcaseSession(session: QuestionSession): boolean {
  return session.owner_principal_id === SHOWCASE_PRINCIPAL;
}

export async function listShowcase(): Promise<QuestionSessionSummary[]> {
  const response = await fetch("/api/showcase");
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as QuestionSessionSummary[];
}

export async function getShowcaseSession(sessionId: string): Promise<QuestionSession> {
  const response = await fetch(`/api/showcase/${encodeURIComponent(sessionId)}`);
  return readSessionResponse(response);
}

export async function getShowcaseAnswerMap(sessionId: string): Promise<AnswerMapFeatureCollection> {
  const response = await fetch(`/api/showcase/${encodeURIComponent(sessionId)}/answer-map`);
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as AnswerMapFeatureCollection;
}

export async function getShowcaseObservations(sessionId: string): Promise<ObservationListing> {
  const response = await fetch(`/api/showcase/${encodeURIComponent(sessionId)}/observations`);
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as ObservationListing;
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

export function catalogRasterViewUrl(dataset: string, featureType: string): string {
  return `/api/catalog-layers/${encodeURIComponent(dataset)}/${encodeURIComponent(featureType)}/raster-view`;
}

export async function getRasterView(viewUrl: string): Promise<RasterView> {
  const response = await fetch(viewUrl);
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as RasterView;
}

export async function getObservations(sessionId: string): Promise<ObservationListing> {
  const response = await fetch(
    `/api/question-sessions/${encodeURIComponent(sessionId)}/observations`,
  );
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as ObservationListing;
}

export async function editSession(
  sessionId: string,
  instruction: string,
): Promise<QuestionSession> {
  return mutateSession(sessionId, "edit", { instruction });
}

export async function regenerateSession(sessionId: string): Promise<QuestionSession> {
  return mutateSession(sessionId, "regenerate", {});
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
  decision: "accepted" | "rejected",
  feedback: string | null,
): Promise<QuestionSession> {
  const body: Record<string, string> = { decision };
  if (feedback) body.feedback = feedback;
  return mutateSession(sessionId, "result-decision", body);
}

async function mutateSession(
  sessionId: string,
  action: "edit" | "regenerate" | "result-decision",
  body: Record<string, string>,
): Promise<QuestionSession> {
  const response = await fetch(
    `/api/question-sessions/${encodeURIComponent(sessionId)}/${action}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  return readSessionResponse(response);
}

async function readSessionResponse(response: Response): Promise<QuestionSession> {
  if (!response.ok) throw await apiError(response);
  return (await response.json()) as QuestionSession;
}

async function apiError(response: Response): Promise<ApiError> {
  let message = `GeoQA Agent request failed (${response.status}).`;
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") message = body.detail;
  } catch {
    // Preserve the status-based message for non-JSON proxy responses.
  }
  return new ApiError(response.status, message);
}
