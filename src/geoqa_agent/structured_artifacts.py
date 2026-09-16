# SPDX-License-Identifier: GPL-3.0-only

"""Provider-neutral structured-artifact generation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Mapping, Protocol


# Every model is pinned by its exact id; provenance records it and the
# clients refuse responses served by any other model.
OPENAI_PROVIDER = "openai"
GOOGLE_PROVIDER = "google"
DEEPSEEK_PROVIDER = "deepseek"
DEFAULT_MODEL = "gemini-3.8-flash"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"


@dataclass(frozen=True)
class ModelChoice:
    """One model a user may pick for a question session."""

    model: str
    label: str
    provider: str


# The planning models offered in the sandbox and the benchmark, default
# first, and the model that annotates catalog metadata.
MODEL_CHOICES: tuple[ModelChoice, ...] = (
    ModelChoice(DEFAULT_MODEL, "Gemini 3.8 Flash", GOOGLE_PROVIDER),
    ModelChoice("gpt-5.6-luna", "GPT-5.6 Luna", OPENAI_PROVIDER),
    ModelChoice("deepseek-flash", "DeepSeek V4.1 Flash", DEEPSEEK_PROVIDER),
)
ANNOTATION_MODEL = DEFAULT_MODEL


def model_choice(model: str) -> ModelChoice:
    for choice in MODEL_CHOICES:
        if choice.model == model:
            return choice
    raise KeyError(f"Unknown model: {model}")


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


class StructuredArtifactError(RuntimeError):
    """Base error for failures at the provider boundary."""


class SchemaConformanceError(StructuredArtifactError):
    """Raised when provider output does not match the configured schema."""


class ProviderResponseError(StructuredArtifactError):
    """Raised when the provider response cannot identify a valid artifact."""
