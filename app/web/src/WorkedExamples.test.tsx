// SPDX-License-Identifier: GPL-3.0-only

// Worked examples: accepted answers an anonymous visitor can open and read.

import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  SHOWCASE_PRINCIPAL,
  getAnswerMap,
  getCurrentIdentity,
  getModels,
  getShowcaseAnswerMap,
  getShowcaseObservations,
  getShowcaseSession,
  listSessions,
  listShowcase,
} from "./api";
import { App } from "./App";
import { answerMap, candidateSession } from "./test/fixtures";
import type { QuestionSession, QuestionSessionSummary } from "./types";

vi.mock("./MapPane", () => ({
  MapPane: ({ answerMap: map }: { answerMap: ReturnType<typeof answerMap> | null }) => (
    <section aria-label="Interactive map">
      {map && <p>{map.title}</p>}
    </section>
  ),
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getAnswerMap: vi.fn(),
    getCurrentIdentity: vi.fn(),
    getModels: vi.fn(),
    getObservations: vi.fn(),
    getShowcaseAnswerMap: vi.fn(),
    getShowcaseObservations: vi.fn(),
    getShowcaseSession: vi.fn(),
    listSessions: vi.fn(),
    listShowcase: vi.fn(),
  };
});

const unauthorized = () => new ApiError(401, "GitHub authentication is required.");

function workedExample(): QuestionSession {
  const example = candidateSession();
  return {
    ...example,
    owner_principal_id: SHOWCASE_PRINCIPAL,
    result_decision: {
      decision: "accepted",
      candidate_answer_id: example.candidate_answer!.candidate_answer_id,
      actor_principal_id: SHOWCASE_PRINCIPAL,
      decided_at: "2026-08-25T10:35:00Z",
      feedback: null,
      workflow_id: "workflow-1",
      answer_artifact_ref: null,
      workflow_record_ref: null,
    },
  };
}

function exampleSummary(): QuestionSessionSummary {
  const example = workedExample();
  return {
    session_id: example.session_id,
    question: example.question,
    created_at: example.created_at,
    expires_at: example.expires_at,
    current_draft_version: example.current_draft_version,
    latest_validation_status: "pass",
    has_execution_job: true,
    has_candidate_answer: true,
    has_result_decision: true,
  };
}

describe("Worked examples", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState({}, "", "/sandbox");
    vi.mocked(getCurrentIdentity).mockRejectedValue(unauthorized());
    vi.mocked(listSessions).mockRejectedValue(unauthorized());
    vi.mocked(getModels).mockResolvedValue({ default: "gemini-3.8-flash", models: [] });
    vi.mocked(listShowcase).mockResolvedValue([exampleSummary()]);
    vi.mocked(getShowcaseSession).mockResolvedValue(workedExample());
    vi.mocked(getShowcaseAnswerMap).mockResolvedValue(answerMap());
    vi.mocked(getShowcaseObservations).mockResolvedValue({ items: [] });
  });

  it("lets an anonymous visitor open an example and read it without account controls", async () => {
    const user = userEvent.setup();
    render(<App />);

    const examples = await screen.findByRole("region", { name: /worked examples/i });
    expect(await screen.findByText(/asking a question needs a github account/i)).toBeVisible();
    await user.click(within(examples).getByRole("button", { name: /no sports locations/i }));

    expect(await screen.findByRole("heading", { name: "Results" })).toBeVisible();
    expect(screen.getByText(answerMap().title)).toBeVisible();
    expect(screen.getByText(/worked example · model/i)).toBeVisible();
    expect(window.location.search).toBe("?showcase=session-123");
    expect(getShowcaseAnswerMap).toHaveBeenCalledWith("session-123");
    expect(getAnswerMap).not.toHaveBeenCalled();

    expect(screen.queryByRole("button", { name: /delete session/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revise" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Regenerate" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.getByText("Worked examples are read-only.")).toBeVisible();
    expect(within(screen.getByRole("banner")).getByRole("link", { name: /sign in with github/i })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps a deep-linked example open when the account check comes back unauthorized", async () => {
    window.history.replaceState({}, "", "/sandbox?showcase=session-123");
    let rejectIdentity!: (error: Error) => void;
    vi.mocked(getCurrentIdentity).mockReturnValue(new Promise((_, reject) => { rejectIdentity = reject; }));
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Results" })).toBeVisible();
    await act(async () => {
      rejectIdentity(unauthorized());
      await Promise.resolve();
    });

    expect(await within(screen.getByRole("banner")).findByRole("link", { name: /sign in with github/i })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Results" })).toBeVisible();
    expect(screen.getByText(answerMap().title)).toBeVisible();
    expect(window.location.search).toBe("?showcase=session-123");

    await user.click(screen.getByRole("button", { name: /new question/i }));
    expect(window.location.search).toBe("");
    expect(await screen.findByRole("region", { name: /worked examples/i })).toBeVisible();
  });

  it("reports an example that is no longer published", async () => {
    window.history.replaceState({}, "", "/sandbox?showcase=gone");
    vi.mocked(getShowcaseSession).mockRejectedValue(new ApiError(404, "Not found"));
    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/no longer published/i);
  });
});
