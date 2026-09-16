# SPDX-License-Identifier: GPL-3.0-only

"""Persist reviewed Workflow Records and accepted Answer Artifacts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict

from app.api.session_models import QuestionSession, ResultDecision
from data_pipeline.serialization import canonical_json, sha256
from data_pipeline.storage import ObjectStore
from geoqa_agent.candidate_answer import (
    AnswerMapRepresentation,
    CandidateAnswerValue,
    ReproducibilityEnvelope,
)
from geoqa_agent.execution import ExecutionJob


WORKFLOW_PREFIX = "workflows"


class ImmutableWorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HumanReviewRecord(ImmutableWorkflowModel):
    execution_authorized_by: str
    execution_authorized_at: datetime
    pre_execution_actions: tuple[Mapping[str, object], ...]
    result_decided_by: str
    result_decided_at: datetime
    post_execution_action: Literal["accept_result", "reject_result"] = (
        "accept_result"
    )
    post_execution_feedback: str | None


class AnswerArtifact(ImmutableWorkflowModel):
    answer_artifact_id: str
    workflow_id: str
    session_id: str
    created_at: datetime
    candidate_answer: CandidateAnswerValue
    answer_map: AnswerMapRepresentation
    output_locations: Mapping[str, str]
    accepted_by: str
    accepted_at: datetime
    feedback: str | None


class WorkflowRecord(ImmutableWorkflowModel):
    """Self-contained reviewed workflow: every artifact needed to audit or
    reproduce the decision without the originating session."""

    schema_version: Literal["workflow-record-v4"] = "workflow-record-v4"
    workflow_id: str
    session_id: str
    created_at: datetime
    expires_at: datetime
    question_text: str
    question_phrases: tuple[Mapping[str, object], ...]
    task_semantics: Mapping[str, object]
    data_bindings: tuple[Mapping[str, object], ...]
    concrete_workflow: Mapping[str, object]
    validation_result: Mapping[str, object]
    execution_result: ExecutionJob
    output_locations: Mapping[str, str]
    answer_artifact_ref: str | None
    human_review: HumanReviewRecord
    status: Literal["result_accepted", "result_rejected"]
    reproducibility: ReproducibilityEnvelope


class PersistedDecision(ImmutableWorkflowModel):
    workflow_id: str
    answer_artifact_ref: str | None
    workflow_record_ref: str


class WorkflowRecordRepository:
    """Write a self-contained, expiring record from a reviewed session."""

    def __init__(self, storage: ObjectStore) -> None:
        self._storage = storage

    def decision_references(
        self,
        *,
        session_id: str,
        candidate_answer_id: str,
        actor_principal_id: str,
        decided_at: datetime,
        decision: Literal["accepted", "rejected"],
    ) -> PersistedDecision:
        # The workflow id is derived from the decision identity, so a retried
        # persistence lands on the same keys instead of duplicating records.
        workflow_id = sha256(
            canonical_json(
                {
                    "session_id": session_id,
                    "candidate_answer_id": candidate_answer_id,
                    "actor_principal_id": actor_principal_id,
                    "decided_at": utc(decided_at).isoformat(),
                }
            )
        )
        prefix = f"{WORKFLOW_PREFIX}/{workflow_id}"
        return PersistedDecision(
            workflow_id=workflow_id,
            answer_artifact_ref=(
                self._storage.uri(f"{prefix}/answer.json")
                if decision == "accepted"
                else None
            ),
            workflow_record_ref=self._storage.uri(f"{prefix}/record.json"),
        )

    def persist_decision(
        self,
        session: QuestionSession,
        decision: ResultDecision,
    ) -> None:
        """Persist one complete terminal review decision and its provenance."""

        candidate = session.candidate_answer
        authorization = session.execution_authorization
        execution = session.execution_result
        if candidate is None or authorization is None or execution is None:
            raise ValueError(
                "Workflow persistence requires a decided completed Candidate Answer."
            )
        if decision.candidate_answer_id != candidate.candidate_answer_id:
            raise ValueError("Result decision does not identify the Candidate Answer.")
        draft = session.draft_versions[-1]
        if draft.concrete_workflow is None:
            raise ValueError("Decided Candidate Answer has no executable workflow.")
        assert decision.workflow_id is not None
        workflow_id = decision.workflow_id
        prefix = f"{WORKFLOW_PREFIX}/{workflow_id}"
        if decision.decision == "accepted":
            answer = AnswerArtifact(
                answer_artifact_id=(
                    f"sha256:{sha256(candidate.identity_payload())}"
                ),
                workflow_id=workflow_id,
                session_id=session.session_id,
                created_at=decision.decided_at,
                candidate_answer=candidate,
                answer_map=candidate.answer_map,
                output_locations=execution.output_locations,
                accepted_by=decision.actor_principal_id,
                accepted_at=decision.decided_at,
                feedback=decision.feedback,
            )
            self._put_expiring(
                f"{prefix}/answer.json",
                canonical_json(answer.model_dump(mode="json")),
                session.expires_at,
            )
        record = WorkflowRecord(
            workflow_id=workflow_id,
            session_id=session.session_id,
            created_at=decision.decided_at,
            expires_at=session.expires_at,
            question_text=session.question,
            question_phrases=draft.question_phrases,
            task_semantics=draft.task_specification,
            data_bindings=draft.bindings,
            concrete_workflow={
                **draft.concrete_workflow,
                "data_bindings": list(draft.bindings),
            },
            validation_result=draft.validation.model_dump(mode="json"),
            execution_result=execution,
            output_locations=execution.output_locations,
            answer_artifact_ref=decision.answer_artifact_ref,
            human_review=HumanReviewRecord(
                execution_authorized_by=authorization.actor_principal_id,
                execution_authorized_at=authorization.authorized_at,
                pre_execution_actions=tuple(
                    item.model_dump(mode="json")
                    for item in session.feedback_history
                ),
                result_decided_by=decision.actor_principal_id,
                result_decided_at=decision.decided_at,
                post_execution_action=(
                    "accept_result"
                    if decision.decision == "accepted"
                    else "reject_result"
                ),
                post_execution_feedback=decision.feedback,
            ),
            status=(
                "result_accepted"
                if decision.decision == "accepted"
                else "result_rejected"
            ),
            reproducibility=candidate.reproducibility,
        )
        self._put_expiring(
            f"{prefix}/record.json",
            canonical_json(record.model_dump(mode="json")),
            session.expires_at,
        )

    def _put_expiring(
        self,
        key: str,
        data: bytes,
        expires_at: datetime,
    ) -> None:
        self._storage.put_immutable(key, data)
        self._storage.set_expiry(key, expires_at)


def utc(value: datetime) -> datetime:
    """Normalize an aware timestamp to UTC; naive clocks are a bug."""

    if value.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return value.astimezone(UTC)
