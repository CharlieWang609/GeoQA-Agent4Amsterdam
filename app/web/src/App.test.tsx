// SPDX-License-Identifier: GPL-3.0-only

// Live Sandbox shell tests: session lifecycle, auth handling, review flows.

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  createSession,
  decideResult,
  deleteSession,
  getAnswerMap,
  getExecutionJob,
  getCurrentIdentity,
  getSession,
  listSessions,
  regenerateSession,
} from "./api";
import { App } from "./App";
import {
  answerMap,
  candidateSession,
  failedDraft,
  passDraft,
  sanityCheckFailureSession,
  sessionWith,
} from "./test/fixtures";
import type {
  QuestionSession,
  QuestionSessionSummary,
  SessionDraftVersion,
} from "./types";

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
    createSession: vi.fn(),
    decideResult: vi.fn(),
    deleteSession: vi.fn(),
    editSession: vi.fn(),
    getAnswerMap: vi.fn(),
    getExecutionJob: vi.fn(),
    getCurrentIdentity: vi.fn(),
    getSession: vi.fn(),
    listSessions: vi.fn(),
    regenerateSession: vi.fn(),
  };
});

describe("Live Sandbox", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listSessions).mockResolvedValue([]);
    vi.mocked(getCurrentIdentity).mockResolvedValue({
      principal_id: "github-principal-123",
      display_name: "octocat",
    });
    vi.mocked(getAnswerMap).mockResolvedValue(answerMap());
    vi.mocked(getExecutionJob).mockReturnValue(new Promise(() => undefined));
  });

  it("opens as a two-pane governed workspace with the contractual controls", () => {
    render(<App />);

    expect(screen.getByRole("heading", { name: "GeoQA Agent for Amsterdam" })).toBeVisible();
    expect(screen.queryByText("Live Sandbox")).not.toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: /session workspace/i })).toBeVisible();
    expect(screen.getByRole("region", { name: /interactive map/i })).toBeVisible();
    expect(screen.getByRole("tab", { name: /current session/i })).toBeVisible();
    expect(screen.getByRole("tab", { name: /history/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /new question/i })).toBeVisible();
    expect(screen.getByText("Sessions expire after 7 days.")).toBeVisible();
    expect(screen.getByRole("textbox", { name: /geo-analytical question/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /plan question/i })).toBeDisabled();
    expect(screen.queryByLabelText(/model/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^reject$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /dismiss/i })).not.toBeInTheDocument();
  });

  it("fills and focuses the question from an example without submitting it", async () => {
    const user = userEvent.setup();
    render(<App />);
    const questionInput = screen.getByRole("textbox", {
      name: /geo-analytical question/i,
    });
    const exampleQuestion =
      "For each registered public sports location in Amsterdam, how far away is the nearest swimming pool?";

    await user.type(questionInput, "Replace this draft");
    await user.click(screen.getByRole("button", { name: exampleQuestion }));

    expect(questionInput).toHaveValue(exampleQuestion);
    expect(questionInput).toHaveFocus();
    expect(createSession).not.toHaveBeenCalled();
  });

  it("expands truncated example questions on hover or focus and collapses afterward", () => {
    render(<App />);
    const exampleQuestion = screen.getByRole("button", {
      name: "For each gymnasium in Amsterdam, how far is the nearest swimming pool?",
    });

    expect(exampleQuestion).toHaveAttribute("aria-expanded", "false");

    fireEvent.mouseEnter(exampleQuestion);
    expect(exampleQuestion).toHaveAttribute("aria-expanded", "true");
    fireEvent.mouseLeave(exampleQuestion);
    expect(exampleQuestion).toHaveAttribute("aria-expanded", "false");

    fireEvent.focus(exampleQuestion);
    expect(exampleQuestion).toHaveAttribute("aria-expanded", "true");
    fireEvent.blur(exampleQuestion);
    expect(exampleQuestion).toHaveAttribute("aria-expanded", "false");
  });

  it("shows the signed-in GitHub identity and sign-out action", async () => {
    render(<App />);

    const header = screen.getByRole("banner");
    expect(await within(header).findByText("octocat")).toBeVisible();
    expect(within(header).getByRole("link", { name: /sign out/i })).toHaveAttribute(
      "href",
      "/.auth/logout?post_logout_redirect_uri=/",
    );
    expect(within(header).queryByRole("link", { name: /sign in with github/i })).not.toBeInTheDocument();
  });

  it("lists owned history, restores a selection, and New Question clears the URL", async () => {
    const item = sessionSummary();
    vi.mocked(listSessions).mockResolvedValue([item]);
    vi.mocked(getSession).mockResolvedValue({ session: sessionWith(), etag: "revision-1" });
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("tab", { name: /history/i }));
    const sessionButton = await screen.findByRole("button", { name: /^which amsterdam neighborhoods/i });
    expect(sessionButton).toHaveTextContent(/aug 25, 2026/i);
    expect(sessionButton).toHaveTextContent(/passed/i);
    await user.click(sessionButton);

    expect(await screen.findByRole("region", { name: "Question" })).toHaveTextContent(sessionWith().question);
    expect(window.location.search).toBe("?session=session-123");
    await user.click(screen.getByRole("button", { name: /new question/i }));
    expect(screen.getByRole("textbox", { name: /geo-analytical question/i })).toBeVisible();
    expect(window.location.search).toBe("");
  });

  it("requires confirmation to delete history and refreshes after success", async () => {
    const item = sessionSummary();
    vi.mocked(listSessions)
      .mockResolvedValueOnce([item])
      .mockResolvedValueOnce([]);
    vi.mocked(getSession).mockResolvedValue({
      session: sessionWith(),
      etag: "revision-1",
    });
    vi.mocked(deleteSession).mockResolvedValue();
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("tab", { name: /history/i }));
    const deleteButton = await screen.findByRole("button", {
      name: /delete which amsterdam neighborhoods/i,
    });
    await user.click(deleteButton);

    const dialog = screen.getByRole("dialog", { name: /delete question session/i });
    expect(within(dialog).getByText(item.question)).toBeVisible();
    expect(deleteSession).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: /cancel/i }));
    expect(deleteSession).not.toHaveBeenCalled();

    await user.click(deleteButton);
    await user.click(within(screen.getByRole("dialog", {
      name: /delete question session/i,
    })).getByRole("button", { name: /^delete session$/i }));

    expect(deleteSession).toHaveBeenCalledWith("session-123", "revision-1");
    expect(listSessions).toHaveBeenCalledTimes(2);
    expect(await screen.findByText("No saved sessions.")).toBeVisible();
  });

  it("deleting the open session returns to the question form and clears the URL", async () => {
    window.history.replaceState({}, "", "/sandbox?session=session-123");
    vi.mocked(getSession).mockResolvedValue({
      session: sessionWith(),
      etag: "revision-1",
    });
    vi.mocked(deleteSession).mockResolvedValue();
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /^delete session$/i }));
    expect(deleteSession).not.toHaveBeenCalled();
    await user.click(within(screen.getByRole("dialog", {
      name: /delete question session/i,
    })).getByRole("button", { name: /^delete session$/i }));

    expect(deleteSession).toHaveBeenCalledWith("session-123", "revision-1");
    expect(await screen.findByRole("textbox", {
      name: /geo-analytical question/i,
    })).toBeVisible();
    expect(window.location.search).toBe("");
  });

  it("explains that an active execution must finish before deletion", async () => {
    window.history.replaceState({}, "", "/sandbox?session=session-123");
    vi.mocked(getSession).mockResolvedValue({
      session: sessionWith(),
      etag: "revision-1",
    });
    vi.mocked(deleteSession).mockRejectedValue(new ApiError(
      409,
      "Wait for the execution to finish before deleting this session.",
    ));
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /^delete session$/i }));
    await user.click(within(screen.getByRole("dialog", {
      name: /delete question session/i,
    })).getByRole("button", { name: /^delete session$/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /wait for the execution to finish before deleting this session/i,
    );
    expect(screen.getByRole("region", { name: "Question" })).toBeVisible();
  });

  it("keeps the Analysis Plan and all review subsections collapsed with minimal Retrieved Data", async () => {
    window.history.replaceState({}, "", "/sandbox?session=session-123");
    vi.mocked(getSession).mockResolvedValue({ session: sessionWith(), etag: "revision-1" });
    const user = userEvent.setup();
    render(<App />);

    const planSummary = await screen.findByText("Analysis Plan");
    const planDetails = planSummary.closest("details")!;
    expect(planDetails).not.toHaveAttribute("open");
    expect(screen.getByText("Passed")).toBeVisible();
    expect(screen.getByText("Amsterdam neighborhoods")).not.toBeVisible();

    await user.click(planSummary);
    expect(planDetails.querySelector(".details-chevron")).toBeInTheDocument();
    const subsectionNames = [
      "Question phrases", "Task specification", "Retrieved Data", "Assumptions",
    ];
    subsectionNames.forEach((name) => {
      expect(screen.getByText(name).closest("details")).not.toHaveAttribute("open");
    });
    expect(within(planDetails).getByLabelText(/draft version/i)).toBeVisible();
    expect(within(planDetails).getByRole("button", { name: "Regenerate" })).toBeEnabled();

    const retrieved = screen.getByText("Retrieved Data").closest("details")!;
    await user.click(screen.getByText("Retrieved Data"));
    expect([...retrieved.querySelectorAll("dt")].map((item) => item.textContent)).toEqual([
      "Dataset", "Layer", "CCD", "Attributes",
      "Dataset", "Layer", "CCD", "Attributes",
    ]);
    expect(within(retrieved).getByText("gebieden")).toBeVisible();
    expect(within(retrieved).getByText("buurten")).toBeVisible();
    expect(within(retrieved).getAllByText("ObjectDS")).toHaveLength(2);
    expect(within(retrieved).getByText("begin_geldigheid, eind_geldigheid, identificatie, sports_count, volgnummer")).toBeVisible();
    expect(within(retrieved).getByText("id")).toBeVisible();
    expect(within(retrieved).queryByText(/supports|content hash|score|sha256/i)).not.toBeInTheDocument();

    expect(within(planDetails).queryByText("Abstract workflow")).not.toBeInTheDocument();
    expect(within(planDetails).queryByText("Concrete workflow")).not.toBeInTheDocument();
    const workflowTrigger = screen.getByRole("button", { name: /workflow/i });
    await user.click(workflowTrigger);
    const workflowDialog = screen.getByRole("dialog", { name: "Workflow" });
    expect(within(workflowDialog).getByRole("img", { name: /workflow data flow/i })).toBeVisible();
    expect(within(workflowDialog).queryByRole("heading", { name: "Abstract workflow" })).not.toBeInTheDocument();
    expect(within(workflowDialog).queryByRole("heading", { name: "Concrete workflow" })).not.toBeInTheDocument();
    expect(within(workflowDialog).getByText("Count sports within supports")).toBeVisible();
    expect(within(workflowDialog).getByText("Countpointsinpolygon")).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Workflow" })).not.toBeInTheDocument();
    expect(workflowTrigger).toHaveFocus();
    await user.click(workflowTrigger);
    await user.click(screen.getByRole("button", { name: "Close Workflow" }));
    expect(screen.queryByRole("dialog", { name: "Workflow" })).not.toBeInTheDocument();
    await user.click(workflowTrigger);
    fireEvent.mouseDown(screen.getByRole("dialog", { name: "Workflow" }).parentElement!);
    expect(screen.queryByRole("dialog", { name: "Workflow" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Technical details/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Material diagnostics/i)).not.toBeInTheDocument();
    expect(screen.queryByText("session-123")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revise" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Regenerate" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /approve & execute/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^reject$/i })).not.toBeInTheDocument();
  });

  it("keeps plan mutations gated while a result awaits review and after either decision", async () => {
    window.history.replaceState({}, "", "/sandbox?session=session-123");
    const awaitingDecision = candidateSession();
    vi.mocked(getSession).mockResolvedValue({ session: awaitingDecision, etag: "revision-3" });
    const user = userEvent.setup();
    const { unmount } = render(<App />);

    expect(await screen.findByRole("button", { name: "Approve" })).toBeEnabled();
    await user.click(screen.getByText("Analysis Plan"));
    expect(screen.getByRole("button", { name: "Regenerate" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Revise" })).toBeDisabled();
    unmount();

    const accepted = candidateSession();
    accepted.result_decision = {
      decision: "accepted",
      candidate_answer_id: accepted.candidate_answer!.candidate_answer_id,
      actor_principal_id: "github-principal-123",
      decided_at: "2026-08-25T10:35:00Z",
      feedback: null,
      workflow_id: "workflow-123",
      answer_artifact_ref: "answers/answer.json",
      workflow_record_ref: "workflows/workflow.json",
    };
    vi.mocked(getSession).mockResolvedValue({ session: accepted, etag: "revision-4" });
    render(<App />);

    expect(await screen.findByText(/result accepted for this question session/i)).toBeVisible();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    await user.click(screen.getByText("Analysis Plan"));
    expect(screen.getByRole("button", { name: "Regenerate" })).toBeDisabled();
  });

  it("shows blocking diagnostics for a failing draft", async () => {
    const currentFail = failedDraftVersion(2);
    const session = sessionWith([passDraft(1), currentFail]);
    window.history.replaceState({}, "", "/sandbox?session=session-123");
    vi.mocked(getSession).mockResolvedValue({ session, etag: "revision-2" });
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByText("Analysis Plan"));
    const diagnostics = screen.getByRole("heading", { name: /validation diagnostics/i }).closest("section");
    expect(within(diagnostics!).getByText(/^tool-not-registered$/i)).toBeVisible();
    expect(within(diagnostics!).getByText(/requested phenomenon is unavailable/i)).toBeVisible();
    expect(within(diagnostics!).queryByText("sport/openbaresportplek")).not.toBeInTheDocument();
  });

  it("surfaces semantic warnings on an auto-executed draft", async () => {
    const draft = advisoryDraft();
    const warned = authorizedWarningSession(sessionWith([draft]));
    window.history.replaceState({}, "", "/sandbox?session=session-123");
    vi.mocked(getSession).mockResolvedValue({ session: warned, etag: "revision-2" });
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByText("Analysis Plan"));
    const warningHeading = await screen.findByText(/semantic verification warnings/i);
    expect(warningHeading).toBeVisible();
    expect(within(warningHeading.closest("div")!).getByText(/cct-inference-failed/i)).toBeVisible();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve & execute/i })).not.toBeInTheDocument();
    expect(await screen.findByText(/executed with semantic warnings/i)).toBeVisible();
  });

  it("keeps source limitation visible and makes the exact result table reachable", async () => {
    window.history.replaceState({}, "", "/sandbox?session=session-123");
    vi.mocked(getSession).mockResolvedValue({ session: candidateSession(), etag: "revision-3" });
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Results" })).toBeVisible();
    expect(screen.getByText(/selected source snapshot/i)).toBeVisible();
    expect(screen.queryByRole("region", { name: /exact result table/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Table" }));
    expect(screen.getByRole("region", { name: /exact result table/i })).toBeVisible();
    expect(screen.getByRole("cell", { name: "B" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /dismiss/i })).not.toBeInTheDocument();
    expect(getAnswerMap).toHaveBeenCalledWith("session-123");
  });

  it.each(["accepted", "rejected"] as const)(
    "records a terminal %s decision without regeneration",
    async (decision) => {
      window.history.replaceState({}, "", "/sandbox?session=session-123");
      const current = candidateSession();
      const decided = candidateSession();
      decided.result_decision = {
        decision,
        candidate_answer_id: decided.candidate_answer!.candidate_answer_id,
        actor_principal_id: "github-principal-123",
        decided_at: "2026-08-27T10:35:00Z",
        feedback: null,
        workflow_id: "workflow-123",
        answer_artifact_ref: decision === "accepted" ? "answers/answer.json" : null,
        workflow_record_ref: "workflows/workflow.json",
      };
      vi.mocked(getSession).mockResolvedValue({ session: current, etag: "revision-3" });
      vi.mocked(decideResult).mockResolvedValue({ session: decided, etag: "revision-4" });
      const user = userEvent.setup();
      render(<App />);

      await user.click(await screen.findByRole("button", {
        name: decision === "accepted" ? "Approve" : "Reject",
      }));

      expect(decideResult).toHaveBeenCalledWith("session-123", "revision-3", decision, null);
      expect(await screen.findByText(`Result ${decision} for this Question Session.`)).toBeVisible();
      expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
      expect(regenerateSession).not.toHaveBeenCalled();
    },
  );

  it("keeps candidate-construction failures visible", async () => {
    window.history.replaceState({}, "", "/sandbox?session=session-123");
    vi.mocked(getSession).mockResolvedValue({ session: sanityCheckFailureSession(), etag: "revision-3" });
    render(<App />);
    expect(await screen.findByText(/crs-mismatch/i)).toBeVisible();
  });

  it("keeps anonymous Catalog inspection available and offers sign-in", async () => {
    vi.mocked(getCurrentIdentity).mockRejectedValueOnce(new ApiError(401, "GitHub authentication is required."));
    vi.mocked(listSessions).mockRejectedValueOnce(new ApiError(401, "GitHub authentication is required."));
    const { unmount } = render(<App />);
    const signIn = await within(screen.getByRole("banner")).findByRole("link", {
      name: /sign in with github/i,
    });
    expect(signIn).toBeVisible();
    expect(within(screen.getByRole("banner")).queryByRole("link", { name: /sign out/i })).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: /interactive map/i })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/upload|external.*url/i)).not.toBeInTheDocument();
    unmount();
  });

  it("reports expiry without stale protected content", async () => {
    window.history.replaceState({}, "", "/sandbox?session=expired");
    vi.mocked(listSessions).mockResolvedValue([]);
    vi.mocked(getSession).mockRejectedValue(new ApiError(410, "Expired"));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/question session has expired/i);
    expect(screen.queryByRole("heading", { name: "Results" })).not.toBeInTheDocument();
  });

  it("clears authenticated content when an owned answer request returns 401", async () => {
    window.history.replaceState({}, "", "/sandbox?session=session-123");
    vi.mocked(getSession).mockResolvedValue({ session: candidateSession(), etag: "revision-3" });
    vi.mocked(getAnswerMap).mockRejectedValueOnce(new ApiError(401, "GitHub authentication is required."));
    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/github authentication is required/i);
    expect(within(screen.getByRole("banner")).getByRole("link", { name: /sign in with github/i })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Results" })).not.toBeInTheDocument();
    expect(window.location.search).toBe("");
  });

  it("ignores session and history responses started before anonymous state", async () => {
    window.history.replaceState({}, "", "/sandbox?session=session-123");
    let resolveSession!: (value: Awaited<ReturnType<typeof getSession>>) => void;
    let resolveHistory!: (value: QuestionSessionSummary[]) => void;
    vi.mocked(getSession).mockReturnValue(new Promise((resolve) => { resolveSession = resolve; }));
    vi.mocked(listSessions).mockReturnValue(new Promise((resolve) => { resolveHistory = resolve; }));
    vi.mocked(getCurrentIdentity).mockRejectedValueOnce(new ApiError(401, "GitHub authentication is required."));
    const user = userEvent.setup();
    render(<App />);

    expect(await within(screen.getByRole("banner")).findByRole("link", {
      name: /sign in with github/i,
    })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await act(async () => {
      resolveSession({ session: sessionWith(), etag: "revision-1" });
      resolveHistory([sessionSummary()]);
      await Promise.resolve();
    });

    expect(screen.queryByRole("region", { name: "Question" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /history/i }));
    expect(screen.getByText("No saved sessions.")).toBeVisible();
  });
});

function sessionSummary(): QuestionSessionSummary {
  return {
    session_id: "session-123",
    question: sessionWith().question,
    created_at: "2026-08-25T10:30:00Z",
    expires_at: "2026-09-01T10:30:00Z",
    current_draft_version: 1,
    latest_validation_status: "pass",
    has_execution_job: false,
    has_candidate_answer: false,
    has_result_decision: false,
  };
}

function failedDraftVersion(version: number): SessionDraftVersion {
  const draft = failedDraft();
  return {
    ...draft,
    version,
    draft_version_id: `draft-version-${version}`,
    draft_id: `plan-${version}`,
    validation: { ...draft.validation, draft_id: `plan-${version}` },
  };
}

function advisoryDraft(): SessionDraftVersion {
  const draft = passDraft();
  return {
    ...draft,
    validation: {
      ...draft.validation,
      status: "pass_with_warnings",
      diagnostics: [{
        code: "cct-inference-failed",
        severity: "advisory",
        message: "CCD/CCT composition could not be verified.",
      }],
    },
  };
}

function authorizedWarningSession(session: QuestionSession): QuestionSession {
  return {
    ...session,
    version: 2,
    execution_authorization: {
      draft_version: 1,
      draft_version_id: "draft-version-1",
      draft_id: "plan-1",
      validation_id: "validation-1",
      authorized_at: "2026-08-25T10:31:00Z",
      advisory_override: {
        actor_principal_id: "github-principal-123",
        acknowledged_at: "2026-08-25T10:31:00Z",
        diagnostic_codes: ["cct-inference-failed"],
      },
    },
    job_reference: { job_id: "job-123", status: "queued" },
  };
}
