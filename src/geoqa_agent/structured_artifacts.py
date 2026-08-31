# SPDX-License-Identifier: GPL-3.0-only

"""Provider-neutral structured-artifact generation contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Mapping, Protocol, cast

import httpx

from geoqa_agent.prompts import load_prompt
from geoqa_agent.semantic_types import AttributeCCDMeaning, LayerCCDMeaning


# The MVP pins one exact dated model snapshot; provenance records it and the
# client refuses responses served by any other model.
OPENAI_PROVIDER = "openai"
MVP_MODEL = "gpt-5.4-mini-2026-03-17"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


class ArtifactRole(StrEnum):
    """Independently configured model roles in the MVP."""

    ANNOTATION = "annotation"
    PLANNING = "planning"


class ArtifactContract(StrEnum):
    """Application-selected structured schema within a model role."""

    METADATA_ANNOTATION = "metadata_annotation"
    QUESTION_INTERPRETATION = "question_interpretation"
    WORKFLOW_PLANNING = "workflow_planning"


@dataclass(frozen=True)
class ArtifactRequest:
    """Caller-controlled input, deliberately excluding provider settings."""

    contract: ArtifactContract
    input_text: str


@dataclass(frozen=True)
class RoleSettings:
    """Owner-controlled generation settings recorded with every artifact."""

    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"]
    max_output_tokens: int


@dataclass(frozen=True)
class ArtifactProvenance:
    """Reproducibility metadata for one generated artifact."""

    provider: str
    model: str
    role: ArtifactRole
    settings: RoleSettings
    prompt_version: str
    schema_version: str


@dataclass(frozen=True)
class StructuredArtifact:
    """Schema-validated data and its generation provenance."""

    data: Mapping[str, object]
    provenance: ArtifactProvenance


class StructuredArtifactClient(Protocol):
    """Provider-neutral boundary used by Annotation and Planning."""

    def generate(self, request: ArtifactRequest) -> StructuredArtifact:
        """Generate one artifact using the configured role contract."""


@dataclass(frozen=True)
class _RoleConfiguration:
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
_LAYER_CCD_ANNOTATION_SCHEMA = _annotation_value_schema(
    allowed_values=tuple(meaning.value for meaning in LayerCCDMeaning)
)
_ATTRIBUTE_CCD_ANNOTATION_SCHEMA = _annotation_value_schema(
    allowed_values=tuple(meaning.value for meaning in AttributeCCDMeaning)
)
_ATTRIBUTE_ANNOTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "name_en": _TEXT_ANNOTATION_SCHEMA,
        "description_en": _TEXT_ANNOTATION_SCHEMA,
        "semantic_label": _TEXT_ANNOTATION_SCHEMA,
        "ccd_meaning": _ATTRIBUTE_CCD_ANNOTATION_SCHEMA,
    },
    "required": [
        "name",
        "name_en",
        "description_en",
        "semantic_label",
        "ccd_meaning",
    ],
    "additionalProperties": False,
}

METADATA_ANNOTATION_CONFIGURATION = _RoleConfiguration(
    role=ArtifactRole.ANNOTATION,
    prompt_file="metadata_annotation/v7.md",
    prompt_version="metadata-annotation-v7",
    schema_name="metadata_annotation",
    schema_version="metadata-annotation-v1",
    schema={
        "type": "object",
        "properties": {
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
                        "ccd_meaning": _LAYER_CCD_ANNOTATION_SCHEMA,
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
                        "ccd_meaning",
                        "attributes",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["layers"],
        "additionalProperties": False,
    },
    settings=RoleSettings(
        reasoning_effort="low",
        max_output_tokens=8192,
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
        "candidate_ccd_meaning": _NULLABLE_TEXT_SCHEMA,
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
        "candidate_ccd_meaning",
        "confidence",
        "alternatives",
    ],
    "additionalProperties": False,
}
_COUNT_TASK_SPECIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "required_output": {"type": "string", "minLength": 1},
        "support": {
            "type": "object",
            "properties": {
                "semantic_label": {"type": "string", "minLength": 1},
                "source_state": {"type": "string", "minLength": 1},
                "identity_fields": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "uniqueItems": True,
                },
            },
            "required": [
                "semantic_label",
                "source_state",
                "identity_fields",
            ],
            "additionalProperties": False,
        },
        "counted_objects": {
            "type": "object",
            "properties": {
                "semantic_label": {"type": "string", "minLength": 1},
                "distinct_by": {"type": "string", "minLength": 1},
            },
            "required": ["semantic_label", "distinct_by"],
            "additionalProperties": False,
        },
        "constraints": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "uniqueItems": True,
        },
        "spatial_extent": {"type": "string", "minLength": 1},
        "temporal_mode": {
            "type": "string",
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
        "support",
        "counted_objects",
        "constraints",
        "spatial_extent",
        "temporal_mode",
        "temporal_meaning",
        "target_transformation",
    ],
    "additionalProperties": False,
}
_NEAREST_POINT_ROLE_SCHEMA = {
    "type": "object",
    "properties": {
        "semantic_label": {"type": "string", "minLength": 1},
        "identity_fields": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "uniqueItems": True,
        },
    },
    "required": ["semantic_label", "identity_fields"],
    "additionalProperties": False,
}
_NEAREST_TASK_SPECIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "required_output": {"type": "string", "minLength": 1},
        "source_points": _NEAREST_POINT_ROLE_SCHEMA,
        "target_points": _NEAREST_POINT_ROLE_SCHEMA,
        "distance": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["planar_euclidean", "network"],
                },
                "crs": {"type": "string", "minLength": 1},
                "unit": {"type": "string", "minLength": 1},
                "nearest_targets": {"type": "integer", "minimum": 1},
                "maximum_distance_m": {"type": ["number", "null"]},
                "retain_all_ties": {"type": "boolean"},
            },
            "required": [
                "method",
                "crs",
                "unit",
                "nearest_targets",
                "maximum_distance_m",
                "retain_all_ties",
            ],
            "additionalProperties": False,
        },
        "constraints": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "uniqueItems": True,
        },
        "spatial_extent": {"type": "string", "minLength": 1},
        "temporal_mode": {
            "type": "string",
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
        "source_points",
        "target_points",
        "distance",
        "constraints",
        "spatial_extent",
        "temporal_mode",
        "temporal_meaning",
        "target_transformation",
    ],
    "additionalProperties": False,
}
_TASK_SPECIFICATION_SCHEMA = {
    "oneOf": [
        _COUNT_TASK_SPECIFICATION_SCHEMA,
        _NEAREST_TASK_SPECIFICATION_SCHEMA,
    ]
}
_ROLE_REQUIREMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {
            "type": "string",
            "enum": [
                "supports",
                "counted_objects",
                "source_points",
                "target_points",
            ],
        },
        "semantic_label": {"type": "string", "minLength": 1},
        "ccd_meaning": {
            "type": "string",
            "enum": [meaning.value for meaning in LayerCCDMeaning],
        },
        "geometry_types": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "uniqueItems": True,
        },
        "source_identity_fields": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "uniqueItems": True,
        },
    },
    "required": [
        "role",
        "semantic_label",
        "ccd_meaning",
        "geometry_types",
        "source_identity_fields",
    ],
    "additionalProperties": False,
}

QUESTION_INTERPRETATION_CONFIGURATION = _RoleConfiguration(
    role=ArtifactRole.PLANNING,
    prompt_file="question_interpretation/v13.md",
    prompt_version="question-interpretation-v13",
    schema_name="question_interpretation",
    schema_version="question-interpretation-v5",
    schema={
        "type": "object",
        "properties": {
            "question_phrases": {
                "type": "array",
                "items": _QUESTION_PHRASE_SCHEMA,
                "minItems": 1,
            },
            "task_specification": _TASK_SPECIFICATION_SCHEMA,
            "role_requirements": {
                "type": "array",
                "items": _ROLE_REQUIREMENT_SCHEMA,
                "minItems": 1,
                "maxItems": 2,
            },
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
            "role_requirements",
            "assumptions",
            "unresolved_ambiguities",
        ],
        "additionalProperties": False,
    },
    settings=RoleSettings(
        reasoning_effort="low",
        max_output_tokens=8192,
    ),
)


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

WORKFLOW_PLANNING_CONFIGURATION = _RoleConfiguration(
    role=ArtifactRole.PLANNING,
    prompt_file="workflow_planning/v10.md",
    prompt_version="workflow-planning-v10",
    schema_name="workflow_planning",
    schema_version="workflow-planning-v2",
    schema={
        "type": "object",
        "properties": {
            "abstract_workflow": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "step_id": {"type": "string", "minLength": 1},
                                "abstraction_id": {
                                    "type": "string",
                                    "minLength": 1,
                                },
                                "input_refs": {
                                    "type": "array",
                                    "items": {"type": "string", "minLength": 1},
                                    "minItems": 1,
                                    "uniqueItems": True,
                                },
                                "output_ref": {
                                    "type": "string",
                                    "minLength": 1,
                                },
                            },
                            "required": [
                                "step_id",
                                "abstraction_id",
                                "input_refs",
                                "output_ref",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "final_output_ref": {"type": "string", "minLength": 1},
                },
                "required": ["steps", "final_output_ref"],
                "additionalProperties": False,
            },
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
                                "abstract_step_id": {
                                    "type": "string",
                                    "minLength": 1,
                                },
                                "algorithm_id": {
                                    "type": "string",
                                    "minLength": 1,
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
                                "abstract_step_id",
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
        "required": ["abstract_workflow", "concrete_workflow"],
        "additionalProperties": False,
    },
    settings=RoleSettings(
        reasoning_effort="low",
        max_output_tokens=8192,
    ),
)

class StructuredArtifactError(RuntimeError):
    """Base error for failures at the provider boundary."""


class SchemaConformanceError(StructuredArtifactError):
    """Raised when provider output does not match the configured schema."""


class ProviderResponseError(StructuredArtifactError):
    """Raised when the provider response cannot identify a valid artifact."""


def _provider_schema(schema: object) -> object:
    """OpenAI strict structured outputs reject "uniqueItems"."""
    if isinstance(schema, dict):
        alternatives = schema.get("oneOf")
        if isinstance(alternatives, list):
            return _provider_object_union(alternatives)
        return {
            key: _provider_schema(value)
            for key, value in schema.items()
            if key != "uniqueItems"
        }
    if isinstance(schema, list):
        return [_provider_schema(item) for item in schema]
    return schema


def _provider_object_union(alternatives: list[object]) -> dict[str, object]:
    """Merge object alternatives into one strict provider-facing shape.

    Strict Structured Outputs do not accept ``oneOf`` and require every
    declared property to be required. Fields owned by only one alternative
    are therefore nullable in the provider contract. They are removed before
    the unchanged application schema validates the response.
    """

    branches = [
        cast(Mapping[str, object], _provider_schema(alternative))
        for alternative in alternatives
    ]
    if not branches or any(branch.get("type") != "object" for branch in branches):
        raise ValueError("Provider oneOf alternatives must all be objects.")
    property_sets = [
        cast(Mapping[str, object], branch.get("properties", {}))
        for branch in branches
    ]
    all_names = set().union(*(properties.keys() for properties in property_sets))
    merged: dict[str, object] = {}
    for name in sorted(all_names):
        candidates = [properties[name] for properties in property_sets if name in properties]
        first = candidates[0]
        if any(candidate != first for candidate in candidates[1:]):
            raise ValueError(
                f"Provider oneOf property {name!r} has incompatible schemas."
            )
        merged[name] = (
            first if len(candidates) == len(branches) else _nullable_schema(first)
        )
    return {
        "type": "object",
        "properties": merged,
        "required": sorted(all_names),
        "additionalProperties": False,
    }


def _nullable_schema(schema: object) -> object:
    if not isinstance(schema, dict):
        raise ValueError("Provider union properties must use object schemas.")
    nullable = dict(schema)
    value_type = nullable.get("type")
    if isinstance(value_type, str):
        nullable["type"] = [value_type, "null"]
    elif isinstance(value_type, list) and "null" not in value_type:
        nullable["type"] = [*value_type, "null"]
    else:
        raise ValueError("Provider union property has no supported type.")
    return nullable


def _normalize_provider_data(
    data: object,
    configuration: _RoleConfiguration,
) -> object:
    if configuration is not QUESTION_INTERPRETATION_CONFIGURATION:
        return data
    if not isinstance(data, dict):
        return data
    task = data.get("task_specification")
    if not isinstance(task, dict):
        return data
    for key in (
        "support",
        "counted_objects",
        "source_points",
        "target_points",
        "distance",
    ):
        if task.get(key, object()) is None:
            task.pop(key)
    return data


class OpenAIResponsesClient:
    """OpenAI Responses adapter behind the provider-neutral contract."""

    def __init__(
        self,
        *,
        api_key: str,
        http_client: httpx.Client | None = None,
        model: str = MVP_MODEL,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._http_client = http_client or httpx.Client(timeout=60.0)
        self._owns_http_client = http_client is None
        self._model = model

    def close(self) -> None:
        """Close an internally managed HTTP client."""

        if self._owns_http_client:
            self._http_client.close()

    def __enter__(self) -> OpenAIResponsesClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def generate(self, request: ArtifactRequest) -> StructuredArtifact:
        configuration = self._configuration(request)
        settings = configuration.settings
        try:
            response = self._http_client.post(
                OPENAI_RESPONSES_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "instructions": configuration.instructions,
                    "input": request.input_text,
                    "reasoning": {"effort": settings.reasoning_effort},
                    "max_output_tokens": settings.max_output_tokens,
                    "store": False,
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": configuration.schema_name,
                            "strict": True,
                            "schema": _provider_schema(configuration.schema),
                        }
                    },
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderResponseError(
                "Structured-artifact provider request failed."
            ) from error
        # Reject silent provider-side model substitution: provenance must
        # record the exact model that actually generated the artifact.
        response_model = payload.get("model")
        if response_model != self._model:
            raise ProviderResponseError(
                "Provider response model does not match the configured exact "
                f"model: expected {self._model!r}, got {response_model!r}."
            )

        data = _normalize_provider_data(self._parse_output(payload), configuration)
        if not isinstance(data, dict):
            raise SchemaConformanceError(
                f"{configuration.schema_name} output must be a JSON object."
            )
        return StructuredArtifact(
            data=data,
            provenance=ArtifactProvenance(
                provider=OPENAI_PROVIDER,
                model=response_model,
                role=configuration.role,
                settings=settings,
                prompt_version=configuration.prompt_version,
                schema_version=configuration.schema_version,
            ),
        )

    @staticmethod
    def _configuration(request: ArtifactRequest) -> _RoleConfiguration:
        return _CONFIGURATIONS[request.contract]

    @staticmethod
    def _parse_output(payload: object) -> object:
        """Extract the first output_text part of a Responses API payload."""

        if not isinstance(payload, dict):
            raise ProviderResponseError("Provider response must be an object.")
        output = payload.get("output")
        if not isinstance(output, list):
            raise ProviderResponseError("Provider response has no output list.")
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    try:
                        return json.loads(part["text"])
                    except json.JSONDecodeError as error:
                        raise SchemaConformanceError(
                            "Provider output is not valid JSON."
                        ) from error
        raise ProviderResponseError(
            "Provider response contains no structured output text."
        )


_CONFIGURATIONS = {
    ArtifactContract.METADATA_ANNOTATION: METADATA_ANNOTATION_CONFIGURATION,
    ArtifactContract.QUESTION_INTERPRETATION: QUESTION_INTERPRETATION_CONFIGURATION,
    ArtifactContract.WORKFLOW_PLANNING: WORKFLOW_PLANNING_CONFIGURATION,
}
