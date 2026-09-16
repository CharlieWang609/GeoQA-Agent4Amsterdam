# SPDX-License-Identifier: GPL-3.0-only

"""Serve the production React and FastAPI application on Azure Container Apps."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
import os
from pathlib import Path

import uvicorn

from app.api.main import create_question_session_app
from app.api.session_repository import QuestionSessionExecutionSink
from catalog_stores import azure_store, required_environment
from geoqa_agent.candidate_answer import CandidateAnswerBuilder
from geoqa_agent.execution import (
    ExecutionWorker,
)
from geoqa_agent.runners import default_runner, runtime_provenance
from geoqa_agent.model_clients import close_all, planning_clients
from geoqa_agent.tool_registry import load_tool_registry

MAX_INPUT_BYTES = 200 * 1024 * 1024


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    code_commit = required_environment("GEOQA_CODE_COMMIT")
    static_directory = Path(
        os.environ.get("GEOQA_STATIC_DIR", "/workspace/app/web/dist")
    )
    storage = azure_store()
    clock = lambda: datetime.now(UTC)  # noqa: E731
    execution_worker = ExecutionWorker(
        storage=storage,
        max_input_bytes=MAX_INPUT_BYTES,
        runner=default_runner(),
        tool_registry=load_tool_registry(),
        clock=clock,
        runtime_provenance=runtime_provenance(code_commit),
        session_sink=QuestionSessionExecutionSink(
            storage,
            candidate_answer_builder=CandidateAnswerBuilder(
                storage=storage,
                evaluated_at=clock,
            ),
        ),
    )
    structured_clients = planning_clients()
    app = create_question_session_app(
        storage=storage,
        structured_clients=structured_clients,
        tool_registry=load_tool_registry(),
        clock=clock,
        execution_worker=execution_worker,
        static_directory=static_directory,
    )
    try:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "8000")),
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    finally:
        close_all(structured_clients)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
