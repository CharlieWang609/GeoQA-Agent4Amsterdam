# SPDX-License-Identifier: GPL-3.0-only

"""Accepted-workflow case base: retain, match, and re-instantiate.

The retention gate is the human accept decision (the oracle plays that
role in evaluation). Cases are indexed by a structural task signature —
family + per-role CCD types + cutoff shape — computed after interpretation,
so retrieval never depends on question wording. An exact hit (same bound
layers, same cutoff value) re-instantiates the stored workflow without any
model call; the full validator still judges the result. Near hits become
worked examples for the free-composition tier.

Cases persist indefinitely (no expiry): they are the system's earned
knowledge, unlike the 7-day session artifacts. A case whose replay is
rejected by the reviewer is removed.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, TypeAdapter

from data_pipeline.catalog import CatalogReader
from data_pipeline.models import CatalogVersion
from data_pipeline.serialization import canonical_json, sha256
from data_pipeline.storage import ObjectStore
from geoqa_agent.governance import (
    CapabilityTaskSpecification,
    NearestTaskSpecification,
    task_family,
)
from geoqa_agent.question_interpretation import (
    DataBinding,
    SupportedInterpretation,
)
from geoqa_agent.structured_artifacts import (
    ArtifactProvenance,
    ArtifactRole,
    RoleSettings,
)
from geoqa_agent.workflow_planning import (
    AbstractWorkflow,
    AbstractWorkflowArtifact,
    ConcreteWorkflow,
    ConcreteWorkflowArtifact,
    DataBindingsArtifact,
    TaskSpecificationArtifact,
    WorkflowDraft,
    WorkflowDraftRepository,
    bound_ccd_types,
    sealed_draft,
)

CASE_PREFIX = "case-base"

_ABSTRACT: TypeAdapter[AbstractWorkflow] = TypeAdapter(AbstractWorkflow)
_CONCRETE: TypeAdapter[ConcreteWorkflow] = TypeAdapter(ConcreteWorkflow)


class WorkflowCase(BaseModel):
    """One accepted workflow, stored under its structural task signature."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    signature: str
    family: str
    question: str
    role_layers: Mapping[str, tuple[str, str]]
    maximum_distance_m: float | None
    abstract_workflow: Mapping[str, object]
    concrete_workflow: Mapping[str, object]
    planning_source: Literal["composition", "enumeration"]
    accepted_at: datetime


class TaskStructure(BaseModel):
    """The retrieval key of one grounded task."""

    model_config = ConfigDict(frozen=True)

    family: str
    signature: str
    role_layers: Mapping[str, tuple[str, str]]
    maximum_distance_m: float | None


def task_structure(
    task: CapabilityTaskSpecification,
    bindings: tuple[DataBinding, ...],
    catalog: CatalogVersion,
) -> TaskStructure:
    """Compute the structural signature of a grounded task."""

    layers = {
        (layer.dataset_id, layer.layer_id): layer for layer in catalog.layers
    }
    family = task_family(task)
    role_types = {
        binding.role.value: sorted(
            str(item).rsplit("#", 1)[-1]
            for item in bound_ccd_types(
                layers[(binding.dataset_id, binding.layer_id)]
            )
        )
        for binding in bindings
    }
    cutoff = (
        task.distance.maximum_distance_m
        if isinstance(task, NearestTaskSpecification)
        else None
    )
    signature = sha256(
        canonical_json(
            {
                "family": family,
                "role_types": role_types,
                "has_cutoff": cutoff is not None,
            }
        )
    )[:16]
    return TaskStructure(
        family=family,
        signature=signature,
        role_layers={
            binding.role.value: (binding.dataset_id, binding.layer_id)
            for binding in bindings
        },
        maximum_distance_m=cutoff,
    )


class CaseBase:
    """Signature-indexed store of accepted workflows."""

    def __init__(self, storage: ObjectStore) -> None:
        self._storage = storage

    def retain(
        self,
        *,
        question: str,
        draft: WorkflowDraft,
        catalog: CatalogVersion,
        planning_source: Literal["composition", "enumeration"],
        accepted_at: datetime,
    ) -> str:
        """Store one accepted draft as a case; return its storage key."""

        structure = task_structure(
            draft.task_specification.value,
            draft.data_bindings.value,
            catalog,
        )
        provisional = WorkflowCase(
            case_id="",
            signature=structure.signature,
            family=structure.family,
            question=question,
            role_layers=structure.role_layers,
            maximum_distance_m=structure.maximum_distance_m,
            abstract_workflow=asdict(draft.abstract_workflow.value),
            concrete_workflow=asdict(draft.concrete_workflow.value),
            planning_source=planning_source,
            accepted_at=accepted_at,
        )
        case = provisional.model_copy(
            update={
                "case_id": sha256(
                    canonical_json(
                        provisional.model_dump(mode="json", exclude={"case_id"})
                    )
                )
            }
        )
        key = _case_key(case.signature, case.case_id)
        if self._storage.read(key) is None:
            self._storage.put_immutable(
                key, case.model_dump_json().encode()
            )
        return key

    def exact_match(
        self,
        structure: TaskStructure,
    ) -> tuple[str, WorkflowCase] | None:
        """The newest case whose layers and cutoff match exactly."""

        for key, case in self._cases(structure.signature):
            if (
                dict(case.role_layers) == dict(structure.role_layers)
                and case.maximum_distance_m == structure.maximum_distance_m
            ):
                return key, case
        return None

    def near_examples(
        self,
        structure: TaskStructure,
        *,
        limit: int = 2,
    ) -> tuple[WorkflowCase, ...]:
        """Same-signature cases over other layers or cutoffs, newest first."""

        examples = [
            case
            for _, case in self._cases(structure.signature)
            if dict(case.role_layers) != dict(structure.role_layers)
            or case.maximum_distance_m != structure.maximum_distance_m
        ]
        return tuple(examples[:limit])

    def remove(self, key: str) -> None:
        self._storage.delete(key)

    def _cases(self, signature: str) -> list[tuple[str, WorkflowCase]]:
        cases = []
        for key in self._storage.list_keys(f"{CASE_PREFIX}/{signature}/"):
            stored = self._storage.read(key)
            if stored is not None:
                cases.append((key, WorkflowCase.model_validate_json(stored.data)))
        cases.sort(key=lambda item: item[1].accepted_at, reverse=True)
        return cases


def case_example_documents(
    examples: tuple[WorkflowCase, ...],
) -> tuple[Mapping[str, object], ...] | None:
    """Render near-hit cases for the planning input; None when empty."""

    if not examples:
        return None
    return tuple(
        {
            "question": case.question,
            "abstract_workflow": case.abstract_workflow,
            "concrete_workflow": case.concrete_workflow,
        }
        for case in examples
    )


def instantiate_draft(
    case: WorkflowCase,
    interpretation: SupportedInterpretation,
    *,
    tool_registry_version: str,
    storage: ObjectStore,
) -> WorkflowDraft:
    """Re-instantiate a case for a new question without a model call.

    The current interpretation supplies fresh pinned bindings; the case
    supplies the workflows. The result is a normal draft: content-hashed,
    persisted, and judged by the full validator like any proposal.
    """

    catalog = CatalogReader(storage).get(interpretation.catalog_version)
    provenance = ArtifactProvenance(
        provider="case-base",
        model=case.case_id,
        role=ArtifactRole.PLANNING,
        settings=RoleSettings(reasoning_effort="none", max_output_tokens=0),
        prompt_version="case-base",
        schema_version="case-base",
    )
    provisional = WorkflowDraft(
        draft_id="",
        catalog_version=catalog.version,
        tool_registry_version=tool_registry_version,
        provenance=provenance,
        task_specification=TaskSpecificationArtifact(
            value=interpretation.task_specification,
            provenance=interpretation.provenance,
        ),
        data_bindings=DataBindingsArtifact(
            value=interpretation.bindings,
            provenance=interpretation.provenance,
        ),
        abstract_workflow=AbstractWorkflowArtifact(
            value=_ABSTRACT.validate_python(case.abstract_workflow),
            provenance=provenance,
        ),
        concrete_workflow=ConcreteWorkflowArtifact(
            value=_CONCRETE.validate_python(case.concrete_workflow),
            provenance=provenance,
        ),
    )
    draft = sealed_draft(provisional)
    WorkflowDraftRepository(storage).save(draft)
    return draft


def _case_key(signature: str, case_id: str) -> str:
    return f"{CASE_PREFIX}/{signature}/{case_id}.json"
