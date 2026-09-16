# SPDX-License-Identifier: GPL-3.0-only

"""Per-contract prompt files and strict JSON Schemas for the provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from data_pipeline.models import DataKind, MeasurementScale
from geoqa_agent.prompts import load_prompt
from geoqa_agent.tool_registry import load_tool_registry
from geoqa_agent.structured_artifacts import (
    ArtifactContract,
    ArtifactRole,
    RoleSettings,
)


@dataclass(frozen=True)
class RoleConfiguration:
    role: ArtifactRole
    prompt_file: str
    prompt_version: str
    schema_name: str
    schema_version: str
    schema: Mapping[str, object]
    settings: RoleSettings

    @property
    def instructions(self) -> str:
        """Load this role's versioned provider instructions."""
        return load_prompt(self.prompt_file)


# Role configurations below pair a versioned prompt with the JSON Schema its
# output must satisfy; the schema is sent to the provider in strict mode.


def _annotation_value_schema(
    *,
    allowed_values: tuple[str, ...] | None = None,
) -> dict[str, object]:
    value_schema: dict[str, object] = {"type": "string", "minLength": 1}
    if allowed_values is not None:
        value_schema["enum"] = list(allowed_values)
    return {
        "type": "object",
        "properties": {
            "value": value_schema,
            "evidence_refs": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
        },
        "required": ["value", "evidence_refs", "confidence"],
        "additionalProperties": False,
    }


_TEXT_ANNOTATION_SCHEMA = _annotation_value_schema()
_MEASUREMENT_SCALE_ANNOTATION_SCHEMA = _annotation_value_schema(
    allowed_values=tuple(scale.value for scale in MeasurementScale)
)
_ATTRIBUTE_ANNOTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "name_en": _TEXT_ANNOTATION_SCHEMA,
        "description_en": _TEXT_ANNOTATION_SCHEMA,
        "semantic_label": _TEXT_ANNOTATION_SCHEMA,
        "measurement_scale": _MEASUREMENT_SCALE_ANNOTATION_SCHEMA,
    },
    "required": [
        "name",
        "name_en",
        "description_en",
        "semantic_label",
        "measurement_scale",
    ],
    "additionalProperties": False,
}

METADATA_ANNOTATION_CONFIGURATION = RoleConfiguration(
    role=ArtifactRole.ANNOTATION,
    prompt_file="metadata_annotation/v10.md",
    prompt_version="metadata-annotation-v10",
    schema_name="metadata_annotation",
    schema_version="metadata-annotation-v3",
    schema={
        "type": "object",
        "properties": {
            "datasets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "dataset_id": {"type": "string", "minLength": 1},
                        "title_en": _TEXT_ANNOTATION_SCHEMA,
                        "description_en": _TEXT_ANNOTATION_SCHEMA,
                    },
                    "required": ["dataset_id", "title_en", "description_en"],
                    "additionalProperties": False,
                },
            },
            "layers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "dataset_id": {"type": "string", "minLength": 1},
                        "layer_id": {"type": "string", "minLength": 1},
                        "name_en": _TEXT_ANNOTATION_SCHEMA,
                        "description_en": _TEXT_ANNOTATION_SCHEMA,
                        "semantic_label": _TEXT_ANNOTATION_SCHEMA,
                        "attributes": {
                            "type": "array",
                            "items": _ATTRIBUTE_ANNOTATION_SCHEMA,
                        },
                    },
                    "required": [
                        "dataset_id",
                        "layer_id",
                        "name_en",
                        "description_en",
                        "semantic_label",
                        "attributes",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["datasets", "layers"],
        "additionalProperties": False,
    },
    # Thought tokens count toward the output budget: annotating the whole
    # catalog in one call spends ~20k on thinking before the ~10k artifact.
    settings=RoleSettings(
        reasoning_effort="medium",
        max_output_tokens=65536,
    ),
)


_NULLABLE_TEXT_SCHEMA = {"type": ["string", "null"]}
_QUESTION_PHRASE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1},
        "normalized_meaning": {"type": "string", "minLength": 1},
        "functional_role": {
            "type": "string",
            "enum": [
                "measure",
                "condition",
                "subcondition",
                "support",
                "spatial_extent",
                "temporal_extent",
            ],
        },
        "referenced_phenomenon": _NULLABLE_TEXT_SCHEMA,
        "referenced_property": _NULLABLE_TEXT_SCHEMA,
        "referenced_relation": _NULLABLE_TEXT_SCHEMA,
        "referenced_place": _NULLABLE_TEXT_SCHEMA,
        "referenced_time": _NULLABLE_TEXT_SCHEMA,
        "quantity": _NULLABLE_TEXT_SCHEMA,
        "unit": _NULLABLE_TEXT_SCHEMA,
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "alternatives": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
    "required": [
        "text",
        "normalized_meaning",
        "functional_role",
        "referenced_phenomenon",
        "referenced_property",
        "referenced_relation",
        "referenced_place",
        "referenced_time",
        "quantity",
        "unit",
        "confidence",
        "alternatives",
    ],
    "additionalProperties": False,
}
_ROLE_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "minLength": 1},
        "semantic_label": {"type": "string", "minLength": 1},
        "identity_fields": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "data_kind": {"enum": [kind.value for kind in DataKind]},
        "geometry_types": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
    },
    "required": [
        "role",
        "semantic_label",
        "identity_fields",
        "data_kind",
        "geometry_types",
    ],
    "additionalProperties": False,
}
_GOAL_SCHEMA = {
    "type": "object",
    "properties": {
        "per": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "maxItems": 2,
            "uniqueItems": True,
        },
        "value_name": {"type": "string", "minLength": 1},
        "value_scale": {
            "enum": [scale.value for scale in MeasurementScale],
        },
        "aggregation": {
            "enum": ["count", "sum", "mean", "min", "max", "density", "distance", "other"],
        },
        "value_attribute": {"type": ["string", "null"]},
        "selection": {"type": "boolean"},
    },
    "required": [
        "per",
        "value_name",
        "value_scale",
        "aggregation",
        "value_attribute",
        "selection",
    ],
    "additionalProperties": False,
}
_QUANTITY_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "value": {"type": "number"},
        "unit": {"type": ["string", "null"]},
    },
    "required": ["name", "value", "unit"],
    "additionalProperties": False,
}
_PERIOD_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "start": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
        "end": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
    },
    "required": ["name", "start", "end"],
    "additionalProperties": False,
}
_TASK_SPECIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "required_output": {"type": "string", "minLength": 1},
        "roles": {
            "type": "array",
            "items": _ROLE_SCHEMA,
            "minItems": 1,
        },
        "goal": _GOAL_SCHEMA,
        "quantities": {
            "type": "array",
            "items": _QUANTITY_SCHEMA,
        },
        "periods": {
            "type": "array",
            "items": _PERIOD_SCHEMA,
        },
        "constraints": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "spatial_extent": {"type": "string", "minLength": 1},
        "temporal_mode": {
            "enum": ["current_snapshot", "explicit"],
        },
        "temporal_meaning": {"type": "string", "minLength": 1},
        "target_transformation": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        },
    },
    "required": [
        "required_output",
        "roles",
        "goal",
        "quantities",
        "periods",
        "constraints",
        "spatial_extent",
        "temporal_mode",
        "temporal_meaning",
        "target_transformation",
    ],
    "additionalProperties": False,
}

QUESTION_INTERPRETATION_CONFIGURATION = RoleConfiguration(
    role=ArtifactRole.PLANNING,
    prompt_file="question_interpretation/v19.md",
    prompt_version="question-interpretation-v19",
    schema_name="question_interpretation",
    schema_version="question-interpretation-v11",
    schema={
        "type": "object",
        "properties": {
            "question_phrases": {
                "type": "array",
                "items": _QUESTION_PHRASE_SCHEMA,
                "minItems": 1,
            },
            "task_specification": _TASK_SPECIFICATION_SCHEMA,
            "assumptions": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            },
            "unresolved_ambiguities": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            },
        },
        "required": [
            "question_phrases",
            "task_specification",
            "assumptions",
            "unresolved_ambiguities",
        ],
        "additionalProperties": False,
    },
    settings=RoleSettings(
        reasoning_effort="medium",
        max_output_tokens=32768,
    ),
)


# The planner can only name operations that exist: the schema enumerates
# the registry's ids, so a misspelled id is impossible rather than a
# rejected draft.
ALGORITHM_IDS: tuple[str, ...] = load_tool_registry().executable_algorithm_ids

_WORKFLOW_PARAMETER_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "source": {
            "type": "string",
            "enum": ["ref", "literal", "template"],
        },
        "value": {
            "type": ["string", "number", "boolean", "null"],
        },
    },
    "required": ["name", "source", "value"],
    "additionalProperties": False,
}
_WORKFLOW_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "ref": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "enum": ["sink", "result"]},
    },
    "required": ["name", "ref", "kind"],
    "additionalProperties": False,
}

WORKFLOW_PLANNING_CONFIGURATION = RoleConfiguration(
    role=ArtifactRole.PLANNING,
    prompt_file="workflow_planning/v18.md",
    prompt_version="workflow-planning-v18",
    schema_name="workflow_planning",
    schema_version="workflow-planning-v5",
    schema={
        "type": "object",
        "properties": {
            "concrete_workflow": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "step_id": {"type": "string", "minLength": 1},
                                "algorithm_id": {
                                    "type": "string",
                                    "enum": list(ALGORITHM_IDS),
                                },
                                "parameters": {
                                    "type": "array",
                                    "items": _WORKFLOW_PARAMETER_SCHEMA,
                                },
                                "outputs": {
                                    "type": "array",
                                    "items": _WORKFLOW_OUTPUT_SCHEMA,
                                },
                            },
                            "required": [
                                "step_id",
                                "algorithm_id",
                                "parameters",
                                "outputs",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "final_output_ref": {"type": "string", "minLength": 1},
                    "result_table_ref": {"type": "string", "minLength": 1},
                    "diagnostic_refs": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                },
                "required": [
                    "steps",
                    "final_output_ref",
                    "result_table_ref",
                    "diagnostic_refs",
                ],
                "additionalProperties": False,
            },
        },
        "required": ["concrete_workflow"],
        "additionalProperties": False,
    },
    settings=RoleSettings(
        reasoning_effort="medium",
        max_output_tokens=32768,
    ),
)

CONFIGURATIONS = {
    ArtifactContract.METADATA_ANNOTATION: METADATA_ANNOTATION_CONFIGURATION,
    ArtifactContract.QUESTION_INTERPRETATION: QUESTION_INTERPRETATION_CONFIGURATION,
    ArtifactContract.WORKFLOW_PLANNING: WORKFLOW_PLANNING_CONFIGURATION,
}
