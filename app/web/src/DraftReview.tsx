// SPDX-License-Identifier: GPL-3.0-only

import { useState, type ReactNode } from "react";

import { Modal } from "./Modal";
import type { JsonValue, SessionDraftVersion } from "./types";
import { deriveWorkflowDag, layoutWorkflowDag, type WorkflowDagLayout } from "./workflowDag";

export function DraftReview({ draft, versionControl, actions }: {
  draft: SessionDraftVersion;
  versionControl: ReactNode;
  actions: ReactNode;
}) {
  const abstractSteps = workflowSteps(draft.abstract_workflow);
  const concreteSteps = workflowSteps(draft.concrete_workflow);
  const workflowDag = deriveWorkflowDag(draft);
  const workflowLayout = workflowDag ? layoutWorkflowDag(workflowDag) : null;
  const [workflowOpen, setWorkflowOpen] = useState(false);

  return (
    <article className="plan-card" aria-label="Analysis Plan">
      <details className="plan-details">
        <summary className="card-heading">
          <span><small className="eyebrow">Draft {draft.version}</small><strong className="card-title">Analysis Plan</strong></span>
          <span className="plan-summary-end">
            <span className={`status-badge status-${draft.validation.status}`}>{validationLabel(draft.validation.status)}</span>
            <span className="details-chevron" aria-hidden="true">⌄</span>
          </span>
        </summary>
        <div className="plan-body">
          {versionControl}
          <ReviewSection heading="Question phrases">
            <div className="phrase-list">
              {draft.question_phrases.map((phrase, index) => (
                <span className="phrase-chip" key={`${String(phrase.text ?? "phrase")}-${index}`}>
                  {String(phrase.text ?? "Unlabelled phrase")}
                  <small>{readableLabel(String(phrase.role ?? phrase.functional_role ?? "phrase"))}</small>
                </span>
              ))}
            </div>
          </ReviewSection>
          <ReviewSection heading="Task specification"><DefinitionList value={draft.task_specification} /></ReviewSection>
          <ReviewSection heading="Retrieved Data">
            <div className="binding-list">
              {retrievedData(draft).map((binding, index) => (
                <dl key={`${binding.dataset}-${binding.layer}-${index}`}>
                  <Definition term="Dataset" value={binding.dataset} />
                  <Definition term="Layer" value={binding.layer} />
                  <Definition term="CCD" value={binding.ccd} />
                  <Definition term="Attributes" value={binding.attributes.join(", ") || "Not specified"} />
                </dl>
              ))}
            </div>
          </ReviewSection>
          <button type="button" className="workflow-entry" onClick={() => setWorkflowOpen(true)}>
            <span>Workflow</span>
            <small>View declared data flow</small>
          </button>
          <ReviewSection heading="Assumptions">
            {draft.assumptions.length ? <ul>{draft.assumptions.map((item) => <li key={item}>{item}</li>)}</ul> : <p>No assumptions.</p>}
            {draft.unresolved_items.length > 0 && <><h4>Unresolved items</h4><ul>{draft.unresolved_items.map((item) => <li key={item}>{item}</li>)}</ul></>}
          </ReviewSection>
          <section>
            <h3>Validation diagnostics</h3>
            {draft.validation.diagnostics.length ? (
              <ul className="diagnostic-list">
                {draft.validation.diagnostics.map((diagnostic, index) => (
                  <li key={`${String(diagnostic.code ?? "diagnostic")}-${index}`}>
                    <strong>{String(diagnostic.code ?? "Diagnostic")}</strong>
                    {diagnostic.severity ? ` · ${String(diagnostic.severity)}` : ""}
                    {`: ${String(diagnostic.message ?? "No message supplied.")}`}
                  </li>
                ))}
              </ul>
            ) : <p>No validation diagnostics.</p>}
            {draft.unsupported_result && (
              <div className="unsupported-result">
                <h4>Why matching stopped</h4>
                {recordArray(draft.unsupported_result.failed_roles).map((failedRole, index) => (
                  <div key={`${String(failedRole.role ?? "role")}-${index}`}>
                    <strong>{readableLabel(String(failedRole.role ?? "Data role"))}</strong>
                    <ul>
                      {recordArray(failedRole.closest_candidates).map((candidate, candidateIndex) => (
                        <li key={candidateIndex}>
                          {stringArray(candidate.rejection_reasons).join(" ") || "No compatible data was found."}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            )}
          </section>
          {actions}
        </div>
      </details>
      {workflowOpen && (
        <Modal title="Workflow" onClose={() => setWorkflowOpen(false)}>
          {workflowLayout ? <WorkflowDiagram layout={workflowLayout} /> : (
            <>
              <section className="modal-workflow-section">
                <h3>Abstract workflow</h3>
                <WorkflowChain steps={abstractSteps} />
              </section>
              <section className="modal-workflow-section">
                <h3>Concrete workflow</h3>
                <ConcreteWorkflowChain abstractSteps={abstractSteps} concreteSteps={concreteSteps} />
              </section>
            </>
          )}
        </Modal>
      )}
    </article>
  );
}

function WorkflowDiagram({ layout }: { layout: WorkflowDagLayout }) {
  return (
    <div className="workflow-dag-scroll">
      <svg
        className="workflow-dag"
        role="img"
        aria-label="Workflow data flow"
        width={layout.width}
        height={layout.height}
        viewBox={`0 0 ${layout.width} ${layout.height}`}
      >
        <defs>
          <marker
            id="workflow-arrowhead"
            markerWidth="8"
            markerHeight="8"
            refX="7"
            refY="4"
            orient="auto"
            markerUnits="strokeWidth"
          >
            <path d="M 0 0 L 8 4 L 0 8 z" />
          </marker>
        </defs>
        {layout.bands.map((band) => (
          <g className="workflow-band" key={band.id}>
            <rect x={band.x} y={band.y} width={band.width} height={band.height} rx="10" />
            <text x={band.x + 10} y={band.y + 18}>{band.label}</text>
          </g>
        ))}
        {layout.edges.map((edge) => (
          <g className="workflow-edge" key={edge.id}>
            <path
              d={edge.path}
              markerEnd="url(#workflow-arrowhead)"
              data-edge-ref={edge.ref}
              data-edge-from={edge.from}
              data-edge-to={edge.to}
            />
            <text x={edge.labelX} y={edge.labelY} textAnchor="middle">{edge.label}</text>
          </g>
        ))}
        {layout.nodes.map((node) => (
          <g
            className={`workflow-node workflow-node-${node.kind}`}
            data-node-id={node.id}
            data-node-kind={node.kind}
            key={node.id}
            transform={`translate(${node.x} ${node.y})`}
          >
            <rect width={node.width} height={node.height} rx="8" />
            <text x={node.width / 2} y="22" textAnchor="middle">
              <tspan className="workflow-node-kind" x={node.width / 2}>{node.kind === "output" ? "Result" : node.kind}</tspan>
              <tspan x={node.width / 2} dy="21">{node.label}</tspan>
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}

function ReviewSection({ heading, children }: { heading: string; children: ReactNode }) {
  return (
    <details className="review-section">
      <summary><span>{heading}</span><span className="details-chevron" aria-hidden="true">⌄</span></summary>
      <div>{children}</div>
    </details>
  );
}

function DefinitionList({ value }: { value: Record<string, JsonValue> }) {
  return <dl className="readable-definition">{Object.entries(value).map(([key, item]) => <Definition key={key} term={readableLabel(key)} value={formatValue(item)} />)}</dl>;
}

function Definition({ term, value }: { term: string; value: string }) {
  return <><dt>{term}</dt><dd>{value}</dd></>;
}

function WorkflowChain({ steps }: { steps: Record<string, JsonValue>[] }) {
  return steps.length ? (
    <ol className="workflow-chain workflow-chain-abstract">
      {steps.map((step, index) => (
        <li key={`${String(step.step_id ?? "step")}-${index}`}>
          {readableLabel(String(step.label ?? step.operation ?? step.abstraction_id ?? step.step_id ?? `Step ${index + 1}`))}
        </li>
      ))}
    </ol>
  ) : <p>Not available for this draft.</p>;
}

function ConcreteWorkflowChain({
  abstractSteps,
  concreteSteps,
}: {
  abstractSteps: Record<string, JsonValue>[];
  concreteSteps: Record<string, JsonValue>[];
}) {
  if (!concreteSteps.length) return <p>Not available for this draft.</p>;
  const abstractIds = abstractSteps.map((step) => String(step.step_id ?? ""));
  const unmatchedIds = concreteSteps
    .map((step) => String(step.abstract_step_id ?? "Execution step"))
    .filter((id, index, ids) => !abstractIds.includes(id) && ids.indexOf(id) === index);
  return (
    <div className="workflow-alignment">
      {[...abstractIds, ...unmatchedIds].map((abstractId) => {
        const grouped = concreteSteps.filter(
          (step) => String(step.abstract_step_id ?? "Execution step") === abstractId,
        );
        return (
          <div key={abstractId} className="workflow-column">
            <small>{readableLabel(abstractId)}</small>
            {grouped.length ? (
              <ol className="workflow-subchain">
                {grouped.map((step, index) => (
                  <li key={`${String(step.step_id ?? "step")}-${index}`}>
                    {readableLabel(String(step.algorithm_id ?? step.name ?? step.step_id ?? `Step ${index + 1}`))}
                  </li>
                ))}
              </ol>
            ) : <span className="workflow-empty">No execution step</span>}
          </div>
        );
      })}
    </div>
  );
}

type RetrievedBinding = { dataset: string; layer: string; ccd: string; attributes: string[] };

function retrievedData(draft: SessionDraftVersion): RetrievedBinding[] {
  const attributesByRef = workflowAttributes(draft);
  return draft.bindings.map((binding) => {
    const capabilityRef = String(binding.capability_input_ref ?? "");
    const assessment = asRecord(binding.analytical_compatibility);
    return {
      dataset: String(binding.dataset_id ?? "Not specified"),
      layer: String(binding.layer_id ?? "Not specified"),
      ccd: assessment?.passed === true
        ? String(binding.analytical_ccd_meaning ?? "Not specified")
        : "Not specified",
      attributes: [...(attributesByRef.get(capabilityRef) ?? [])].sort(),
    };
  });
}

// Infer which source attributes each binding contributes to the workflow:
// identity fields seed each binding's set, then the concrete steps are
// walked with a ref->source lineage map so fields named in expressions and
// *FIELD parameters are attributed to the bindings they actually read.
function workflowAttributes(draft: SessionDraftVersion) {
  const attributes = new Map<string, Set<string>>();
  const lineage = new Map<string, Set<string>>();
  draft.bindings.forEach((binding) => {
    const ref = String(binding.capability_input_ref ?? "");
    if (!ref) return;
    const requirement = bindingRequirement(draft, binding);
    lineage.set(ref, new Set([ref]));
    attributes.set(ref, new Set(requirement ? requirement.identityFields : []));
  });
  workflowSteps(draft.concrete_workflow).forEach((step) => {
    const parameters = recordArray(step.parameters);
    const refsByName = new Map<string, Set<string>>();
    parameters.forEach((parameter) => {
      if (parameter.source !== "ref") return;
      refsByName.set(String(parameter.name ?? ""), new Set(lineage.get(String(parameter.value ?? "")) ?? []));
    });
    const allInputs = unionSets([...refsByName.values()]);
    parameters.forEach((parameter) => {
      const name = String(parameter.name ?? "").toUpperCase();
      if (parameter.source === "template" || name.includes("EXPRESSION")) {
        quotedFields(parameter.value).forEach((field) => addAttribute(attributes, allInputs, field));
      }
      if (!name.includes("FIELD")) return;
      // Map each *field parameter to the input it reads from in the
      // operation contracts: class_field counts points, join_field(s) read
      // the join layer, fields_to_copy reads the nearest target, etc.
      const target = name.startsWith("CLASS")
        ? refsByName.get("points") ?? allInputs
        : name.startsWith("JOIN")
          ? refsByName.get("join") ?? allInputs
          : name.startsWith("FIELDS")
            ? refsByName.get("target") ?? allInputs
            : name.startsWith("INPUT")
              ? refsByName.get("input") ?? allInputs
              : refsByName.get("polygons") ?? refsByName.get("input") ?? allInputs;
      stringArray(parameter.value).forEach((field) => addAttribute(attributes, target, field));
    });
    const outputLineage = refsByName.get("polygons") ?? refsByName.get("input") ?? allInputs;
    recordArray(step.outputs).forEach((output) => {
      const ref = String(output.ref ?? "");
      if (ref) lineage.set(ref, new Set(outputLineage));
    });
  });
  return attributes;
}

function bindingRequirement(
  draft: SessionDraftVersion,
  binding: Record<string, JsonValue>,
): { identityFields: string[] } | null {
  const role = String(binding.role ?? "");
  // Older drafts embedded role_requirements in the task specification;
  // newer ones derive identity fields from the role's task-spec shape.
  const legacy = recordArray(draft.task_specification.role_requirements).find(
    (item) => String(item.capability_input_ref ?? "") === String(binding.capability_input_ref ?? ""),
  );
  if (legacy) {
    return {
      identityFields: stringArray(legacy.source_identity_fields),
    };
  }

  const taskRole = asRecord(draft.task_specification[
    role === "supports" ? "support" : role === "counted_objects" ? "counted_objects" : ""
  ]);
  if (!taskRole) return null;
  return {
    identityFields: role === "supports"
      ? stringArray(taskRole.identity_fields)
      : stringArray(taskRole.distinct_by),
  };
}

function addAttribute(attributes: Map<string, Set<string>>, refs: Set<string>, field: string) {
  if (field) refs.forEach((ref) => attributes.get(ref)?.add(field));
}

// Column references in the pandas query/eval dialect are bare identifiers;
// string literals and template placeholders are stripped, then the dialect's
// keywords and accessor names are filtered out.
const EXPRESSION_KEYWORDS = new Set(["and", "or", "not", "in", "isnull", "notnull", "True", "False"]);

function quotedFields(value: JsonValue | undefined) {
  if (typeof value !== "string") return [];
  const withoutLiterals = value.replace(/'[^']*'/g, "").replace(/\{[^}]*\}/g, "");
  return [...withoutLiterals.matchAll(/[A-Za-z_][A-Za-z0-9_]*/g)]
    .map((match) => match[0])
    .filter((token) => !EXPRESSION_KEYWORDS.has(token));
}

function stringArray(value: JsonValue | undefined): string[] {
  if (typeof value === "string") return [value];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function unionSets(values: Set<string>[]) {
  return new Set(values.flatMap((value) => [...value]));
}

function recordArray(value: JsonValue | undefined): Record<string, JsonValue>[] {
  return Array.isArray(value) ? value.filter((item): item is Record<string, JsonValue> => Boolean(item) && !Array.isArray(item) && typeof item === "object") : [];
}

function asRecord(value: JsonValue | undefined) {
  return value && !Array.isArray(value) && typeof value === "object" ? value : null;
}

function workflowSteps(workflow: Record<string, JsonValue> | null) {
  return recordArray(workflow?.steps);
}

function formatValue(value: JsonValue | undefined): string {
  if (value === undefined || value === null) return "Not specified";
  if (Array.isArray(value)) return value.map((item) => formatValue(item)).join(", ");
  if (typeof value === "object") return Object.entries(value).map(([key, item]) => `${readableLabel(key)}: ${formatValue(item)}`).join(" · ");
  return String(value);
}

function readableLabel(value: string) {
  const terminal = value.includes("#") ? value.split("#").at(-1)! : value;
  const spaced = terminal.replace(/[_-]+/g, " ").trim();
  return spaced ? `${spaced.charAt(0).toUpperCase()}${spaced.slice(1)}` : value;
}

function validationLabel(status: SessionDraftVersion["validation"]["status"]) {
  if (status === "pass") return "Passed";
  if (status === "pass_with_warnings") return "Passed with warnings";
  return "Failed";
}
