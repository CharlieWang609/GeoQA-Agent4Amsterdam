// SPDX-License-Identifier: GPL-3.0-only

// Derive and lay out a renderable dataflow DAG from a draft's concrete
// workflow. Parsing is fail-closed: any structural inconsistency makes
// deriveWorkflowDag return null and the UI falls back to a step list
// instead of drawing a wrong diagram.

import { nonBlankString, readableLabel, strictRecordArray } from "./jsonRecords";
import type { JsonValue, SessionDraftVersion } from "./types";

export type WorkflowDagNodeKind = "source" | "step" | "output";

export interface WorkflowDagNode {
  id: string;
  kind: WorkflowDagNodeKind;
  label: string;
  // The operation a step applies; sources and the output carry none.
  detail?: string;
}

export interface WorkflowDagEdge {
  id: string;
  from: string;
  to: string;
  ref: string;
  label: string;
}

export interface WorkflowDag {
  nodes: WorkflowDagNode[];
  edges: WorkflowDagEdge[];
}

export interface PositionedWorkflowNode extends WorkflowDagNode {
  x: number;
  y: number;
  width: number;
  height: number;
  rank: number;
}

export interface PositionedWorkflowEdge extends WorkflowDagEdge {
  path: string;
  labelX: number;
  labelY: number;
  labelLines: string[];
}

export interface WorkflowDagLayout {
  width: number;
  height: number;
  nodes: PositionedWorkflowNode[];
  edges: PositionedWorkflowEdge[];
}

interface ConcreteStep {
  id: string;
  label: string;
  operation: string;
  inputRefs: Set<string>;
  outputRefs: string[];
}

const NODE_WIDTH = 168;
const NODE_HEIGHT = 62;
const MIN_RANK_GAP = 132;
const NODE_GAP = 30;
const EDGE_LABEL_FONT = 10;
const EDGE_LABEL_LINE_HEIGHT = 12;
const EDGE_LABEL_MARGIN = 24;
const MARGIN_X = 42;
const MARGIN_Y = 34;

// Nodes are the bound source layers, every step, and the single final
// output; edges carry the workflow refs that connect them.
export function deriveWorkflowDag(draft: SessionDraftVersion): WorkflowDag | null {
  const records = strictRecordArray(draft.concrete_workflow?.steps);
  const finalRef = nonBlankString(draft.concrete_workflow?.final_output_ref);
  if (!records?.length || !finalRef) return null;

  const parsed = records.map(parseConcreteStep);
  if (parsed.some((step) => step === null)) return null;
  const steps = parsed as ConcreteStep[];
  if (hasDuplicates(steps.map((step) => step.id))) return null;

  const producerByRef = new Map<string, ConcreteStep>();
  for (const step of steps) {
    for (const ref of step.outputRefs) {
      if (producerByRef.has(ref)) return null;
      producerByRef.set(ref, step);
    }
  }
  const sourceRefs: string[] = [];
  for (const step of steps) {
    for (const ref of step.inputRefs) {
      if (!producerByRef.has(ref) && !sourceRefs.includes(ref)) sourceRefs.push(ref);
    }
  }
  const sourceLabels = bindingLabels(draft, sourceRefs);
  if (!sourceLabels) return null;
  const finalProducer = producerByRef.get(finalRef);
  if (!finalProducer) return null;

  const nodes: WorkflowDagNode[] = [
    ...sourceRefs.map<WorkflowDagNode>((ref) => ({ id: sourceNodeId(ref), kind: "source", label: sourceLabels.get(ref)! })),
    ...steps.map<WorkflowDagNode>((step) => ({ id: stepNodeId(step.id), kind: "step", label: step.label, detail: step.operation })),
    { id: outputNodeId(finalRef), kind: "output", label: readableLabel(finalRef) },
  ];
  const edges: WorkflowDagEdge[] = [];
  for (const step of steps) {
    for (const ref of step.inputRefs) {
      const producer = producerByRef.get(ref);
      edges.push(edge(producer ? stepNodeId(producer.id) : sourceNodeId(ref), stepNodeId(step.id), ref));
    }
  }
  edges.push(edge(stepNodeId(finalProducer.id), outputNodeId(finalRef), finalRef));

  const dag = { nodes, edges: withUniqueEdgeIds(edges) };
  return isConnectedDag(dag) ? dag : null;
}

// Columns follow topological rank; nodes of one rank stack vertically;
// edges that skip more than one rank are routed below the content on their
// own tracks so they never cross through nodes.
export function layoutWorkflowDag(dag: WorkflowDag): WorkflowDagLayout {
  const ranks = topologicalRanks(dag);
  if (!ranks) throw new Error("Workflow DAG layout requires an acyclic graph.");

  // Edge labels between adjacent columns are wrapped to the column gap, and
  // the gap grows to fit the longest wrapped line so labels never run under
  // the node boxes on either side.
  const edgeLabelLines = new Map<string, string[]>();
  let rankGap = MIN_RANK_GAP;
  dag.edges.forEach((item) => {
    if (ranks.get(item.to)! - ranks.get(item.from)! > 1) {
      edgeLabelLines.set(item.id, [item.label]);
      return;
    }
    const lines = wrapLabel(item.label, MIN_RANK_GAP - EDGE_LABEL_MARGIN, EDGE_LABEL_FONT, false);
    edgeLabelLines.set(item.id, lines);
    const widest = Math.max(...lines.map((line) => estimateTextWidth(line, EDGE_LABEL_FONT, false)));
    rankGap = Math.max(rankGap, widest + EDGE_LABEL_MARGIN);
  });

  const nodesByRank = new Map<number, WorkflowDagNode[]>();
  dag.nodes.forEach((node) => {
    const rank = ranks.get(node.id)!;
    const ranked = nodesByRank.get(rank) ?? [];
    ranked.push(node);
    nodesByRank.set(rank, ranked);
  });

  const nodes = dag.nodes.map<PositionedWorkflowNode>((node) => {
    const rank = ranks.get(node.id)!;
    const x = MARGIN_X + rank * (NODE_WIDTH + rankGap);
    const y = MARGIN_Y + nodesByRank.get(rank)!.indexOf(node) * (NODE_HEIGHT + NODE_GAP);
    return { ...node, x, y, width: NODE_WIDTH, height: NODE_HEIGHT, rank };
  });
  const positionedById = new Map(nodes.map((node) => [node.id, node]));
  const contentBottom = Math.max(...nodes.map((node) => node.y + node.height));
  let longEdgeTrack = 0;
  const edges = dag.edges.map<PositionedWorkflowEdge>((item) => {
    const from = positionedById.get(item.from)!;
    const to = positionedById.get(item.to)!;
    const startX = from.x + from.width;
    const startY = from.y + from.height / 2;
    const endX = to.x;
    const endY = to.y + to.height / 2;
    const labelLines = edgeLabelLines.get(item.id)!;
    if (to.rank - from.rank > 1) {
      const routeY = contentBottom + 22 + longEdgeTrack * 28;
      longEdgeTrack += 1;
      return {
        ...item,
        path: `M ${startX} ${startY} C ${startX + 30} ${startY}, ${startX + 30} ${routeY}, ${startX + 60} ${routeY} L ${endX - 60} ${routeY} C ${endX - 30} ${routeY}, ${endX - 30} ${endY}, ${endX} ${endY}`,
        labelX: (startX + endX) / 2,
        labelY: routeY - 7,
        labelLines,
      };
    }
    const curve = Math.max(38, (endX - startX) / 2);
    // labelY is the baseline of the first line; the block stacks downward
    // from there and ends just above the edge's midpoint.
    return {
      ...item,
      path: `M ${startX} ${startY} C ${startX + curve} ${startY}, ${endX - curve} ${endY}, ${endX} ${endY}`,
      labelX: (startX + endX) / 2,
      labelY: (startY + endY) / 2 - 8 - (labelLines.length - 1) * EDGE_LABEL_LINE_HEIGHT,
      labelLines,
    };
  });
  const maxRank = Math.max(...ranks.values());
  const routedBottom = longEdgeTrack === 0
    ? contentBottom
    : contentBottom + 22 + (longEdgeTrack - 1) * 28;
  return {
    width: MARGIN_X * 2 + NODE_WIDTH + maxRank * (NODE_WIDTH + rankGap),
    height: routedBottom + MARGIN_Y,
    nodes,
    edges,
  };
}

function parseConcreteStep(record: Record<string, JsonValue>): ConcreteStep | null {
  const id = nonBlankString(record.step_id);
  const algorithmId = nonBlankString(record.algorithm_id);
  const parameters = strictRecordArray(record.parameters);
  const outputs = strictRecordArray(record.outputs);
  if (!id || !algorithmId || !parameters || !outputs?.length) return null;
  const inputRefs = new Set(
    parameters
      .filter((parameter) => parameter.source === "ref")
      .map((parameter) => nonBlankString(parameter.value))
      .filter((ref): ref is string => ref !== null),
  );
  const outputRefs = outputs.map((output) => nonBlankString(output.ref));
  if (outputRefs.some((ref) => ref === null) || hasDuplicates(outputRefs as string[])) return null;
  return {
    id,
    label: readableLabel(id),
    operation: algorithmId.includes(":") ? algorithmId.split(":").at(-1)! : algorithmId,
    inputRefs,
    outputRefs: outputRefs as string[],
  };
}

function bindingLabels(draft: SessionDraftVersion, refs: string[]): Map<string, string> | null {
  const labels = new Map<string, string>();
  for (const binding of draft.bindings) {
    const ref = nonBlankString(binding.capability_input_ref);
    if (!ref || !refs.includes(ref)) continue;
    const label = nonBlankString(binding.layer_display_name) ??
      nonBlankString(binding.display_name) ??
      nonBlankString(binding.layer_id);
    if (!label || labels.has(ref)) return null;
    labels.set(ref, readableLabel(label));
  }
  return refs.every((ref) => labels.has(ref)) ? labels : null;
}

function edge(from: string, to: string, ref: string): WorkflowDagEdge {
  return { id: "", from, to, ref, label: readableLabel(ref) };
}

function withUniqueEdgeIds(edges: WorkflowDagEdge[]): WorkflowDagEdge[] {
  return edges.map((item, index) => ({ ...item, id: `edge:${index}:${item.ref}` }));
}

// Acyclic, every non-source node has an incoming edge, and every node is
// reachable from some source.
function isConnectedDag(dag: WorkflowDag): boolean {
  const ranks = topologicalRanks(dag);
  if (!ranks) return false;
  const sources = dag.nodes.filter((node) => node.kind === "source");
  const incoming = new Map(dag.nodes.map((node) => [node.id, 0]));
  dag.edges.forEach((item) => incoming.set(item.to, (incoming.get(item.to) ?? 0) + 1));
  if (dag.nodes.some((node) => incoming.get(node.id) === 0 && node.kind !== "source")) return false;
  const reachable = new Set(sources.map((node) => node.id));
  let changed = true;
  while (changed) {
    changed = false;
    dag.edges.forEach((item) => {
      if (reachable.has(item.from) && !reachable.has(item.to)) {
        reachable.add(item.to);
        changed = true;
      }
    });
  }
  return reachable.size === dag.nodes.length;
}

// Kahn's algorithm; rank = longest path from a source. Returns null on a
// cycle or an edge that references an unknown node.
function topologicalRanks(dag: WorkflowDag): Map<string, number> | null {
  const nodeIds = new Set(dag.nodes.map((node) => node.id));
  if (nodeIds.size !== dag.nodes.length || dag.edges.some((item) => !nodeIds.has(item.from) || !nodeIds.has(item.to))) {
    return null;
  }
  const incoming = new Map(dag.nodes.map((node) => [node.id, 0]));
  const outgoing = new Map(dag.nodes.map((node) => [node.id, [] as WorkflowDagEdge[]]));
  dag.edges.forEach((item) => {
    incoming.set(item.to, incoming.get(item.to)! + 1);
    outgoing.get(item.from)!.push(item);
  });
  const queue = dag.nodes.filter((node) => incoming.get(node.id) === 0).map((node) => node.id);
  const ranks = new Map(queue.map((id) => [id, 0]));
  let visited = 0;
  for (let index = 0; index < queue.length; index += 1) {
    const current = queue[index];
    visited += 1;
    for (const item of outgoing.get(current)!) {
      ranks.set(item.to, Math.max(ranks.get(item.to) ?? 0, ranks.get(current)! + 1));
      incoming.set(item.to, incoming.get(item.to)! - 1);
      if (incoming.get(item.to) === 0) queue.push(item.to);
    }
  }
  return visited === dag.nodes.length ? ranks : null;
}

// No text measurement is available at layout time (the layout is pure and
// runs under jsdom in tests), so widths are estimated from the character
// count with a per-font average advance.
function estimateTextWidth(text: string, fontSize: number, bold: boolean): number {
  return text.length * fontSize * (bold ? 0.64 : 0.56);
}

// Greedy word wrap; a single word longer than the limit stays on its own line.
function wrapLabel(text: string, maxWidth: number, fontSize: number, bold: boolean): string[] {
  const lines: string[] = [];
  let current = "";
  for (const word of text.split(/\s+/).filter(Boolean)) {
    const candidate = current ? `${current} ${word}` : word;
    if (current && estimateTextWidth(candidate, fontSize, bold) > maxWidth) {
      lines.push(current);
      current = word;
    } else {
      current = candidate;
    }
  }
  if (current) lines.push(current);
  return lines.length ? lines : [text];
}

function hasDuplicates(values: string[]): boolean {
  return new Set(values).size !== values.length;
}

function sourceNodeId(ref: string): string {
  return `source:${ref}`;
}

function stepNodeId(stepId: string): string {
  return `step:${stepId}`;
}

function outputNodeId(ref: string): string {
  return `output:${ref}`;
}
