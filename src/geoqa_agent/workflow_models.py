# SPDX-License-Identifier: GPL-3.0-only

"""Workflow draft, validation-result models and their persistence."""

from __future__ import annotations


from dataclasses import (
    dataclass,
    replace,
)
from datetime import datetime
from enum import StrEnum

from pydantic import TypeAdapter

from data_pipeline.serialization import sha256
from data_pipeline.storage import ObjectStore
from geoqa_agent.governance import TaskSpecification
from geoqa_agent.question_interpretation import DataBinding
from geoqa_agent.structured_artifacts import ArtifactProvenance
from geoqa_agent.tool_registry import (
    OutputBinding,
    ParameterBinding,
)


class ArtifactKind(StrEnum):
    """Persisted planning artifacts kept distinct for review."""

    TASK_SPECIFICATION = "task-specification"
    DATA_BINDINGS = "data-bindings"
    CONCRETE_WORKFLOW = "concrete-workflow"


@dataclass(frozen=True)
class ConcreteWorkflowStep:
    """One proposed allow-listed operation invocation."""

    step_id: str
    algorithm_id: str
    parameters: tuple[ParameterBinding, ...]
    outputs: tuple[OutputBinding, ...]


@dataclass(frozen=True)
class ConcreteWorkflow:
    """A complete proposed executable dataflow."""

    steps: tuple[ConcreteWorkflowStep, ...]
    final_output_ref: str
    result_table_ref: str
    diagnostic_refs: tuple[str, ...]


# Each *Artifact below pairs one planning payload with the LLM provenance
# that produced it, so interpretation and planning stay separately auditable.
@dataclass(frozen=True)
class TaskSpecificationArtifact:
    value: TaskSpecification
    provenance: ArtifactProvenance


@dataclass(frozen=True)
class DataBindingsArtifact:
    value: tuple[DataBinding, ...]
    provenance: ArtifactProvenance


@dataclass(frozen=True)
class ConcreteWorkflowArtifact:
    value: ConcreteWorkflow
    provenance: ArtifactProvenance


@dataclass(frozen=True)
class WorkflowDraft:
    """Immutable review draft; validation alone may mark it executable."""

    draft_id: str
    catalog_version: str
    tool_registry_version: str
    provenance: ArtifactProvenance
    task_specification: TaskSpecificationArtifact
    data_bindings: DataBindingsArtifact
    concrete_workflow: ConcreteWorkflowArtifact


class ValidationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"


VALIDATION_SCHEMA_VERSION = "workflow-validation-v2"


class DiagnosticCode(StrEnum):
    STRUCTURAL_ERROR = "structural-error"
    INVENTED_DATA = "invented-data"
    UNRESOLVED_ANNOTATION = "unresolved-annotation"
    INCOMPATIBLE_TYPE = "incompatible-type"
    MISSING_PARAMETER = "missing-parameter"
    INVALID_PARAMETER = "invalid-parameter"
    CRS_CONFLICT = "crs-conflict"
    COVERAGE_CONFLICT = "coverage-conflict"
    UNAVAILABLE_ALGORITHM = "unavailable-algorithm"
    DISCONNECTED_REFERENCE = "disconnected-reference"


@dataclass(frozen=True)
class ValidationDiagnostic:
    """Stable machine-readable validation failure."""

    code: DiagnosticCode
    message: str
    artifact: ArtifactKind
    step_id: str | None = None
    ref: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    """Deterministic gate result: any diagnostic blocks execution."""

    validation_id: str
    draft_id: str
    status: ValidationStatus
    diagnostics: tuple[ValidationDiagnostic, ...]
    schema_version: str = VALIDATION_SCHEMA_VERSION


DRAFT_ADAPTER: TypeAdapter[WorkflowDraft] = TypeAdapter(WorkflowDraft)
VALIDATION_ADAPTER: TypeAdapter[ValidationResult] = TypeAdapter(ValidationResult)


def sealed_draft(provisional: WorkflowDraft) -> WorkflowDraft:
    """Stamp a complete draft with its content-addressed identity."""

    return replace(
        provisional,
        draft_id=f"sha256:{sha256(DRAFT_ADAPTER.dump_json(provisional))}",
    )


# Boundary parser for the planner's untrusted artifact JSON.
CONCRETE_ADAPTER: TypeAdapter[ConcreteWorkflow] = TypeAdapter(ConcreteWorkflow)


class WorkflowDraftRepository:
    """Persist each draft and its validation as one JSON blob apiece."""

    def __init__(self, storage: ObjectStore) -> None:
        self._storage = storage

    def save(self, draft: WorkflowDraft) -> None:
        self._storage.put_immutable(
            self._draft_key(draft.draft_id),
            DRAFT_ADAPTER.dump_json(draft),
        )

    def get(self, draft_id: str) -> WorkflowDraft:
        stored = self._storage.read(self._draft_key(draft_id))
        if stored is None:
            raise LookupError(f"Workflow draft does not exist: {draft_id}")
        return DRAFT_ADAPTER.validate_json(stored.data)

    def retain_until(self, draft_id: str, expires_at: datetime) -> None:
        """Keep the draft and its validation through a session deadline."""

        self._storage.set_expiry(self._draft_key(draft_id), expires_at)
        self._storage.set_expiry(self._validation_key(draft_id), expires_at)

    def save_validation(self, result: ValidationResult) -> None:
        self._storage.put_immutable(
            self._validation_key(result.draft_id),
            VALIDATION_ADAPTER.dump_json(result),
        )

    def get_validation(self, draft_id: str) -> ValidationResult:
        stored = self._storage.read(self._validation_key(draft_id))
        if stored is None:
            raise LookupError(
                f"Workflow validation does not exist: {draft_id}"
            )
        return VALIDATION_ADAPTER.validate_json(stored.data)

    @staticmethod
    def _draft_key(draft_id: str) -> str:
        return f"workflow-drafts/{draft_id}.json"

    @staticmethod
    def _validation_key(draft_id: str) -> str:
        return f"workflow-drafts/{draft_id}.validation.json"
