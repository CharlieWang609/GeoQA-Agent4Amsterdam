// SPDX-License-Identifier: GPL-3.0-only

// API client tests: ETag handling, error normalization, request shapes.

import {
  ApiError,
  createSession,
  decideResult,
  deleteSession,
  editSession,
  getAnswerMap,
  getExecutionJob,
  getCatalogLayerPreview,
  getCatalogLayers,
  getCurrentIdentity,
  getSession,
  listSessions,
  regenerateSession,
} from "./api";
import { describe, expect, it, vi } from "vitest";

describe("Live Sandbox API client", () => {
  it("reads the caller identity without using the Easy Auth token store", async () => {
    const identity = {
      principal_id: "github-principal-123",
      display_name: "octocat",
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(identity), {
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(getCurrentIdentity()).resolves.toEqual(identity);
    expect(fetchMock).toHaveBeenCalledWith("/api/me");
    expect(fetchMock).not.toHaveBeenCalledWith("/.auth/me");
  });

  it("submits only question text and retains the session ETag", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ session_id: "session-123" }), {
        status: 201,
        headers: { ETag: '"revision-1"', "Content-Type": "application/json" },
      }),
    );

    const result = await createSession("Which neighborhoods have no sports locations?");

    expect(fetchMock).toHaveBeenCalledWith("/api/question-sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: "Which neighborhoods have no sports locations?",
      }),
    });
    expect(result.etag).toBe("revision-1");
    expect(result.session).toEqual({ session_id: "session-123" });
  });

  it("edits the inspected session revision with free-text instruction", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ session_id: "session-123" }), {
        headers: { ETag: '"revision-2"', "Content-Type": "application/json" },
      }),
    );

    const result = await editSession(
      "session-123",
      "revision-1",
      "Keep strict within semantics.",
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/question-sessions/session-123/edit",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "If-Match": '"revision-1"',
        },
        body: JSON.stringify({ instruction: "Keep strict within semantics." }),
      },
    );
    expect(result.etag).toBe("revision-2");
  });

  it("restores an owned session and its current ETag", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ session_id: "session-123" }), {
        headers: { ETag: '"revision-4"', "Content-Type": "application/json" },
      }),
    );

    const result = await getSession("session-123");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/question-sessions/session-123",
    );
    expect(result.etag).toBe("revision-4");
  });

  it("regenerates from scratch against the inspected session revision", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ session_id: "session-123" }), {
        headers: { ETag: '"revision-3"', "Content-Type": "application/json" },
      }),
    );

    await regenerateSession("session-123", "revision-2");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/question-sessions/session-123/regenerate",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "If-Match": '"revision-2"',
        },
        body: "{}",
      },
    );
  });

  it("lists owned session history", async () => {
    const sessions = [{ session_id: "session-123", question: "Where?" }];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(sessions), {
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(listSessions()).resolves.toEqual(sessions);
    expect(fetchMock).toHaveBeenCalledWith("/api/question-sessions");
  });

  it("deletes the exact inspected session revision", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(null, { status: 204 }),
    );

    await deleteSession("session-123", "revision-4");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/question-sessions/session-123",
      {
        method: "DELETE",
        headers: { "If-Match": '"revision-4"' },
      },
    );
  });

  it("exposes the structured active-job deletion message", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({
        detail: {
          code: "active-execution-job",
          message: "Wait for the execution to finish before deleting this session.",
          job_id: "job-123",
          status: "running",
        },
      }), {
        status: 409,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(deleteSession("session-123", "revision-4")).rejects.toEqual(
      new ApiError(
        409,
        "Wait for the execution to finish before deleting this session.",
      ),
    );
  });

  it("reads Catalog layers and one preview through their read-only routes", async () => {
    const listing = { catalog_version: "catalog-v1", layers: [] };
    const preview = { type: "FeatureCollection", features: [] };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify(listing), {
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(preview), {
          headers: { "Content-Type": "application/json" },
        }),
      );

    await expect(getCatalogLayers()).resolves.toEqual(listing);
    await expect(
      getCatalogLayerPreview("sport data", "openbare/sportplek"),
    ).resolves.toEqual(preview);
    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/catalog-layers");
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/catalog-layers/sport%20data/openbare%2Fsportplek/preview",
    );
  });

  it("reads the durable execution job independently from session revision", async () => {
    const job = { job_id: "job-123", status: "running" };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(job), {
        headers: { "Content-Type": "application/json" },
      }),
    );

    const result = await getExecutionJob("session-123");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/question-sessions/session-123/execution-job",
    );
    expect(result).toEqual(job);
  });

  it("reads the owned Candidate Answer map", async () => {
    const answerMap = {
      type: "FeatureCollection",
      source_crs: "EPSG:28992",
      features: [],
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(answerMap), {
        headers: { "Content-Type": "application/json" },
      }),
    );

    const result = await getAnswerMap("session-123");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/question-sessions/session-123/answer-map",
    );
    expect(result).toEqual(answerMap);
  });

  it("records Result Acceptance against the inspected session revision", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ session_id: "session-123" }), {
        headers: { ETag: '"revision-4"', "Content-Type": "application/json" },
      }),
    );

    const result = await decideResult(
      "session-123",
      "revision-3",
      "accepted",
      "The map and diagnostics are satisfactory.",
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/question-sessions/session-123/result-decision",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "If-Match": '"revision-3"',
        },
        body: JSON.stringify({
          decision: "accepted",
          feedback: "The map and diagnostics are satisfactory.",
        }),
      },
    );
    expect(result.etag).toBe("revision-4");
  });

  it("exposes API quota detail to the review surface", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: "A Sandbox User may have only one active execution job.",
        }),
        { status: 429, headers: { "Content-Type": "application/json" } },
      ),
    );

    await expect(regenerateSession("session-123", "revision-1")).rejects.toEqual(
      new ApiError(
        429,
        "A Sandbox User may have only one active execution job.",
      ),
    );
  });
});
