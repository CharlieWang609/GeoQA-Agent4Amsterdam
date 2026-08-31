// SPDX-License-Identifier: GPL-3.0-only

// Draft review tests: plan sections, workflow modal, unsupported results.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { DraftReview } from "./DraftReview";
import { passDraft } from "./test/fixtures";

describe("Workflow review", () => {
  it("renders one connected SVG with abstract bands and declared edge labels", async () => {
    const user = userEvent.setup();
    render(<DraftReview draft={passDraft()} versionControl={null} actions={null} />);

    await user.click(screen.getByRole("button", { name: /workflow/i }));
    const dialog = screen.getByRole("dialog", { name: "Workflow" });
    const diagram = within(dialog).getByRole("img", { name: /workflow data flow/i });

    expect(within(dialog).queryByRole("heading", { name: "Abstract workflow" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("heading", { name: "Concrete workflow" })).not.toBeInTheDocument();
    expect(within(diagram).getByText("Select active supports")).toBeVisible();
    expect(within(diagram).getByText("Count sports within supports")).toBeVisible();
    expect(within(diagram).getByText("Select zero count supports")).toBeVisible();
    expect(within(diagram).getByText("Active supports")).toBeVisible();
    expect(within(diagram).getByText("Sports points")).toBeVisible();
    expect(diagram.querySelectorAll("[data-node-id]")).toHaveLength(6);
    expect(diagram.querySelectorAll('path[marker-end="url(#workflow-arrowhead)"]')).toHaveLength(5);

    const nodes = [...diagram.querySelectorAll<SVGGElement>("[data-node-id]")];
    const edges = [...diagram.querySelectorAll<SVGPathElement>("[data-edge-from][data-edge-to]")];
    expect(nodes.filter((node) => node.dataset.nodeKind === "source").map((node) => node.dataset.nodeId)).toEqual([
      "source:supports",
      "source:sports_points",
    ]);
    expect(edges.map((edge) => `${edge.dataset.edgeFrom}>${edge.dataset.edgeTo}`)).toEqual([
      "source:supports>step:select",
      "source:sports_points>step:count",
      "step:select>step:count",
      "step:count>step:select-zero",
      "step:select-zero>output:zero_count_supports",
    ]);
  });

  it("falls back to grouped chains when a declared ref cannot be interpreted", async () => {
    const draft = passDraft();
    const steps = draft.abstract_workflow!.steps as Array<Record<string, unknown>>;
    steps[1] = { ...steps[1], input_refs: ["sports_points", "missing_supports"] };
    const user = userEvent.setup();
    render(<DraftReview draft={draft} versionControl={null} actions={null} />);

    await user.click(screen.getByRole("button", { name: /workflow/i }));
    const dialog = screen.getByRole("dialog", { name: "Workflow" });

    expect(within(dialog).queryByRole("img", { name: /workflow data flow/i })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("heading", { name: "Abstract workflow" })).toBeVisible();
    expect(within(dialog).getByRole("heading", { name: "Concrete workflow" })).toBeVisible();
    expect(within(dialog).getByText("Geopandas:countpointsinpolygon")).toBeVisible();
  });
});
