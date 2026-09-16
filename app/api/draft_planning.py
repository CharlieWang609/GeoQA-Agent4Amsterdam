# SPDX-License-Identifier: GPL-3.0-only

"""Question to validated draft: interpretation, retrieval, composition.

An exact case-base hit replays an accepted workflow without a model call;
otherwise the planning model composes one and repairs it from validator
diagnostics within a bounded budget. The serving path and the benchmark
harness both drive this class, so they stay identical by construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime

from app.api.session_models import DraftTrigger, QuestionSession, ResultDecisionKind, SessionDraftVersion
from app.api.workflow_records import utc
from data_pipeline.catalog import CatalogReader
from data_pipeline.models import CatalogVersion
from data_pipeline.serialization import canonical_json, sha256
from data_pipeline.storage import ObjectStore
from geoqa_agent.case_base import (
    CaseBase,
    case_example_documents,
    instantiate_draft,
    task_structure,
)
from geoqa_agent.question_interpretation import (
    InterpretationResult,
    QuestionInterpretationAndMatchingService,
    SupportedInterpretation,
    UnsupportedInterpretation,
    interpretation_diagnostic_codes,
    interpretation_repair_context_document,
    is_repairable_interpretation_failure,
)
from geoqa_agent.structured_artifacts import StructuredArtifactClient
from geoqa_agent.tool_registry import ToolRegistry
from geoqa_agent.workflow_models import (
    ValidationResult,
    ValidationStatus,
    WorkflowDraft,
    WorkflowDraftRepository,
)
from geoqa_agent.workflow_planning import (
    WorkflowPlanningService,
    planning_repair_context_document,
)
from geoqa_agent.workflow_validation import WorkflowValidator


# The initial interpretation plus up to two diagnostic-guided repairs.
MAX_INTERPRETATION_ATTEMPTS = 3
# The initial proposal plus diagnostic-guided repairs; composition
# converges within two or three attempts or not at all.
MAX_PLANNING_ATTEMPTS = 5


@dataclass(frozen=True)
class InterpretationOutcome:
    interpretation: InterpretationResult
    attempt_count: int
    failed_attempts: tuple[Mapping[str, object], ...]

    @property
    def repair_document(self) -> dict[str, object] | None:
        if not self.failed_attempts:
            return None
        return {
            "attempt_count": self.attempt_count,
            "failed_attempts": list(self.failed_attempts),
        }


@dataclass(frozen=True)
class PlanningOutcome:
    draft: WorkflowDraft
    validation: ValidationResult
    attempt_count: int
    failed_attempts: tuple[Mapping[str, object], ...]

    @property
    def repair_document(self) -> dict[str, object] | None:
        if not self.failed_attempts:
            return None
        return {
            "attempt_count": self.attempt_count,
            "failed_attempts": list(self.failed_attempts),
        }


class DraftPlanner:
    """Turn one question into a validated draft version."""

    def __init__(
        self,
        *,
        storage: ObjectStore,
        structured_client: StructuredArtifactClient,
        tool_registry: ToolRegistry,
    ) -> None:
        self._storage = storage
        self._tool_registry = tool_registry
        self.interpreter = QuestionInterpretationAndMatchingService(
            storage=storage,
            client=structured_client,
        )
        self.case_base = CaseBase(storage)
        self.planner = WorkflowPlanningService(
            storage=storage,
            client=structured_client,
            tool_registry=tool_registry,
        )
        self.validator = WorkflowValidator(
            storage=storage,
            tool_registry=tool_registry,
        )

    def interpret(self, question: str, *, catalog_version: str) -> InterpretationOutcome:
        """Interpret with a bounded diagnostic repair loop."""

        failed_attempts: list[Mapping[str, object]] = []
        repair_context: Mapping[str, object] | None = None
        for attempt in range(1, MAX_INTERPRETATION_ATTEMPTS + 1):
            interpretation = self.interpreter.interpret(
                question,
                catalog_version=catalog_version,
                repair_context=repair_context,
            )
            if isinstance(interpretation, SupportedInterpretation):
                break
            failed_attempts.append(
                {"diagnostic_codes": list(interpretation_diagnostic_codes(interpretation))}
            )
            if (
                attempt == MAX_INTERPRETATION_ATTEMPTS
                or not is_repairable_interpretation_failure(interpretation)
            ):
                break
            repair_context = interpretation_repair_context_document(
                interpretation, attempt=attempt + 1
            )
        return InterpretationOutcome(interpretation, attempt, tuple(failed_attempts))

    def retrieve(
        self,
        interpretation: SupportedInterpretation,
        catalog: CatalogVersion,
        *,
        expires_at: datetime,
    ) -> tuple[str, WorkflowDraft, ValidationResult] | None:
        """Replay an exact structural case-base hit, if one validates."""

        structure = task_structure(
            interpretation.task_specification, interpretation.bindings, catalog
        )
        hit = self.case_base.exact_match(structure)
        if hit is None:
            return None
        case_key, case = hit
        draft = instantiate_draft(
            case,
            interpretation,
            tool_registry_version=self._tool_registry.version,
            storage=self._storage,
        )
        validation = self._validate(draft, expires_at)
        if validation.status is ValidationStatus.FAIL:
            return None
        return case_key, draft, validation

    def compose(
        self,
        interpretation: SupportedInterpretation,
        catalog: CatalogVersion,
        *,
        expires_at: datetime,
        review_context: Mapping[str, object] | None = None,
        with_examples: bool = True,
    ) -> PlanningOutcome:
        """Compose with the planning model, repairing from diagnostics."""

        structure = task_structure(
            interpretation.task_specification, interpretation.bindings, catalog
        )
        case_examples = (
            case_example_documents(self.case_base.near_examples(structure))
            if with_examples
            else None
        )
        base_review_context = dict(review_context or {})
        context: Mapping[str, object] | None = base_review_context or None
        failed_attempts: list[Mapping[str, object]] = []
        attempt = 0
        while True:
            attempt += 1
            draft = self.planner.propose(
                interpretation,
                review_context=context,
                case_examples=case_examples,
            )
            validation = self._validate(draft, expires_at)
            if validation.status is not ValidationStatus.FAIL:
                break
            failed_attempts.append(
                {
                    "draft_id": draft.draft_id,
                    "diagnostic_codes": [item.code.value for item in validation.diagnostics],
                    "diagnostics": [
                        f"{item.code.value}: {item.message[:240]}"
                        for item in validation.diagnostics
                    ],
                }
            )
            if attempt >= MAX_PLANNING_ATTEMPTS:
                break
            context = {
                **base_review_context,
                "repair": planning_repair_context_document(
                    draft, validation, attempt=attempt + 1
                ),
            }
        return PlanningOutcome(draft, validation, attempt, tuple(failed_attempts))

    def plan(
        self,
        *,
        question: str,
        version: int,
        trigger: DraftTrigger,
        created_at: datetime,
        expires_at: datetime,
        instruction: str | None = None,
        previous_draft: Mapping[str, object] | None = None,
        answer_failure: Mapping[str, object] | None = None,
        execution_failure: Mapping[str, object] | None = None,
    ) -> SessionDraftVersion:
        """Interpret, then retrieve or compose, and record the draft version."""

        catalog_version = CatalogReader(self._storage).current().version
        interpreted = self.interpret(question, catalog_version=catalog_version)
        interpretation = interpreted.interpretation
        if isinstance(interpretation, UnsupportedInterpretation):
            return _unsupported_draft_document(
                interpretation,
                version=version,
                trigger=trigger,
                created_at=created_at,
                instruction=instruction,
                interpretation_repair=interpreted.repair_document,
            )
        catalog = CatalogReader(self._storage).get(catalog_version)
        # Edits and regenerations skip the replay (the reviewer asked for a
        # fresh plan) but still see near examples.
        if trigger == "submission":
            hit = self.retrieve(interpretation, catalog, expires_at=expires_at)
            if hit is not None:
                case_key, draft, validation = hit
                return _draft_document(
                    interpretation,
                    draft,
                    validation,
                    version=version,
                    trigger=trigger,
                    created_at=created_at,
                    instruction=instruction,
                    interpretation_repair=interpreted.repair_document,
                    planning_source="retrieval",
                    source_case_ref=case_key,
                )
        review_context: dict[str, object] = {}
        if previous_draft is not None:
            review_context.update({"instruction": instruction, "previous_draft": previous_draft})
        if answer_failure is not None:
            review_context["answer_construction_failure"] = answer_failure
        if execution_failure is not None:
            review_context["execution_failure"] = execution_failure
        outcome = self.compose(
            interpretation, catalog, expires_at=expires_at, review_context=review_context
        )
        return _draft_document(
            interpretation,
            outcome.draft,
            outcome.validation,
            version=version,
            trigger=trigger,
            created_at=created_at,
            instruction=instruction,
            interpretation_repair=interpreted.repair_document,
            planning_repair=outcome.repair_document,
            planning_source="composition",
        )

    def remember(
        self,
        session: QuestionSession,
        decision: ResultDecisionKind,
        *,
        at: datetime,
    ) -> None:
        """Retain an accepted workflow; drop a rejected retrieval replay."""

        latest = session.draft_versions[-1]
        if decision == "accepted" and latest.draft_id is not None:
            if latest.planning_source == "retrieval":
                return  # the case already exists
            pinned = WorkflowDraftRepository(self._storage).get(latest.draft_id)
            catalog = CatalogReader(self._storage).get(pinned.catalog_version)
            self.case_base.retain(
                question=session.question,
                draft=pinned,
                catalog=catalog,
                accepted_at=at,
            )
        elif decision == "rejected" and latest.source_case_ref is not None:
            self.case_base.remove(latest.source_case_ref)

    def _validate(self, draft: WorkflowDraft, expires_at: datetime) -> ValidationResult:
        validation = self.validator.validate(draft)
        WorkflowDraftRepository(self._storage).retain_until(draft.draft_id, expires_at)
        return validation


def _draft_document(
    interpretation: SupportedInterpretation,
    workflow: WorkflowDraft,
    validation: ValidationResult,
    *,
    version: int,
    trigger: DraftTrigger,
    created_at: datetime,
    instruction: str | None,
    interpretation_repair: Mapping[str, object] | None = None,
    planning_repair: Mapping[str, object] | None = None,
    planning_source: str = "composition",
    source_case_ref: str | None = None,
) -> SessionDraftVersion:
    payload: dict[str, object] = {
        "version": version,
        "trigger": trigger,
        "instruction": instruction,
        "created_at": _timestamp(created_at),
        "draft_id": workflow.draft_id,
        "question_phrases": [
            asdict(phrase) for phrase in interpretation.question_phrases
        ],
        "task_specification": asdict(workflow.task_specification.value),
        "bindings": [asdict(binding) for binding in workflow.data_bindings.value],
        "concrete_workflow": asdict(workflow.concrete_workflow.value),
        "assumptions": list(interpretation.assumptions),
        "unresolved_items": [],
        "validation": {
            "schema_version": validation.schema_version,
            "validation_id": validation.validation_id,
            "draft_id": validation.draft_id,
            "status": validation.status,
            "diagnostics": [asdict(item) for item in validation.diagnostics],
        },
        "unsupported_result": None,
        "planning_source": planning_source,
        "source_case_ref": source_case_ref,
    }
    if planning_repair is not None:
        payload["planning_repair"] = planning_repair
    if interpretation_repair is not None:
        payload["interpretation_repair"] = interpretation_repair
    payload["draft_version_id"] = sha256(canonical_json(payload))
    return SessionDraftVersion.model_validate(payload)


def _unsupported_draft_document(
    interpretation: UnsupportedInterpretation,
    *,
    version: int,
    trigger: DraftTrigger,
    created_at: datetime,
    instruction: str | None,
    interpretation_repair: Mapping[str, object] | None = None,
) -> SessionDraftVersion:
    diagnostics = [
        {
            "code": "unsupported",
            "message": reason,
            "artifact": "task-specification",
            "step_id": None,
            "ref": failure.role,
        }
        for failure in interpretation.failed_roles
        for reason in failure.rejection_reasons
    ]
    unresolved = [item["message"] for item in diagnostics]
    payload: dict[str, object] = {
        "version": version,
        "trigger": trigger,
        "instruction": instruction,
        "created_at": _timestamp(created_at),
        "draft_id": None,
        "question_phrases": [
            asdict(phrase) for phrase in interpretation.question_phrases
        ],
        "task_specification": asdict(interpretation.task_specification),
        "bindings": [],
        "concrete_workflow": None,
        "assumptions": list(interpretation.assumptions),
        "unresolved_items": unresolved,
        "validation": {
            "validation_id": None,
            "draft_id": None,
            "status": "fail",
            "diagnostics": diagnostics,
        },
        "unsupported_result": {
            "failed_roles": [
                asdict(failure) for failure in interpretation.failed_roles
            ]
        },
    }
    if interpretation_repair is not None:
        payload["interpretation_repair"] = interpretation_repair
    payload["draft_version_id"] = sha256(canonical_json(payload))
    return SessionDraftVersion.model_validate(payload)


def _timestamp(value: datetime) -> str:
    return utc(value).isoformat().replace("+00:00", "Z")
