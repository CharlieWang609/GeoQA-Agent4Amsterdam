# SPDX-License-Identifier: GPL-3.0-only

"""Owned state lifecycle for pre-execution Question Sessions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import logging
from typing import Mapping

from app.api.answer_maps import build_answer_map
from app.api.session_models import (
    DraftTrigger,
    FeedbackAction,
    FeedbackRecord,
    QuestionSession,
    QuestionSessionSummary,
    ResultDecision,
    ResultDecisionKind,
    SessionDraftVersion,
)
from pydantic import ValidationError

from app.api.workflow_records import WorkflowRecordRepository
from data_pipeline.catalog import CatalogReader
from data_pipeline.errors import ConcurrentPublicationError
from data_pipeline.serialization import canonical_json, sha256
from data_pipeline.storage import ObjectStore
from geoqa_agent.candidate_answer import (
    CandidateAnswerConstructor,
    CandidateAnswerRejected,
)
from geoqa_agent.execution import (
    AdvisoryOverride,
    ExecutionAuthorization,
    ExecutionAuthorizationError,
    ExecutionFailure,
    ExecutionJob,
    ExecutionJobReference,
    ExecutionJobStatus,
    ExecutionRepository,
    ExecutionWorker,
)
from geoqa_agent.question_interpretation import (
    QuestionInterpretationAndMatchingService,
    SupportedInterpretation,
    UnsupportedInterpretation,
    interpretation_diagnostic_codes,
    interpretation_repair_context_document,
    is_repairable_interpretation_failure,
)
from geoqa_agent.case_base import (
    CaseBase,
    case_example_documents,
    instantiate_draft,
    task_structure,
)
from geoqa_agent.semantic_validation import AbstractionCatalog
from geoqa_agent.skeleton_enumeration import skeletons_for_interpretation
from geoqa_agent.structured_artifacts import StructuredArtifactClient
from geoqa_agent.tool_registry import ToolRegistry
from geoqa_agent.workflow_planning import (
    AbstractWorkflow,
    ValidationResult,
    ValidationSeverity,
    ValidationStatus,
    WorkflowDraft,
    WorkflowDraftRepository,
    WorkflowPlanningService,
    WorkflowValidator,
    planning_repair_context_document,
)


SESSION_RETENTION = timedelta(days=7)
SESSION_PREFIX = "question-sessions"
# The initial interpretation plus up to two diagnostic-guided repairs.
MAX_INTERPRETATION_ATTEMPTS = 3
# Execution and answer construction are part of the repair surface: a
# validated plan that fails at runtime or produces a result table outside
# the family contract is replanned with the failure as context, up to
# this many automatic rounds per request.
MAX_ANSWER_REPAIR_ATTEMPTS = 2
# Free composition converges by attempt 2-3 or not at all (measured in the
# budget-10 benchmark round), so it gets three attempts ...
MAX_PLANNING_ATTEMPTS = 3
# ... and then planning escalates: enumerated well-typed skeletons are
# supplied and the model selects instead of composing, with one repair.
ENUMERATION_PLANNING_ATTEMPTS = 2
LOGGER = logging.getLogger(__name__)


class SessionNotFoundError(LookupError):
    """The session does not exist or is not visible to this principal."""


class SessionExpiredError(LookupError):
    """The temporary Question Session has passed its retention deadline."""


class SessionPreconditionError(ValueError):
    """The caller's session ETag no longer identifies the current version."""


class SessionPreconditionRequiredError(ValueError):
    """The caller omitted the session ETag required for deletion."""


class SessionStateTransitionError(ValueError):
    """The requested review action is invalid for the current session state."""


class QuestionSessionRepository:
    """Persist each Question Session as one ETag-guarded JSON document."""

    def __init__(self, storage: ObjectStore) -> None:
        self._storage = storage

    def create(self, session: QuestionSession) -> str:
        key = _session_key(session.session_id)
        data = canonical_json(session.model_dump(mode="json"))
        try:
            self._storage.compare_and_swap(key, data, None)
        except ConcurrentPublicationError as error:
            raise ValueError(
                f"Question Session already exists: {session.session_id}"
            ) from error
        self._storage.set_expiry(key, session.expires_at)
        return _public_etag(data)

    def get(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
    ) -> tuple[QuestionSession, str]:
        stored = self._storage.read(_session_key(session_id))
        if stored is None:
            raise SessionNotFoundError(session_id)
        session = QuestionSession.model_validate_json(stored.data)
        if session.owner_principal_id != owner_principal_id:
            raise SessionNotFoundError(session_id)
        return session, _public_etag(stored.data)

    def update(
        self,
        session: QuestionSession,
        *,
        expected_etag: str,
    ) -> str:
        key = _session_key(session.session_id)
        current = self._storage.read(key)
        if current is None or _public_etag(current.data) != expected_etag:
            raise SessionPreconditionError(
                "Question Session changed after it was inspected."
            )
        data = canonical_json(session.model_dump(mode="json"))
        try:
            self._storage.compare_and_swap(key, data, current.etag)
        except ConcurrentPublicationError as error:
            raise SessionPreconditionError(
                "Question Session changed after it was inspected."
            ) from error
        self._storage.set_expiry(key, session.expires_at)
        return _public_etag(data)

    def delete(
        self,
        session: QuestionSession,
        *,
        expected_etag: str,
    ) -> None:
        key = _session_key(session.session_id)
        current = self._storage.read(key)
        if current is None or _public_etag(current.data) != expected_etag:
            raise SessionPreconditionError(
                "Question Session changed after it was inspected."
            )
        self._storage.delete(key)

    def list_session_ids(self) -> tuple[str, ...]:
        prefix = f"{SESSION_PREFIX}/"
        ids = []
        for key in self._storage.list_keys(prefix):
            relative = key.removeprefix(prefix)
            if "/" not in relative and relative.endswith(".json"):
                ids.append(relative.removesuffix(".json"))
        return tuple(ids)


class QuestionSessionExecutionSink:
    """Aggregate one terminal Execution Job into its owning session."""

    def __init__(
        self,
        storage: ObjectStore,
        *,
        candidate_answer_builder: CandidateAnswerConstructor | None = None,
    ) -> None:
        self._repository = QuestionSessionRepository(storage)
        self._candidate_answer_builder = candidate_answer_builder

    def record(self, job: ExecutionJob) -> None:
        """Fold a terminal job (and its Candidate Answer) into the session.

        Idempotent: a no-longer-referenced job or an already-recorded result
        returns silently; an ETag race retries once against fresh state.
        """

        if job.status not in {
            ExecutionJobStatus.SUCCEEDED,
            ExecutionJobStatus.FAILED,
        }:
            raise ValueError("Only a terminal Execution Job may be aggregated.")
        for _ in range(2):
            session, etag = self._repository.get(
                job.session_id,
                owner_principal_id=job.owner_principal_id,
            )
            if (
                session.job_reference is None
                or session.job_reference.job_id != job.job_id
            ):
                return
            if session.execution_result == job:
                return
            candidate_answer = None
            candidate_answer_failure = None
            if (
                job.status is ExecutionJobStatus.SUCCEEDED
                and self._candidate_answer_builder is not None
            ):
                try:
                    candidate_answer = self._candidate_answer_builder.construct(job)
                except CandidateAnswerRejected as error:
                    candidate_answer_failure = error.failure
            updated = session.model_copy(
                update={
                    "version": session.version + 1,
                    "updated_at": job.updated_at,
                    "job_reference": ExecutionJobReference(
                        job_id=job.job_id,
                        status=job.status,
                    ),
                    "execution_result": job,
                    "candidate_answer": candidate_answer,
                    "candidate_answer_failure": candidate_answer_failure,
                    "result_decision": None,
                }
            )
            try:
                self._repository.update(updated, expected_etag=etag)
            except SessionPreconditionError:
                continue
            _log_execution_aggregation(updated, job)
            return
        raise SessionPreconditionError(
            "Question Session changed while recording execution state."
        )


class QuestionSessionService:
    """Orchestrate planning and deterministic validation within owned state."""

    def __init__(
        self,
        *,
        storage: ObjectStore,
        structured_client: StructuredArtifactClient,
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
        self._interpreter = QuestionInterpretationAndMatchingService(
            storage=storage,
            client=structured_client,
        )
        self._abstractions = AbstractionCatalog()
        self._tool_registry = tool_registry
        self._case_base = CaseBase(storage)
        self._planner = WorkflowPlanningService(
            storage=storage,
            client=structured_client,
            tool_registry=tool_registry,
            abstraction_catalog=self._abstractions,
        )
        self._validator = WorkflowValidator(
            storage=storage,
            tool_registry=tool_registry,
            abstraction_catalog=self._abstractions,
        )

    def create(
        self,
        *,
        owner_principal_id: str,
        question: str,
    ) -> tuple[QuestionSession, str]:
        now = _utc(self._clock())
        draft = self._plan_draft(
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
            created_at=now,
            updated_at=now,
            expires_at=now + SESSION_RETENTION,
            current_draft_version=1,
            draft_versions=(draft,),
            feedback_history=(),
            execution_authorization=None,
            job_reference=None,
            execution_result=None,
            candidate_answer=None,
            candidate_answer_failure=None,
            result_review_history=(),
            result_decision=None,
        )
        etag = self._repository.create(session)
        LOGGER.info(
            "question_session.created session=%s status=%s",
            session.session_id,
            draft.validation.status,
        )
        if draft.validation.status != "fail":
            return self._execute_current_draft(
                session.session_id,
                owner_principal_id=owner_principal_id,
                expected_etag=etag,
                repair_budget=MAX_ANSWER_REPAIR_ATTEMPTS,
            )
        return session, etag

    def get(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
    ) -> tuple[QuestionSession, str]:
        session, etag = self._repository.get(
            session_id,
            owner_principal_id=owner_principal_id,
        )
        self._ensure_active(session)
        return session, etag

    def list(
        self,
        *,
        owner_principal_id: str,
    ) -> tuple[QuestionSessionSummary, ...]:
        summaries: list[QuestionSessionSummary] = []
        for session_id in self._repository.list_session_ids():
            try:
                session, _ = self.get(
                    session_id,
                    owner_principal_id=owner_principal_id,
                )
            except (SessionNotFoundError, SessionExpiredError):
                continue
            except ValidationError:
                # A session persisted by an older schema; it expires with
                # its TTL and must not break the listing meanwhile.
                continue
            latest_draft = session.draft_versions[-1]
            summaries.append(
                QuestionSessionSummary(
                    session_id=session.session_id,
                    question=session.question,
                    created_at=session.created_at,
                    expires_at=session.expires_at,
                    current_draft_version=session.current_draft_version,
                    latest_validation_status=latest_draft.validation.status,
                    has_execution_job=session.job_reference is not None,
                    has_candidate_answer=session.candidate_answer is not None,
                    has_result_decision=session.result_decision is not None,
                )
            )
        summaries.sort(key=lambda summary: summary.created_at, reverse=True)
        return tuple(summaries)

    def delete(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
        if_match: str | None,
    ) -> None:
        session, current_etag = self._repository.get(
            session_id,
            owner_principal_id=owner_principal_id,
        )
        self._ensure_active(session)
        expected_etag = normalize_session_etag(if_match)
        if current_etag != expected_etag:
            raise SessionPreconditionError(
                "Question Session changed after it was inspected."
            )
        self._repository.delete(session, expected_etag=expected_etag)
        LOGGER.info("question_session.deleted session=%s", session.session_id)

    def get_execution_job(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
    ) -> ExecutionJob:
        session, _ = self.get(
            session_id,
            owner_principal_id=owner_principal_id,
        )
        if session.job_reference is None:
            raise SessionNotFoundError(session_id)
        job = self._execution_repository.get_job(session.job_reference.job_id)
        if (
            job.session_id != session.session_id
            or job.owner_principal_id != owner_principal_id
        ):
            raise SessionNotFoundError(session_id)
        return job

    def get_answer_map(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
    ) -> dict[str, object]:
        """Return display geometry only for the current owned Candidate Answer."""

        session, _ = self.get(
            session_id,
            owner_principal_id=owner_principal_id,
        )
        return build_answer_map(self._storage, session)

    def edit(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
        instruction: str,
        expected_etag: str,
    ) -> tuple[QuestionSession, str]:
        return self._revise(
            session_id,
            owner_principal_id=owner_principal_id,
            expected_etag=expected_etag,
            trigger="edit",
            instruction=instruction,
            repair_budget=MAX_ANSWER_REPAIR_ATTEMPTS,
        )

    def regenerate(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
        expected_etag: str,
    ) -> tuple[QuestionSession, str]:
        return self._revise(
            session_id,
            owner_principal_id=owner_principal_id,
            expected_etag=expected_etag,
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
        expected_etag: str,
    ) -> tuple[QuestionSession, str]:
        """Record one owner-bound, ETag-guarded Candidate Answer decision."""

        session, current_etag = self._repository.get(
            session_id,
            owner_principal_id=owner_principal_id,
        )
        self._ensure_active(session)
        if current_etag != expected_etag:
            raise SessionPreconditionError(
                "Question Session changed after it was inspected."
            )
        if session.candidate_answer is None:
            raise SessionStateTransitionError(
                "Only a Candidate Answer may be accepted or rejected."
            )
        if (
            session.execution_result is None
            or session.execution_result.status is not ExecutionJobStatus.SUCCEEDED
            or session.job_reference is None
            or session.job_reference.job_id != session.execution_result.job_id
            or session.job_reference.status is not ExecutionJobStatus.SUCCEEDED
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
        now = _utc(self._clock())
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
        committed_etag = self._repository.update(
            updated,
            expected_etag=expected_etag,
        )
        self._update_case_base(session, decision, at=now)
        LOGGER.info(
            "candidate_answer.%s session=%s workflow=%s",
            decision,
            session.session_id,
            references.workflow_id,
        )
        return updated, committed_etag

    def _update_case_base(
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
            self._case_base.retain(
                question=session.question,
                draft=pinned,
                catalog=catalog,
                planning_source=(
                    "enumeration"
                    if latest.planning_source == "enumeration"
                    else "composition"
                ),
                accepted_at=at,
            )
        elif decision == "rejected" and latest.source_case_ref is not None:
            self._case_base.remove(latest.source_case_ref)

    def _execute_current_draft(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
        expected_etag: str,
        repair_budget: int,
    ) -> tuple[QuestionSession, str]:
        """Execute the freshly planned draft within the same request.

        Submitting the question is the authorization: execution is cheap,
        read-only over pinned snapshots, and allow-listed, so the human
        gate sits where it is informative — reviewing the workflow together
        with its answer, then accepting or rejecting.
        """

        session, current_etag = self._repository.get(
            session_id,
            owner_principal_id=owner_principal_id,
        )
        if current_etag != expected_etag:
            raise SessionPreconditionError(
                "Question Session changed after it was inspected."
            )
        # Re-load the exact pinned Validation result and require it to agree
        # with the session's copy; the session view alone is not trusted.
        draft = session.draft_versions[-1]
        validation_id = draft.validation.validation_id
        if (
            draft.draft_id is None
            or validation_id is None
        ):
            raise ExecutionAuthorizationError(
                "Only the current Validator-Passed draft may be authorized."
            )
        try:
            pinned_validation = WorkflowDraftRepository(
                self._storage
            ).get_validation_version(draft.draft_id, validation_id)
        except (LookupError, ValueError) as error:
            raise ExecutionAuthorizationError(
                "Only the current Validator-Passed draft may be authorized."
            ) from error
        if (
            pinned_validation.status is ValidationStatus.FAIL
            or pinned_validation.status.value != draft.validation.status
        ):
            raise ExecutionAuthorizationError(
                "Only the current Validator-Passed draft may be authorized."
            )
        now = _utc(self._clock())
        # Advisory diagnostics no longer gate execution; they are surfaced
        # with the answer and covered by the accept/reject decision.
        advisory_override = (
            AdvisoryOverride(
                actor_principal_id=owner_principal_id,
                acknowledged_at=now,
                diagnostic_codes=pinned_validation.advisory_diagnostic_codes,
            )
            if pinned_validation.status is ValidationStatus.PASS_WITH_WARNINGS
            else None
        )
        authorization = ExecutionAuthorization(
            session_id=session.session_id,
            actor_principal_id=owner_principal_id,
            authorized_at=now,
            expires_at=session.expires_at,
            draft_version=draft.version,
            draft_version_id=draft.draft_version_id,
            draft_id=draft.draft_id,
            validation_id=validation_id,
            advisory_override=advisory_override,
        )
        job = ExecutionJob(
            job_id=self._job_id_factory(),
            session_id=session.session_id,
            owner_principal_id=owner_principal_id,
            authorization=authorization,
            draft_id=draft.draft_id,
            validation_id=validation_id,
            status=ExecutionJobStatus.QUEUED,
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
                "job_reference": ExecutionJobReference(
                    job_id=job.job_id,
                    status=ExecutionJobStatus.QUEUED,
                ),
            }
        )
        try:
            self._repository.update(updated, expected_etag=expected_etag)
        except SessionPreconditionError:
            self._fail_job(
                job,
                code="authorization-state-conflict",
                message="Question Session changed before execution.",
                at=now,
            )
            raise
        # In-process execution: the worker runs the plan now and its session
        # sink folds the terminal job and Candidate Answer back into the
        # session, so the fresh state is re-read for the response.
        self._execution_worker.execute(job.job_id)
        session, etag = self._repository.get(
            session_id,
            owner_principal_id=owner_principal_id,
        )
        if repair_budget > 0 and _answer_repair_needed(session):
            return self._revise(
                session_id,
                owner_principal_id=owner_principal_id,
                expected_etag=etag,
                trigger="auto_repair",
                instruction=None,
                repair_budget=repair_budget - 1,
            )
        return session, etag

    def _fail_job(
        self,
        job: ExecutionJob,
        *,
        code: str,
        message: str,
        at: datetime,
    ) -> ExecutionJob:
        return self._execution_repository.transition(
            job,
            ExecutionJobStatus.FAILED,
            at=at,
            failure=ExecutionFailure(code=code, message=message),
        )

    def _revise(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
        expected_etag: str,
        trigger: FeedbackAction,
        instruction: str | None,
        repair_budget: int,
    ) -> tuple[QuestionSession, str]:
        """Plan a new draft version, superseding all execution-phase state."""

        session, current_etag = self._repository.get(
            session_id,
            owner_principal_id=owner_principal_id,
        )
        self._ensure_active(session)
        if session.result_decision is not None:
            raise SessionStateTransitionError(
                "A reviewed session cannot be revised."
            )
        if current_etag != expected_etag:
            raise SessionPreconditionError(
                "Question Session changed after it was inspected."
            )
        version = session.current_draft_version + 1
        drafts = session.draft_versions
        current = drafts[-1]
        now = _utc(self._clock())
        draft = self._plan_draft(
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
                "job_reference": None,
                "execution_result": None,
                "candidate_answer": None,
                "candidate_answer_failure": None,
                "result_review_history": _append_result_decision(
                    session.result_review_history,
                    session.result_decision,
                ),
                "result_decision": None,
            }
        )
        etag = self._repository.update(updated, expected_etag=expected_etag)
        LOGGER.info(
            "question_session.revised session=%s version=%s trigger=%s status=%s",
            session.session_id,
            draft.version,
            trigger,
            draft.validation.status,
        )
        if draft.validation.status != "fail":
            return self._execute_current_draft(
                session_id,
                owner_principal_id=owner_principal_id,
                expected_etag=etag,
                repair_budget=repair_budget,
            )
        return updated, etag

    def _plan_draft(
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
        """Interpret and plan with separate bounded diagnostic repair loops."""

        catalog_version = CatalogReader(self._storage).current().version
        interpretation_failed_attempts: list[dict[str, object]] = []
        interpretation_repair_context: Mapping[str, object] | None = None
        for interpretation_attempt in range(
            1, MAX_INTERPRETATION_ATTEMPTS + 1
        ):
            interpretation = self._interpreter.interpret(
                question,
                catalog_version=catalog_version,
                repair_context=interpretation_repair_context,
            )
            if isinstance(interpretation, SupportedInterpretation):
                break
            interpretation_failed_attempts.append(
                {
                    "diagnostic_codes": list(
                        interpretation_diagnostic_codes(interpretation)
                    )
                }
            )
            if (
                interpretation_attempt == MAX_INTERPRETATION_ATTEMPTS
                or not is_repairable_interpretation_failure(interpretation)
            ):
                return _unsupported_draft_document(
                    interpretation,
                    version=version,
                    trigger=trigger,
                    created_at=created_at,
                    instruction=instruction,
                    interpretation_repair=(
                        {
                            "attempt_count": interpretation_attempt,
                            "failed_attempts": interpretation_failed_attempts,
                        }
                        if interpretation_attempt > 1
                        else None
                    ),
                )
            interpretation_repair_context = (
                interpretation_repair_context_document(
                    interpretation,
                    attempt=interpretation_attempt + 1,
                )
            )
        assert isinstance(interpretation, SupportedInterpretation)
        interpretation_repair = (
            {
                "attempt_count": interpretation_attempt,
                "failed_attempts": interpretation_failed_attempts,
            }
            if interpretation_failed_attempts
            else None
        )
        catalog = CatalogReader(self._storage).get(catalog_version)
        structure = task_structure(
            interpretation.task_specification,
            interpretation.bindings,
            catalog,
        )
        # Tier 1 — retrieval: an exact structural hit replays an accepted
        # workflow without a model call. Edits and regenerations skip the
        # replay (the reviewer asked for a fresh plan) but keep examples.
        if trigger == "submission":
            hit = self._case_base.exact_match(structure)
            if hit is not None:
                case_key, case = hit
                workflow = instantiate_draft(
                    case,
                    interpretation,
                    tool_registry_version=self._tool_registry.version,
                    storage=self._storage,
                )
                validation = self._validator.validate(workflow)
                WorkflowDraftRepository(self._storage).retain_until(
                    workflow.draft_id,
                    expires_at,
                )
                if validation.status is not ValidationStatus.FAIL:
                    return _draft_document(
                        interpretation,
                        workflow,
                        validation,
                        version=version,
                        trigger=trigger,
                        created_at=created_at,
                        instruction=instruction,
                        interpretation_repair=interpretation_repair,
                        planning_source="retrieval",
                        source_case_ref=case_key,
                    )
        case_examples = case_example_documents(
            self._case_base.near_examples(structure)
        )
        base_review_context: dict[str, object] = {}
        if previous_draft is not None:
            base_review_context.update(
                {
                    "instruction": instruction,
                    "previous_draft": previous_draft,
                }
            )
        if answer_failure is not None:
            base_review_context["answer_construction_failure"] = answer_failure
        if execution_failure is not None:
            base_review_context["execution_failure"] = execution_failure
        review_context = base_review_context or None
        failed_attempts: list[dict[str, object]] = []
        skeletons: tuple[AbstractWorkflow, ...] | None = None
        attempt = 0
        while True:
            attempt += 1
            workflow = self._planner.propose(
                interpretation,
                review_context=review_context,
                skeletons=skeletons,
                case_examples=case_examples,
            )
            validation = self._validator.validate(workflow)
            WorkflowDraftRepository(self._storage).retain_until(
                workflow.draft_id,
                expires_at,
            )
            if validation.status is not ValidationStatus.FAIL:
                break
            failed_attempts.append(
                {
                    "draft_id": workflow.draft_id,
                    "diagnostic_codes": [
                        item.code.value for item in validation.diagnostics
                    ],
                }
            )
            if skeletons is None and attempt >= MAX_PLANNING_ATTEMPTS:
                # Escalate: free composition is out of budget, so enumerate
                # well-typed skeletons and let the model select one instead.
                # The failed draft is deliberately NOT carried into the
                # first escalated attempt: repair context anchors the model
                # to the very composition whose type errors triggered this.
                skeletons = skeletons_for_interpretation(
                    interpretation,
                    CatalogReader(self._storage).get(catalog_version),
                    self._abstractions,
                )
                if not skeletons:
                    break
                review_context = base_review_context or None
                continue
            if skeletons is not None and attempt >= (
                MAX_PLANNING_ATTEMPTS + ENUMERATION_PLANNING_ATTEMPTS
            ):
                break
            review_context = {
                **base_review_context,
                "repair": planning_repair_context_document(
                    workflow,
                    validation,
                    attempt=attempt + 1,
                ),
            }
        return _draft_document(
            interpretation,
            workflow,
            validation,
            version=version,
            trigger=trigger,
            created_at=created_at,
            instruction=instruction,
            interpretation_repair=interpretation_repair,
            planning_repair=(
                {
                    "attempt_count": attempt,
                    "failed_attempts": failed_attempts,
                    "escalated": skeletons is not None,
                }
                if failed_attempts
                else None
            ),
            planning_source=(
                "enumeration" if skeletons is not None else "composition"
            ),
        )

    def _ensure_active(self, session: QuestionSession) -> None:
        if _utc(self._clock()) >= session.expires_at:
            raise SessionExpiredError(session.session_id)


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
        "abstract_workflow": asdict(workflow.abstract_workflow.value),
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
        "abstract_workflow": None,
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


def _log_execution_aggregation(
    session: QuestionSession,
    job: ExecutionJob,
) -> None:
    if session.candidate_answer is not None:
        event = "candidate_answer.ready"
    elif session.candidate_answer_failure is not None:
        event = "candidate_answer.failed"
    else:
        event = "question_session.execution_recorded"
    LOGGER.info(
        "%s session=%s job=%s status=%s",
        event,
        session.session_id,
        job.job_id,
        job.status.value,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Question Session clock must return an aware datetime.")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def normalize_session_etag(value: str | None) -> str:
    """Normalize one required session If-Match value."""

    if value is None:
        raise SessionPreconditionRequiredError("If-Match is required.")
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] == '"':
        normalized = normalized[1:-1]
    if not normalized:
        raise SessionPreconditionError("If-Match is invalid.")
    return normalized


def _public_etag(data: bytes) -> str:
    """Create an HTTP-safe token independent of adapter-specific ETag syntax."""

    return sha256(data)


def _session_key(session_id: str) -> str:
    return f"{SESSION_PREFIX}/{session_id}.json"


def _answer_repair_needed(session: QuestionSession) -> bool:
    """A validated plan that died at runtime or outside the answer contract."""

    if session.candidate_answer_failure is not None:
        return True
    return (
        session.execution_result is not None
        and session.execution_result.status is ExecutionJobStatus.FAILED
    )


def _append_result_decision(
    history: tuple[ResultDecision, ...],
    decision: ResultDecision | None,
) -> tuple[ResultDecision, ...]:
    if decision is None or (history and history[-1] == decision):
        return history
    return (*history, decision)
