// SPDX-License-Identifier: GPL-3.0-only

// Which source attributes each data binding contributes to a draft's
// workflow, inferred from the task specification and the concrete steps.

import { recordArray, stringArray } from "./jsonRecords";
import type { JsonValue, SessionDraftVersion } from "./types";

export type RetrievedBinding = { dataset: string; layer: string; geometry: string; attributes: string[] };

export function retrievedData(draft: SessionDraftVersion): RetrievedBinding[] {
  const attributesByRef = workflowAttributes(draft);
  return draft.bindings.map((binding) => {
    const capabilityRef = String(binding.capability_input_ref ?? "");
    const requirement = bindingRequirement(draft, binding);
    return {
      dataset: String(binding.dataset_id ?? "Not specified"),
      layer: String(binding.layer_id ?? "Not specified"),
      geometry: requirement?.geometryTypes.join(", ") || "Not specified",
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

// A binding role is declared in the task specification's roles, which name
// the identity fields the workflow reads from that layer and its geometry.
function bindingRequirement(
  draft: SessionDraftVersion,
  binding: Record<string, JsonValue>,
): { identityFields: string[]; geometryTypes: string[] } | null {
  const role = String(binding.role ?? "");
  const taskRole = recordArray(draft.task_specification.roles).find(
    (item) => String(item.role ?? "") === role,
  );
  if (!taskRole) return null;
  return {
    identityFields: stringArray(taskRole.identity_fields),
    geometryTypes: stringArray(taskRole.geometry_types),
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

function unionSets(values: Set<string>[]) {
  return new Set(values.flatMap((value) => [...value]));
}

export function workflowSteps(workflow: Record<string, JsonValue> | null) {
  return recordArray(workflow?.steps);
}
