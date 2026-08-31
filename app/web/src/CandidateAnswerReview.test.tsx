// SPDX-License-Identifier: GPL-3.0-only

// Candidate Answer review tests: result states, decisions, table modal.

import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CandidateAnswerReview } from "./CandidateAnswerReview";
import {
  authorizedSession,
  candidateSession,
  nearestCandidateSession,
  sanityCheckFailureSession,
} from "./test/fixtures";
import type { QuestionSession } from "./types";

describe("Candidate Answer review", () => {
  it("keeps the exact table behind the Table toggle and exposes only API decision verbs", async () => {
    const user = userEvent.setup();
    renderReview(candidateSession());

    expect(screen.getByRole("button", { name: "Map" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("region", { name: /exact result table/i })).not.toBeInTheDocument();
    expect(screen.getByText(/selected source snapshot/i)).toBeVisible();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /dismiss/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Table" }));
    const tableDialog = screen.getByRole("dialog", { name: "Result table" });
    expect(within(tableDialog).getByRole("region", { name: /exact result table/i })).toBeVisible();
    expect(within(tableDialog).getByRole("cell", { name: "B" })).toBeVisible();
    expect(within(tableDialog).getByRole("cell", { name: "0" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Table" })).toHaveAttribute("aria-pressed", "true");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Result table" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Table" }));
    await user.click(screen.getByRole("button", { name: "Close Result table" }));
    expect(screen.queryByRole("dialog", { name: "Result table" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Table" }));
    fireEvent.mouseDown(screen.getByRole("dialog", { name: "Result table" }).parentElement!);
    expect(screen.queryByRole("dialog", { name: "Result table" })).not.toBeInTheDocument();
  });

  it("removes technical details and material diagnostics from the UI only", () => {
    renderReview(candidateSession());

    expect(screen.queryByText(/technical details/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/material diagnostics/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/catalog-2026-08-24/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/sha256:neighborhoods/i)).not.toBeInTheDocument();
  });

  it.each(["accepted", "rejected"] as const)(
    "shows a terminal %s decision with disabled actions",
    (decision) => {
      const session = candidateSession();
      session.result_decision = {
        decision,
        candidate_answer_id: session.candidate_answer!.candidate_answer_id,
        actor_principal_id: "github-principal-123",
        decided_at: "2026-08-27T10:35:00Z",
        feedback: "Recorded feedback",
        workflow_id: "workflow-123",
        answer_artifact_ref: decision === "accepted" ? "answers/answer.json" : null,
        workflow_record_ref: "workflows/workflow.json",
      };

      renderReview(session);

      expect(screen.getByRole("status")).toHaveTextContent(`Result ${decision}`);
      expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
      expect(screen.getByText(/new question/i)).toBeVisible();
    },
  );

  it("shows the persistent semantic-warning badge from recorded authorization", () => {
    const session = candidateSession();
    session.execution_authorization = {
      ...session.execution_authorization!,
      advisory_override: {
        actor_principal_id: "github-principal-123",
        acknowledged_at: "2026-08-25T10:31:00Z",
        diagnostic_codes: ["cct-inference-failed"],
      },
    };

    renderReview(session);

    expect(screen.getByText(/executed with semantic warnings/i)).toBeVisible();
  });

  it("keeps recorded warning provenance visible when result construction fails", () => {
    const session = sanityCheckFailureSession();
    session.execution_authorization = {
      ...session.execution_authorization!,
      advisory_override: {
        actor_principal_id: "github-principal-123",
        acknowledged_at: "2026-08-25T10:31:00Z",
        diagnostic_codes: ["cct-inference-failed"],
      },
    };

    renderReview(session);

    expect(screen.getByText(/executed with semantic warnings/i)).toBeVisible();
    expect(screen.getByText(/crs-mismatch/i)).toBeVisible();
  });

  it("uses the durable job reference while execution is active", () => {
    renderReview(authorizedSession());

    expect(screen.getByRole("status")).toHaveTextContent(/waiting for the execution/i);
  });

  it("keeps nearest source-target rows inspectable through the table toggle", async () => {
    const user = userEvent.setup();
    renderReview(nearestCandidateSession());

    expect(screen.getByText(/directional planar Euclidean/i)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Table" }));
    const dialog = screen.getByRole("dialog", { name: "Result table" });
    expect(within(dialog).getByRole("columnheader", { name: "Source" })).toBeVisible();
    expect(within(dialog).getByRole("columnheader", { name: "Distance (m)" })).toBeVisible();
    expect(within(dialog).getAllByRole("cell", { name: "source-tie" })).toHaveLength(2);
    expect(within(dialog).getByRole("cell", { name: "target-zero" })).toBeVisible();
  });
});

function renderReview(session: QuestionSession) {
  return render(
    <CandidateAnswerReview
      session={session}
      feedback=""
      mutating={false}
      onFeedbackChange={vi.fn()}
      onDecision={vi.fn()}
    />,
  );
}
