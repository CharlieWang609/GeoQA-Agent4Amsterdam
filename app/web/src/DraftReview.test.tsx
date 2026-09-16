// SPDX-License-Identifier: GPL-3.0-only

// Draft review tests: plan sections, workflow modal, unsupported results.

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { DraftReview } from "./DraftReview";
import { passDraft } from "./test/fixtures";
import { svgTextContent } from "./test/svgText";

describe("Workflow review", () => {
  it("renders one connected SVG with declared edge labels", async () => {
    const user = userEvent.setup();
    render(<DraftReview draft={passDraft()} versionControl={null} actions={null} />);

    await user.click(screen.getByRole("button", { name: /workflow/i }));
    const dialog = screen.getByRole("dialog", { name: "Workflow" });
    const diagram = within(dialog).getByRole("img", { name: /workflow data flow/i });

    expect(within(dialog).queryByRole("heading", { name: "Workflow steps" })).not.toBeInTheDocument();
    expect(within(diagram).getByText("Select", { selector: "tspan" })).toBeVisible();
    expect(within(diagram).getByText("Count", { selector: "tspan" })).toBeVisible();
    expect(within(diagram).getByText("Select zero", { selector: "tspan" })).toBeVisible();
    expect(within(diagram).getByText("countpointsinpolygon", { selector: "tspan" })).toBeVisible();
    expect(within(diagram).getByText(svgTextContent("Active supports"))).toBeVisible();
    expect(within(diagram).getByText(svgTextContent("Sports points"))).toBeVisible();
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
      "step:select>step:count",
      "source:sports_points>step:count",
      "step:count>step:select-zero",
      "step:select-zero>output:zero_count_supports",
    ]);
  });

  it("names the planning tier that produced the draft", () => {
    const draft = { ...passDraft(), planning_source: "retrieval" as const };
    render(<DraftReview draft={draft} versionControl={null} actions={null} />);

    expect(screen.getByText(/Draft 1 · Replayed/)).toBeVisible();
  });

  it("lists nearest source and target identity fields under Retrieved Data", () => {
    const draft = passDraft();
    draft.task_specification = {
      required_output: "nearest swimming pool per gymnasium",
      roles: [
        { role: "source_points", semantic_label: "gymnasium", identity_fields: ["id"], geometry_types: ["Point"] },
        { role: "target_points", semantic_label: "swimming pool", identity_fields: ["id", "naam"], geometry_types: ["Point"] },
      ],
      goal: {
        per: ["source_points", "target_points"],
        value_name: "distance_m",
        value_scale: "numeric",
        aggregation: "distance",
        value_attribute: null,
        selection: false,
      },
      quantities: [],
      periods: [],
    };
    draft.bindings = [
      { role: "source_points", capability_input_ref: "source_points", dataset_id: "sport", layer_id: "gymzaal", analytical_compatibility: { passed: true, reasons: [] } },
      { role: "target_points", capability_input_ref: "target_points", dataset_id: "sport", layer_id: "zwembad", analytical_compatibility: { passed: true, reasons: [] } },
    ];
    render(<DraftReview draft={draft} versionControl={null} actions={null} />);

    const retrieved = screen.getByText("Retrieved Data").closest("details")!;
    const attributes = [...retrieved.querySelectorAll("dd")].map((item) => item.textContent);
    expect(attributes).toContain("id");
    expect(attributes).toContain("id, naam");
  });

  it("falls back to the step list when a declared ref cannot be interpreted", async () => {
    const draft = passDraft();
    const steps = draft.concrete_workflow!.steps as Array<Record<string, unknown>>;
    const parameters = steps[1].parameters as Array<Record<string, unknown>>;
    steps[1] = {
      ...steps[1],
      parameters: parameters.map((parameter) =>
        parameter.name === "polygons" ? { ...parameter, value: "missing_supports" } : parameter,
      ),
    };
    const user = userEvent.setup();
    render(<DraftReview draft={draft} versionControl={null} actions={null} />);

    await user.click(screen.getByRole("button", { name: /workflow/i }));
    const dialog = screen.getByRole("dialog", { name: "Workflow" });

    expect(within(dialog).queryByRole("img", { name: /workflow data flow/i })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("heading", { name: "Workflow steps" })).toBeVisible();
    expect(within(dialog).getByText("Geopandas:countpointsinpolygon")).toBeVisible();
  });
});
