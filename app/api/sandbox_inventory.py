# SPDX-License-Identifier: GPL-3.0-only

"""Read-only operator inventory over current Live Sandbox sessions."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.api.session_models import QuestionSession
from data_pipeline.storage import ObjectStore
from geoqa_agent.execution import ExecutionRepository


SESSION_PREFIX = "question-sessions/"


class SandboxSessionInventory(BaseModel):
    """One current Question Session and its linked lifecycle state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    principal_id: str
    question: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    validation_status: str
    job_id: str | None
    job_status: str
    result_status: str
    workflow_id: str | None


def inspect_sandbox(storage: ObjectStore) -> tuple[SandboxSessionInventory, ...]:
    """List every current session, newest first."""

    jobs = ExecutionRepository(storage)
    entries: list[SandboxSessionInventory] = []
    for key in storage.list_keys(SESSION_PREFIX):
        relative = key.removeprefix(SESSION_PREFIX)
        if "/" in relative or not relative.endswith(".json"):
            continue
        stored = storage.read(key)
        if stored is None:
            continue
        session = QuestionSession.model_validate_json(stored.data)
        draft = session.draft_versions[-1]
        job = (
            None
            if session.job_reference is None
            else jobs.get_job(session.job_reference.job_id)
        )
        entries.append(
            SandboxSessionInventory(
                session_id=session.session_id,
                principal_id=session.owner_principal_id,
                question=session.question,
                created_at=session.created_at,
                updated_at=session.updated_at,
                expires_at=session.expires_at,
                validation_status=draft.validation.status,
                job_id=None if job is None else job.job_id,
                job_status="none" if job is None else job.status.value,
                result_status=_result_status(session),
                workflow_id=(
                    None
                    if session.result_decision is None
                    else session.result_decision.workflow_id
                ),
            )
        )
    return tuple(
        sorted(
            entries,
            key=lambda entry: (entry.updated_at, entry.session_id),
            reverse=True,
        )
    )


def _result_status(session: QuestionSession) -> str:
    if session.result_decision is not None:
        return session.result_decision.decision
    if session.candidate_answer is not None:
        return "ready"
    if session.candidate_answer_failure is not None:
        return "failed"
    return "none"
