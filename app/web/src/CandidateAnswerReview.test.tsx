// SPDX-License-Identifier: GPL-3.0-only

// Candidate Answer review tests: result states, decisions, table modal.

import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CandidateAnswerReview } from "./CandidateAnswerReview";
import {
  answerMap,
  candidateSession,
  nearestCandidateSession,
} from "./test/fixtures";
import type { AnswerMapFeatureCollection, QuestionSession } from "./types";

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

  it("keeps nearest source-target rows inspectable through the table toggle", async () => {
    const user = userEvent.setup();
    renderReview(nearestCandidateSession());

    expect(screen.getByText(/planar measurements in EPSG:28992/i)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Table" }));
    const dialog = screen.getByRole("dialog", { name: "Result table" });
    expect(within(dialog).getByRole("columnheader", { name: "source_points_id" })).toBeVisible();
    expect(within(dialog).getByRole("columnheader", { name: "distance_m" })).toBeVisible();
    expect(within(dialog).getAllByRole("cell", { name: "source-tie" })).toHaveLength(2);
    expect(within(dialog).getByRole("cell", { name: "target-zero" })).toBeVisible();
  });

  it("names each row from the Answer Map when the layer has names", async () => {
    const user = userEvent.setup();
    const map = answerMap();
    map.features[1].properties.name = "Zuidas";
    renderReview(candidateSession(), map);

    await user.click(screen.getByRole("button", { name: "Table" }));
    const dialog = screen.getByRole("dialog", { name: "Result table" });
    expect(within(dialog).getByRole("columnheader", { name: "name" })).toBeVisible();
    expect(within(dialog).getByRole("cell", { name: "Zuidas" })).toBeVisible();
  });
});

function renderReview(session: QuestionSession, map: AnswerMapFeatureCollection | null = null) {
  return render(
    <CandidateAnswerReview
      session={session}
      answerMap={map}
      feedback=""
      mutating={false}
      onFeedbackChange={vi.fn()}
      onDecision={vi.fn()}
    />,
  );
}
