// SPDX-License-Identifier: GPL-3.0-only

// Workflow DAG tests: derivation from drafts, fail-closed nulls, layout.

import { describe, expect, it } from "vitest";

import { passDraft } from "./test/fixtures";
import { deriveWorkflowDag, layoutWorkflowDag } from "./workflowDag";

describe("workflow DAG derivation", () => {
  it("derives fan-in and edge labels exactly from declared data references", () => {
    const dag = deriveWorkflowDag(passDraft());

    expect(dag).not.toBeNull();
    expect(dag!.edges.map(({ from, to, ref, label }) => ({ from, to, ref, label }))).toEqual([
      { from: "source:supports", to: "step:select", ref: "supports", label: "Supports" },
      { from: "source:sports_points", to: "step:count", ref: "sports_points", label: "Sports points" },
      { from: "step:select", to: "step:count", ref: "active_supports", label: "Active supports" },
      { from: "step:count", to: "step:select-zero", ref: "support_counts", label: "Support counts" },
      { from: "step:select-zero", to: "output:zero_count_supports", ref: "zero_count_supports", label: "Zero count supports" },
    ]);
    expect(dag!.edges.filter((edge) => edge.to === "step:count")).toHaveLength(2);

    const reachable = new Set(dag!.nodes.filter((node) => node.kind === "source").map((node) => node.id));
    while (dag!.edges.some((edge) => reachable.has(edge.from) && !reachable.has(edge.to))) {
      dag!.edges.forEach((edge) => {
        if (reachable.has(edge.from)) reachable.add(edge.to);
      });
    }
    expect(reachable).toEqual(new Set(dag!.nodes.map((node) => node.id)));
  });

  it("ranks the join after both declared inputs", () => {
    const dag = deriveWorkflowDag(passDraft())!;
    const layout = layoutWorkflowDag(dag);
    const ranks = Object.fromEntries(layout.nodes.map((node) => [node.id, node.rank]));

    expect(ranks["source:supports"]).toBe(0);
    expect(ranks["source:sports_points"]).toBe(0);
    expect(ranks["step:select"]).toBe(1);
    expect(ranks["step:count"]).toBe(2);
    expect(ranks["step:count"]).toBeGreaterThan(ranks["step:select"]);
    expect(ranks["step:count"]).toBeGreaterThan(ranks["source:sports_points"]);
    expect(ranks["output:zero_count_supports"]).toBe(4);
  });

  it("keeps declared concrete output order within one abstract band", () => {
    const draft = passDraft();
    const concreteSteps = draft.concrete_workflow!.steps as Array<Record<string, unknown>>;
    const selectStep = concreteSteps[0];
    const parameters = selectStep.parameters as Array<Record<string, unknown>>;
    concreteSteps[0] = {
      ...selectStep,
      parameters: parameters.map((parameter) =>
        parameter.name === "input" ? { ...parameter, value: "normalized_supports" } : parameter,
      ),
    };
    concreteSteps.unshift({
      step_id: "prepare-supports",
      abstract_step_id: "select-active-supports",
      algorithm_id: "geopandas:centroids",
      parameters: [{ name: "input", source: "ref", value: "supports" }],
      outputs: [{ name: "output", ref: "normalized_supports", kind: "sink" }],
    });

    const dag = deriveWorkflowDag(draft);

    expect(dag).not.toBeNull();
    expect(dag!.bands[0].stepNodeIds).toEqual(["step:prepare-supports", "step:select"]);
    expect(dag!.edges).toContainEqual(expect.objectContaining({
      from: "step:prepare-supports",
      to: "step:select",
      ref: "normalized_supports",
      label: "Normalized supports",
    }));
  });

  it("routes a rank-skipping edge label clear of intermediate nodes", () => {
    const layout = layoutWorkflowDag(deriveWorkflowDag(passDraft())!);
    const sportsEdge = layout.edges.find((edge) => edge.ref === "sports_points")!;
    const selectNode = layout.nodes.find((node) => node.id === "step:select")!;

    expect(
      sportsEdge.labelX >= selectNode.x &&
      sportsEdge.labelX <= selectNode.x + selectNode.width &&
      sportsEdge.labelY >= selectNode.y &&
      sportsEdge.labelY <= selectNode.y + selectNode.height,
    ).toBe(false);
  });

  it("rejects an abstract input ref with no declared source or producer", () => {
    const draft = passDraft();
    const steps = draft.abstract_workflow!.steps as Array<Record<string, unknown>>;
    steps[1] = { ...steps[1], input_refs: ["sports_points", "missing_supports"] };

    expect(deriveWorkflowDag(draft)).toBeNull();
  });
});
