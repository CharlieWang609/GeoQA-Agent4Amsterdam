# SPDX-License-Identifier: GPL-3.0-only

"""Question Session persistence and the execution sink that folds results in."""

from __future__ import annotations

import logging
from datetime import timedelta

from app.api.session_models import QuestionSession
from data_pipeline.serialization import canonical_json
from data_pipeline.storage import ObjectStore
from geoqa_agent.candidate_answer import (
    CandidateAnswerBuilder,
    CandidateAnswerRejected,
)
from geoqa_agent.execution import ExecutionJob, ExecutionJobStatus


SESSION_RETENTION = timedelta(days=7)
SESSION_PREFIX = "question-sessions"
LOGGER = logging.getLogger(__name__)


class SessionNotFoundError(LookupError):
    """The session does not exist or is not visible to this principal."""


class SessionExpiredError(LookupError):
    """The temporary Question Session has passed its retention deadline."""


class SessionStateTransitionError(ValueError):
    """The requested review action is invalid for the current session state."""


class QuestionSessionRepository:
    """Persist each Question Session as one JSON document.

    The sandbox has a single writer per session (the request that owns it),
    so documents are replaced without optimistic-concurrency tokens.
    """

    def __init__(self, storage: ObjectStore) -> None:
        self._storage = storage

    def get(
        self,
        session_id: str,
        *,
        owner_principal_id: str,
    ) -> QuestionSession:
        stored = self._storage.read(_session_key(session_id))
        if stored is None:
            raise SessionNotFoundError(session_id)
        session = QuestionSession.model_validate_json(stored.data)
        if session.owner_principal_id != owner_principal_id:
            raise SessionNotFoundError(session_id)
        return session

    def save(self, session: QuestionSession) -> None:
        key = _session_key(session.session_id)
        current = self._storage.read(key)
        self._storage.compare_and_swap(
            key,
            canonical_json(session.model_dump(mode="json")),
            None if current is None else current.etag,
        )
        self._storage.set_expiry(key, session.expires_at)

    def delete(self, session: QuestionSession) -> None:
        self._storage.delete(_session_key(session.session_id))

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
        candidate_answer_builder: CandidateAnswerBuilder | None = None,
    ) -> None:
        self._repository = QuestionSessionRepository(storage)
        self._candidate_answer_builder = candidate_answer_builder

    def record(self, job: ExecutionJob) -> None:
        """Fold a terminal job (and its Candidate Answer) into the session."""

        session = self._repository.get(
            job.session_id,
            owner_principal_id=job.owner_principal_id,
        )
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
                "execution_result": job,
                "candidate_answer": candidate_answer,
                "candidate_answer_failure": candidate_answer_failure,
                "result_decision": None,
            }
        )
        self._repository.save(updated)
        _log_execution_aggregation(updated, job)


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


def _session_key(session_id: str) -> str:
    return f"{SESSION_PREFIX}/{session_id}.json"
