# SPDX-License-Identifier: GPL-3.0-only

"""Owned state lifecycle for pre-execution Question Sessions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import logging

from app.api.raster_views import build_observation_listing, build_observation_view
from app.api.answer_maps import build_answer_map
from app.api.session_models import (
    FeedbackAction,
    FeedbackRecord,
    QuestionSession,
    QuestionSessionSummary,
    ResultDecision,
    ResultDecisionKind,
)
from pydantic import ValidationError

from app.api.draft_planning import DraftPlanner
from app.api.session_repository import (
    SESSION_RETENTION,
    QuestionSessionRepository,
    SessionExpiredError,
    SessionNotFoundError,
    SessionStateTransitionError,
)
from app.api.workflow_records import WorkflowRecordRepository, utc
from data_pipeline.storage import ObjectStore
from geoqa_agent.execution import (
    ExecutionAuthorization,
    ExecutionJob,
    ExecutionJobStatus,
    ExecutionRepository,
    ExecutionWorker,
)
from geoqa_agent.structured_artifacts import StructuredArtifactClient
from geoqa_agent.tool_registry import ToolRegistry


# Execution and answer construction are part of the repair surface: a
# validated plan that fails at runtime or produces a result table outside
# the family contract is replanned with the failure as context, up to
# this many automatic rounds per request.
MAX_ANSWER_REPAIR_ATTEMPTS = 2
LOGGER = logging.getLogger(__name__)


class QuestionSessionService:
    """Orchestrate planning and deterministic validation within owned state."""

    def __init__(
        self,
        *,
        storage: ObjectStore,
        structured_clients: Mapping[str, StructuredArtifactClient],
        tool_registry: ToolRegistry,
        clock: Callable[[], datetime],
        session_id_factory: Callable[[], str],
        execution_worker: ExecutionWorker,
        job_id_factory: Callable[[], str],
    ) -> None:
        self._storage = storage
        self._clock = clock
        self._session_id_factory = session_id_factory
        self._job_id_factory = job_id_factory
        self._execution_worker = execution_worker
        self._execution_repository = ExecutionRepository(storage)
        self._repository = QuestionSessionRepository(storage)
        self._workflow_records = WorkflowRecordRepository(storage)
        # One planner per selectable model; the session remembers its model
        # so edits and regenerations keep using it.
        self._planners = {
            model: DraftPlanner(
                storage=storage,
                structured_client=client,
                tool_registry=tool_registry,
            )
            for model, client in structured_clients.items()
        }

    def create(
        self,
        *,
        owner_principal_id: str,
        question: str,
        model: str | None = None,
    ) -> QuestionSession:
        model = model or next(iter(self._planners))
        now = utc(self._clock())
        draft = self._planners[model].plan(
            question=question,
            version=1,
            trigger="submission",
            created_at=now,
            expires_at=now + SESSION_RETENTION,
        )
        session = QuestionSession(
            session_id=self._session_id_factory(),
            version=1,
            owner_principal_id=owner_principal_id,
            question=question,
            model=model,
            created_at=now,
            updated_at=now,
            expires_at=now + SESSION_RETENTION,
            current_draft_version=1,
            draft_versions=(draft,),
            feedback_history=(),
            execution_authorization=None,
            execution_result=None,
            candidate_answer=None,
            candidate_answer_failure=None,
            result_decision=None,
        )
        self._repository.save(session)
        LOGGER.info(
            "question_session.created session=%s status=%s",
            session.session_id,
            draft.validation.status,
        )
        if draft.validation.status != "fail":
            return self._execute_current_draft(
                session,
                owner_principal_id=owner_principal_id,
                repair_budget=MAX_ANSWER_REPAIR_ATTEMPTS,
            )
        return session

    def get(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
    ) -> QuestionSession:
        session = self._repository.get(
            session_id,
            owner_principal_id=owner_principal_id,
        )
        self._ensure_active(session)
        return session

    def list(
        self,
        *,
        owner_principal_id: str,
    ) -> tuple[QuestionSessionSummary, ...]:
        summaries: list[QuestionSessionSummary] = []
        for session_id in self._repository.list_session_ids():
            try:
                session = self.get(
                    session_id,
                    owner_principal_id=owner_principal_id,
                )
            except (SessionNotFoundError, SessionExpiredError):
                continue
            except ValidationError:
                # A session persisted by an older schema; it expires with
                # its TTL and must not break the listing meanwhile.
                continue
            summaries.append(QuestionSessionSummary.of(session))
        summaries.sort(key=lambda summary: summary.created_at, reverse=True)
        return tuple(summaries)

    def delete(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
    ) -> None:
        session = self._repository.get(
            session_id,
            owner_principal_id=owner_principal_id,
        )
        self._ensure_active(session)
        self._repository.delete(session)
        LOGGER.info("question_session.deleted session=%s", session.session_id)

    def get_answer_map(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
    ) -> dict[str, object]:
        """Return display geometry only for the current owned Candidate Answer."""

        session = self.get(session_id, owner_principal_id=owner_principal_id)
        return build_answer_map(self._storage, session)

    def get_observations(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
    ) -> dict[str, object]:
        """List the raster and satellite data behind the owned execution."""

        session = self.get(session_id, owner_principal_id=owner_principal_id)
        return build_observation_listing(self._storage, session)

    def get_observation_view(
        self,
        session_id: str,
        ref: str,
        *,
        owner_principal_id: str,
    ) -> dict[str, object]:
        session = self.get(session_id, owner_principal_id=owner_principal_id)
        return build_observation_view(self._storage, session, ref)

    def edit(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
        instruction: str,
    ) -> QuestionSession:
        return self._revise(
            session_id,
            owner_principal_id=owner_principal_id,
            trigger="edit",
            instruction=instruction,
            repair_budget=MAX_ANSWER_REPAIR_ATTEMPTS,
        )

    def regenerate(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
    ) -> QuestionSession:
        return self._revise(
            session_id,
            owner_principal_id=owner_principal_id,
            trigger="regeneration",
            instruction=None,
            repair_budget=MAX_ANSWER_REPAIR_ATTEMPTS,
        )

    def decide_result(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
        decision: ResultDecisionKind,
        feedback: str | None,
    ) -> QuestionSession:
        """Record one owner-bound Candidate Answer decision."""

        session = self._repository.get(
            session_id,
            owner_principal_id=owner_principal_id,
        )
        self._ensure_active(session)
        if session.candidate_answer is None:
            raise SessionStateTransitionError(
                "Only a Candidate Answer may be accepted or rejected."
            )
        if (
            session.execution_result is None
            or session.execution_result.status is not ExecutionJobStatus.SUCCEEDED
            or session.candidate_answer.reproducibility.execution_job_id
            != session.execution_result.job_id
        ):
            raise SessionStateTransitionError(
                "Only the current successful Execution Result may be decided."
            )
        if session.result_decision is not None:
            raise SessionStateTransitionError(
                "The Candidate Answer already has a result decision."
            )
        now = utc(self._clock())
        references = self._workflow_records.decision_references(
            session_id=session.session_id,
            candidate_answer_id=session.candidate_answer.candidate_answer_id,
            actor_principal_id=owner_principal_id,
            decided_at=now,
            decision=decision,
        )
        result_decision = ResultDecision(
            decision=decision,
            candidate_answer_id=session.candidate_answer.candidate_answer_id,
            actor_principal_id=owner_principal_id,
            decided_at=now,
            feedback=feedback,
            workflow_id=references.workflow_id,
            answer_artifact_ref=references.answer_artifact_ref,
            workflow_record_ref=references.workflow_record_ref,
        )
        # The record is written first; if the session update then fails, the
        # orphaned record simply expires with its blob TTL.
        self._workflow_records.persist_decision(session, result_decision)
        updated = session.model_copy(
            update={
                "version": session.version + 1,
                "updated_at": now,
                "result_decision": result_decision,
            }
        )
        self._repository.save(updated)
        self._planners[session.model].remember(session, decision, at=now)
        LOGGER.info(
            "candidate_answer.%s session=%s workflow=%s",
            decision,
            session.session_id,
            references.workflow_id,
        )
        return updated

    def _execute_current_draft(
        self,
        session: QuestionSession,
        *,
        owner_principal_id: str,
        repair_budget: int,
    ) -> QuestionSession:
        """Execute the freshly planned, validator-passed draft in-request.

        Submitting the question is the authorization: execution is cheap,
        read-only over pinned snapshots, and allow-listed, so the human
        gate sits where it is informative — reviewing the workflow together
        with its answer, then accepting or rejecting.
        """

        draft = session.draft_versions[-1]
        validation_id = draft.validation.validation_id
        assert draft.draft_id is not None and validation_id is not None
        now = utc(self._clock())
        authorization = ExecutionAuthorization(
            session_id=session.session_id,
            actor_principal_id=owner_principal_id,
            authorized_at=now,
            expires_at=session.expires_at,
            draft_version=draft.version,
            draft_version_id=draft.draft_version_id,
            draft_id=draft.draft_id,
            validation_id=validation_id,
        )
        job = ExecutionJob(
            job_id=self._job_id_factory(),
            session_id=session.session_id,
            owner_principal_id=owner_principal_id,
            authorization=authorization,
            draft_id=draft.draft_id,
            validation_id=validation_id,
            status=ExecutionJobStatus.RUNNING,
            created_at=now,
            updated_at=now,
            expires_at=session.expires_at,
        )
        self._execution_repository.create_job(job)
        updated = session.model_copy(
            update={
                "version": session.version + 1,
                "updated_at": now,
                "execution_authorization": authorization,
            }
        )
        self._repository.save(updated)
        # In-process execution: the worker runs the plan now and its session
        # sink folds the terminal job and Candidate Answer back into the
        # session, so the fresh state is re-read for the response.
        self._execution_worker.execute(job.job_id)
        session = self._repository.get(
            session.session_id,
            owner_principal_id=owner_principal_id,
        )
        if repair_budget > 0 and _answer_repair_needed(session):
            return self._revise(
                session.session_id,
                owner_principal_id=owner_principal_id,
                trigger="auto_repair",
                instruction=None,
                repair_budget=repair_budget - 1,
            )
        return session

    def _revise(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
        trigger: FeedbackAction,
        instruction: str | None,
        repair_budget: int,
    ) -> QuestionSession:
        """Plan a new draft version, superseding all execution-phase state."""

        session = self._repository.get(
            session_id,
            owner_principal_id=owner_principal_id,
        )
        self._ensure_active(session)
        if session.result_decision is not None:
            raise SessionStateTransitionError(
                "A reviewed session cannot be revised."
            )
        version = session.current_draft_version + 1
        drafts = session.draft_versions
        current = drafts[-1]
        now = utc(self._clock())
        draft = self._planners[session.model].plan(
            question=session.question,
            version=version,
            trigger=trigger,
            created_at=now,
            expires_at=session.expires_at,
            instruction=instruction,
            previous_draft=(
                current.model_dump(mode="json")
                if instruction is not None
                else None
            ),
            # A failed run is planning-relevant evidence: the retry should
            # know which contract columns were missing or which step died.
            answer_failure=(
                session.candidate_answer_failure.model_dump(mode="json")
                if session.candidate_answer_failure is not None
                else None
            ),
            execution_failure=(
                session.execution_result.failure.model_dump(mode="json")
                if session.execution_result is not None
                and session.execution_result.failure is not None
                else None
            ),
        )
        feedback = FeedbackRecord(
            action=trigger,
            instruction=instruction,
            actor_principal_id=owner_principal_id,
            submitted_at=now,
            from_draft_version=version - 1,
            to_draft_version=version,
        )
        updated = session.model_copy(
            update={
                "version": session.version + 1,
                "updated_at": now,
                "current_draft_version": version,
                "draft_versions": (*drafts, draft),
                "feedback_history": (*session.feedback_history, feedback),
                "execution_authorization": None,
                "execution_result": None,
                "candidate_answer": None,
                "candidate_answer_failure": None,
                "result_decision": None,
            }
        )
        self._repository.save(updated)
        LOGGER.info(
            "question_session.revised session=%s version=%s trigger=%s status=%s",
            session.session_id,
            draft.version,
            trigger,
            draft.validation.status,
        )
        if draft.validation.status != "fail":
            return self._execute_current_draft(
                updated,
                owner_principal_id=owner_principal_id,
                repair_budget=repair_budget,
            )
        return updated

    def _ensure_active(self, session: QuestionSession) -> None:
        if utc(self._clock()) >= session.expires_at:
            raise SessionExpiredError(session.session_id)


def _answer_repair_needed(session: QuestionSession) -> bool:
    """A validated plan that died at runtime or outside the answer contract."""

    if session.candidate_answer_failure is not None:
        return True
    return (
        session.execution_result is not None
        and session.execution_result.status is ExecutionJobStatus.FAILED
    )
