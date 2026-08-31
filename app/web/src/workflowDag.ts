// SPDX-License-Identifier: GPL-3.0-only

// Derive and lay out a renderable dataflow DAG from a draft's abstract and
// concrete workflows. Parsing is fail-closed: any structural inconsistency
// makes deriveWorkflowDag return null and the UI falls back to a non-graph
// rendering instead of drawing a wrong diagram.

import type { JsonValue, SessionDraftVersion } from "./types";

export type WorkflowDagNodeKind = "source" | "step" | "output";

export interface WorkflowDagNode {
  id: string;
  kind: WorkflowDagNodeKind;
  label: string;
  bandId: string | null;
}

export interface WorkflowDagEdge {
  id: string;
  from: string;
  to: string;
  ref: string;
  label: string;
}

export interface WorkflowDagBand {
  id: string;
  label: string;
  stepNodeIds: string[];
}

export interface WorkflowDag {
  nodes: WorkflowDagNode[];
  edges: WorkflowDagEdge[];
  bands: WorkflowDagBand[];
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
}

export interface PositionedWorkflowBand extends WorkflowDagBand {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface WorkflowDagLayout {
  width: number;
  height: number;
  nodes: PositionedWorkflowNode[];
  edges: PositionedWorkflowEdge[];
  bands: PositionedWorkflowBand[];
}

interface AbstractStep {
  id: string;
  label: string;
  inputRefs: string[];
  outputRef: string;
}

interface ConcreteStep {
  id: string;
  abstractStepId: string;
  label: string;
  inputRefs: Set<string>;
  outputRefs: string[];
}

const NODE_WIDTH = 168;
const NODE_HEIGHT = 62;
const RANK_GAP = 112;
const NODE_GAP = 30;
const BAND_HEADER_HEIGHT = 26;
const BAND_PADDING = 12;
const BAND_GAP = 26;
const MARGIN_X = 42;
const MARGIN_Y = 34;

// Nodes are the bound source layers, every concrete step (grouped into one
// band per abstract step), and the single final output; edges carry the
// workflow refs that connect them.
export function deriveWorkflowDag(draft: SessionDraftVersion): WorkflowDag | null {
  const abstractRecords = strictRecordArray(draft.abstract_workflow?.steps);
  const concreteRecords = strictRecordArray(draft.concrete_workflow?.steps);
  const abstractFinalRef = nonBlankString(draft.abstract_workflow?.final_output_ref);
  const concreteFinalRef = nonBlankString(draft.concrete_workflow?.final_output_ref);
  if (
    !abstractRecords?.length ||
    !concreteRecords?.length ||
    !abstractFinalRef ||
    concreteFinalRef !== abstractFinalRef
  ) {
    return null;
  }

  const abstractSteps = abstractRecords.map(parseAbstractStep);
  const concreteSteps = concreteRecords.map(parseConcreteStep);
  if (abstractSteps.some((step) => step === null) || concreteSteps.some((step) => step === null)) {
    return null;
  }
  const parsedAbstractSteps = abstractSteps as AbstractStep[];
  const parsedConcreteSteps = concreteSteps as ConcreteStep[];
  if (
    hasDuplicates(parsedAbstractSteps.map((step) => step.id)) ||
    hasDuplicates(parsedAbstractSteps.map((step) => step.outputRef)) ||
    hasDuplicates(parsedConcreteSteps.map((step) => step.id))
  ) {
    return null;
  }

  const abstractById = new Map(parsedAbstractSteps.map((step) => [step.id, step]));
  if (parsedConcreteSteps.some((step) => !abstractById.has(step.abstractStepId))) {
    return null;
  }
  const concreteByAbstract = new Map<string, ConcreteStep[]>();
  parsedAbstractSteps.forEach((step) => concreteByAbstract.set(step.id, []));
  parsedConcreteSteps.forEach((step) => concreteByAbstract.get(step.abstractStepId)!.push(step));
  if ([...concreteByAbstract.values()].some((steps) => steps.length === 0)) {
    return null;
  }

  const sourceRefs: string[] = [];
  const producerByRef = new Map<string, AbstractStep>(
    parsedAbstractSteps.map((step) => [step.outputRef, step]),
  );
  for (const step of parsedAbstractSteps) {
    for (const ref of step.inputRefs) {
      if (!producerByRef.has(ref) && !sourceRefs.includes(ref)) sourceRefs.push(ref);
    }
  }
  const sourceLabels = bindingLabels(draft, sourceRefs);
  if (!sourceLabels) return null;

  const nodes: WorkflowDagNode[] = sourceRefs.map((ref) => ({
    id: sourceNodeId(ref),
    kind: "source",
    label: sourceLabels.get(ref)!,
    bandId: null,
  }));
  const bands: WorkflowDagBand[] = [];
  const firstNodeByAbstract = new Map<string, string>();
  const lastNodeByAbstract = new Map<string, string>();
  const internalEdges: WorkflowDagEdge[] = [];

  for (const abstractStep of parsedAbstractSteps) {
    const grouped = concreteByAbstract.get(abstractStep.id)!;
    // The band's first concrete step must consume every abstract input, and
    // consecutive steps must be chained by exactly one carried ref.
    if (abstractStep.inputRefs.some((ref) => !grouped[0].inputRefs.has(ref))) {
      return null;
    }
    const stepNodeIds = grouped.map((step) => stepNodeId(step.id));
    grouped.forEach((step) => nodes.push({
      id: stepNodeId(step.id),
      kind: "step",
      label: step.label,
      bandId: abstractStep.id,
    }));
    bands.push({ id: abstractStep.id, label: abstractStep.label, stepNodeIds });
    firstNodeByAbstract.set(abstractStep.id, stepNodeIds[0]);
    lastNodeByAbstract.set(abstractStep.id, stepNodeIds.at(-1)!);

    for (let index = 0; index < grouped.length - 1; index += 1) {
      const current = grouped[index];
      const next = grouped[index + 1];
      const carriedRefs = current.outputRefs.filter((ref) => next.inputRefs.has(ref));
      if (carriedRefs.length !== 1) return null;
      internalEdges.push(edge(stepNodeIds[index], stepNodeIds[index + 1], carriedRefs[0]));
    }
    if (!grouped.at(-1)!.outputRefs.includes(abstractStep.outputRef)) return null;
  }

  nodes.push({
    id: outputNodeId(abstractFinalRef),
    kind: "output",
    label: readableRef(abstractFinalRef),
    bandId: null,
  });

  const edges: WorkflowDagEdge[] = [];
  for (const abstractStep of parsedAbstractSteps) {
    const target = firstNodeByAbstract.get(abstractStep.id)!;
    for (const ref of abstractStep.inputRefs) {
      const producer = producerByRef.get(ref);
      const from = producer
        ? lastNodeByAbstract.get(producer.id)
        : sourceLabels.has(ref)
          ? sourceNodeId(ref)
          : undefined;
      if (!from) return null;
      edges.push(edge(from, target, ref));
    }
    const groupedIds = new Set(bands.find((band) => band.id === abstractStep.id)!.stepNodeIds);
    edges.push(...internalEdges.filter((item) => groupedIds.has(item.from)));
  }
  const finalProducer = producerByRef.get(abstractFinalRef);
  if (!finalProducer) return null;
  edges.push(edge(
    lastNodeByAbstract.get(finalProducer.id)!,
    outputNodeId(abstractFinalRef),
    abstractFinalRef,
  ));

  const dag = { nodes, edges: withUniqueEdgeIds(edges), bands };
  return isConnectedDag(dag) ? dag : null;
}

// Columns follow topological rank; each band occupies a horizontal lane;
// edges that skip more than one rank are routed below the content on their
// own tracks so they never cross through nodes.
export function layoutWorkflowDag(dag: WorkflowDag): WorkflowDagLayout {
  const ranks = topologicalRanks(dag);
  if (!ranks) throw new Error("Workflow DAG layout requires an acyclic graph.");
  const bandLanes = assignBandLanes(dag, ranks);
  const bandHeight = BAND_HEADER_HEIGHT + NODE_HEIGHT + BAND_PADDING * 2;
  const nodesByRank = new Map<number, WorkflowDagNode[]>();
  dag.nodes.forEach((node) => {
    const rank = ranks.get(node.id)!;
    const ranked = nodesByRank.get(rank) ?? [];
    ranked.push(node);
    nodesByRank.set(rank, ranked);
  });

  const nodes = dag.nodes.map<PositionedWorkflowNode>((node) => {
    const rank = ranks.get(node.id)!;
    const x = MARGIN_X + rank * (NODE_WIDTH + RANK_GAP);
    const y = node.bandId
      ? MARGIN_Y + BAND_HEADER_HEIGHT + BAND_PADDING +
        bandLanes.get(node.bandId)! * (bandHeight + BAND_GAP)
      : MARGIN_Y + nodesByRank.get(rank)!.filter((item) => !item.bandId).indexOf(node) *
        (NODE_HEIGHT + NODE_GAP);
    return { ...node, x, y, width: NODE_WIDTH, height: NODE_HEIGHT, rank };
  });
  const positionedById = new Map(nodes.map((node) => [node.id, node]));
  const bands = dag.bands.map<PositionedWorkflowBand>((band) => {
    const bandNodes = band.stepNodeIds.map((id) => positionedById.get(id)!);
    const left = Math.min(...bandNodes.map((node) => node.x)) - BAND_PADDING;
    const right = Math.max(...bandNodes.map((node) => node.x + node.width)) + BAND_PADDING;
    return {
      ...band,
      x: left,
      y: bandNodes[0].y - BAND_HEADER_HEIGHT - BAND_PADDING,
      width: right - left,
      height: bandHeight,
    };
  });
  const contentBottom = Math.max(
    ...nodes.map((node) => node.y + node.height),
    ...bands.map((band) => band.y + band.height),
  );
  let longEdgeTrack = 0;
  const edges = dag.edges.map<PositionedWorkflowEdge>((item) => {
    const from = positionedById.get(item.from)!;
    const to = positionedById.get(item.to)!;
    const startX = from.x + from.width;
    const startY = from.y + from.height / 2;
    const endX = to.x;
    const endY = to.y + to.height / 2;
    if (to.rank - from.rank > 1) {
      const routeY = contentBottom + 22 + longEdgeTrack * 28;
      longEdgeTrack += 1;
      return {
        ...item,
        path: `M ${startX} ${startY} C ${startX + 30} ${startY}, ${startX + 30} ${routeY}, ${startX + 60} ${routeY} L ${endX - 60} ${routeY} C ${endX - 30} ${routeY}, ${endX - 30} ${endY}, ${endX} ${endY}`,
        labelX: (startX + endX) / 2,
        labelY: routeY - 7,
      };
    }
    const curve = Math.max(38, (endX - startX) / 2);
    return {
      ...item,
      path: `M ${startX} ${startY} C ${startX + curve} ${startY}, ${endX - curve} ${endY}, ${endX} ${endY}`,
      labelX: (startX + endX) / 2,
      labelY: (startY + endY) / 2 - 8,
    };
  });
  const maxRank = Math.max(...ranks.values());
  const routedBottom = longEdgeTrack === 0
    ? contentBottom
    : contentBottom + 22 + (longEdgeTrack - 1) * 28;
  return {
    width: MARGIN_X * 2 + NODE_WIDTH + maxRank * (NODE_WIDTH + RANK_GAP),
    height: routedBottom + MARGIN_Y,
    nodes,
    edges,
    bands,
  };
}

function parseAbstractStep(record: Record<string, JsonValue>): AbstractStep | null {
  const id = nonBlankString(record.step_id);
  const inputRefs = strictStringArray(record.input_refs);
  const outputRef = nonBlankString(record.output_ref);
  if (!id || !inputRefs?.length || hasDuplicates(inputRefs) || !outputRef) return null;
  return {
    id,
    label: readableRef(nonBlankString(record.label) ?? nonBlankString(record.operation) ?? id),
    inputRefs,
    outputRef,
  };
}

function parseConcreteStep(record: Record<string, JsonValue>): ConcreteStep | null {
  const id = nonBlankString(record.step_id);
  const abstractStepId = nonBlankString(record.abstract_step_id);
  const algorithmId = nonBlankString(record.algorithm_id);
  const parameters = strictRecordArray(record.parameters);
  const outputs = strictRecordArray(record.outputs);
  if (!id || !abstractStepId || !algorithmId || !parameters || !outputs?.length) return null;
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
    abstractStepId,
    label: readableAlgorithm(algorithmId),
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
    labels.set(ref, readableRef(label));
  }
  return refs.every((ref) => labels.has(ref)) ? labels : null;
}

function edge(from: string, to: string, ref: string): WorkflowDagEdge {
  return { id: "", from, to, ref, label: readableRef(ref) };
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

// Greedy interval packing: a band takes the first lane whose last occupied
// rank ends before the band starts.
function assignBandLanes(dag: WorkflowDag, ranks: Map<string, number>): Map<string, number> {
  const laneEnds: number[] = [];
  const lanes = new Map<string, number>();
  dag.bands.forEach((band) => {
    const bandRanks = band.stepNodeIds.map((id) => ranks.get(id)!);
    const start = Math.min(...bandRanks);
    const end = Math.max(...bandRanks);
    let lane = laneEnds.findIndex((laneEnd) => laneEnd < start);
    if (lane === -1) lane = laneEnds.length;
    laneEnds[lane] = end;
    lanes.set(band.id, lane);
  });
  return lanes;
}

function strictRecordArray(value: JsonValue | undefined): Record<string, JsonValue>[] | null {
  if (!Array.isArray(value)) return null;
  const records = value.filter(
    (item): item is Record<string, JsonValue> => Boolean(item) && !Array.isArray(item) && typeof item === "object",
  );
  return records.length === value.length ? records : null;
}

function strictStringArray(value: JsonValue | undefined): string[] | null {
  if (!Array.isArray(value)) return null;
  const strings = value.map(nonBlankString);
  return strings.some((item) => item === null) ? null : strings as string[];
}

function nonBlankString(value: JsonValue | undefined): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
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

function readableAlgorithm(value: string): string {
  return readableRef(value.includes(":") ? value.split(":").at(-1)! : value);
}

function readableRef(value: string): string {
  const terminal = value.includes("#") ? value.split("#").at(-1)! : value;
  const spaced = terminal
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .trim();
  return spaced ? `${spaced.charAt(0).toUpperCase()}${spaced.slice(1)}` : value;
}
