# SPDX-License-Identifier: GPL-3.0-only

"""Serve the production React and FastAPI application on Azure Container Apps."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
import os
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
import uvicorn

from app.api.main import create_question_session_app
from app.api.question_sessions import QuestionSessionExecutionSink
from data_pipeline.azure_storage import AzureBlobObjectStore
from geoqa_agent.candidate_answer import CandidateAnswerBuilder
from geoqa_agent.execution import (
    ExecutionWorker,
    ObjectStorePinnedInputMaterializer,
)
from geoqa_agent.geopandas_runner import GeoPandasRunner, runtime_provenance
from geoqa_agent.structured_artifacts import OpenAIResponsesClient
from geoqa_agent.tool_registry import load_tool_registry

MAX_INPUT_BYTES = 200 * 1024 * 1024


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    account_name = _required_environment("AZURE_STORAGE_ACCOUNT_NAME")
    container_name = _required_environment("DATA_FILESYSTEM_NAME")
    api_key = _required_environment("OPENAI_API_KEY")
    code_commit = _required_environment("GEOQA_CODE_COMMIT")
    static_directory = Path(
        os.environ.get("GEOQA_STATIC_DIR", "/workspace/app/web/dist")
    )
    credential = DefaultAzureCredential()
    container = BlobServiceClient(
        account_url=f"https://{account_name}.blob.core.windows.net",
        credential=credential,
    ).get_container_client(container_name)
    storage = AzureBlobObjectStore(container)
    clock = lambda: datetime.now(UTC)  # noqa: E731
    execution_worker = ExecutionWorker(
        storage=storage,
        tool_registry=load_tool_registry(),
        input_materializer=ObjectStorePinnedInputMaterializer(
            storage,
            max_input_bytes=MAX_INPUT_BYTES,
        ),
        runner=GeoPandasRunner(),
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
    structured_client = OpenAIResponsesClient(api_key=api_key)
    app = create_question_session_app(
        storage=storage,
        structured_client=structured_client,
        tool_registry=load_tool_registry(),
        clock=clock,
        execution_worker=execution_worker,
        static_directory=static_directory,
    )
    try:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=_positive_integer_environment("PORT", default=8000),
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    finally:
        structured_client.close()
        credential.close()
    return 0


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required.")
    return value.strip()


def _positive_integer_environment(name: str, *, default: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None and default is not None:
        return default
    value = _required_environment(name)
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer.") from error
    if parsed <= 0:
        raise RuntimeError(f"{name} must be positive.")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
