# SPDX-License-Identifier: GPL-3.0-only

"""Typed persisted state for the Human Review Question Session aggregate."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict

from geoqa_agent.candidate_answer import (
    CandidateAnswerFailure,
    CandidateAnswerValue,
)
from geoqa_agent.execution import (
    ExecutionAuthorization,
    ExecutionJob,
    ExecutionJobReference,
)


DraftTrigger = Literal["submission", "edit", "regeneration", "result_rejection", "auto_repair"]
FeedbackAction = Literal["edit", "regeneration", "result_rejection", "auto_repair"]
ResultDecisionKind = Literal["accepted", "rejected"]


class ImmutableSessionModel(BaseModel):
    """Reject undeclared persisted fields and prevent in-place model mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ValidationReview(ImmutableSessionModel):
    schema_version: Literal["workflow-validation-v2"] = "workflow-validation-v2"
    validation_id: str | None
    draft_id: str | None
    status: Literal["pass", "pass_with_warnings", "fail"]
    diagnostics: tuple[Mapping[str, object], ...]


class UnsupportedReview(ImmutableSessionModel):
    failed_roles: tuple[Mapping[str, object], ...]


class FailedPlanningAttempt(ImmutableSessionModel):
    draft_id: str
    diagnostic_codes: tuple[str, ...]


class PlanningRepair(ImmutableSessionModel):
    attempt_count: int
    failed_attempts: tuple[FailedPlanningAttempt, ...]
    escalated: bool = False


class FailedInterpretationAttempt(ImmutableSessionModel):
    diagnostic_codes: tuple[str, ...]


class InterpretationRepair(ImmutableSessionModel):
    attempt_count: int
    failed_attempts: tuple[FailedInterpretationAttempt, ...]


class SessionDraftVersion(ImmutableSessionModel):
    """One immutable proposal or unsupported planning attempt shown for review."""

    version: int
    draft_version_id: str
    trigger: DraftTrigger
    instruction: str | None
    created_at: datetime
    draft_id: str | None
    question_phrases: tuple[Mapping[str, object], ...]
    task_specification: Mapping[str, object]
    bindings: tuple[Mapping[str, object], ...]
    abstract_workflow: Mapping[str, object] | None
    concrete_workflow: Mapping[str, object] | None
    assumptions: tuple[str, ...]
    unresolved_items: tuple[str, ...]
    validation: ValidationReview
    unsupported_result: UnsupportedReview | None
    planning_repair: PlanningRepair | None = None
    interpretation_repair: InterpretationRepair | None = None
    # Which planning tier produced this draft, and (for a retrieved one)
    # the case it replayed — removed again if the reviewer rejects it.
    planning_source: Literal["composition", "enumeration", "retrieval"] | None = None
    source_case_ref: str | None = None


class FeedbackRecord(ImmutableSessionModel):
    action: FeedbackAction
    instruction: str | None
    actor_principal_id: str
    submitted_at: datetime
    from_draft_version: int
    to_draft_version: int


class ResultDecision(ImmutableSessionModel):
    """One immutable post-execution decision by the owning Review Actor."""

    decision: ResultDecisionKind
    candidate_answer_id: str
    actor_principal_id: str
    decided_at: datetime
    feedback: str | None
    workflow_id: str | None
    answer_artifact_ref: str | None
    workflow_record_ref: str | None


class QuestionSession(ImmutableSessionModel):
    """Owner-bound, optimistic-concurrency-controlled review aggregate."""

    session_id: str
    version: int
    owner_principal_id: str
    question: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    current_draft_version: int
    draft_versions: tuple[SessionDraftVersion, ...]
    feedback_history: tuple[FeedbackRecord, ...]
    execution_authorization: ExecutionAuthorization | None
    job_reference: ExecutionJobReference | None
    execution_result: ExecutionJob | None
    candidate_answer: CandidateAnswerValue | None
    candidate_answer_failure: CandidateAnswerFailure | None
    result_review_history: tuple[ResultDecision, ...]
    result_decision: ResultDecision | None


class QuestionSessionSummary(ImmutableSessionModel):
    """Current lifecycle status for one owned Question Session."""

    session_id: str
    question: str
    created_at: datetime
    expires_at: datetime
    current_draft_version: int
    latest_validation_status: Literal["pass", "pass_with_warnings", "fail"]
    has_execution_job: bool
    has_candidate_answer: bool
    has_result_decision: bool
