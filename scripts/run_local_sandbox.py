# SPDX-License-Identifier: GPL-3.0-only

"""Serve the real application locally against an in-memory catalog.

Every component is the production one — real interpretation/planning LLM
calls, the full validator, in-process GeoPandas execution, the case base —
only the storage is an in-memory copy of a pinned catalog. Pair it with
the Vite dev server (``npm --prefix app/web run dev``), whose proxy
injects the signed-in Easy Auth header; nothing touches Azure.

The catalog comes from a pickle produced by the benchmark harness
(``--save-store``) or is ingested live from the Amsterdam WFS (one
metadata-annotation LLM call). State lives only in this process: killing
it discards sessions and retained cases.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from app.api.main import create_question_session_app
from app.api.session_repository import QuestionSessionExecutionSink
from catalog_stores import live_store, load_store, save_store
from data_pipeline.storage import InMemoryObjectStore
from geoqa_agent.candidate_answer import CandidateAnswerBuilder
from geoqa_agent.execution import (
    ExecutionWorker,
)
from geoqa_agent.runners import default_runner, runtime_provenance
from geoqa_agent.model_clients import close_all, planning_clients
from geoqa_agent.tool_registry import load_tool_registry

DEFAULT_STORE = Path(".local/catalog-store.pkl")


def build_store(path: Path) -> InMemoryObjectStore:
    if path.exists():
        return load_store(path)[0]
    store, catalog_version = live_store()
    save_store(path, store, catalog_version)
    print(f"cached catalog at {path}", file=sys.stderr)
    return store


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--load-store",
        type=Path,
        default=DEFAULT_STORE,
        help="catalog pickle; ingested live from the WFS when missing",
    )
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()

    store = build_store(arguments.load_store)
    clock = lambda: datetime.now(UTC)  # noqa: E731
    execution_worker = ExecutionWorker(
        storage=store,
        max_input_bytes=200 * 1024 * 1024,
        runner=default_runner(),
        tool_registry=load_tool_registry(),
        clock=clock,
        runtime_provenance=runtime_provenance("local-dev"),
        session_sink=QuestionSessionExecutionSink(
            store,
            candidate_answer_builder=CandidateAnswerBuilder(
                storage=store,
                evaluated_at=clock,
            ),
        ),
    )
    structured_clients = planning_clients()
    try:
        app = create_question_session_app(
            storage=store,
            structured_clients=structured_clients,
            tool_registry=load_tool_registry(),
            clock=clock,
            execution_worker=execution_worker,
        )
        print(
            "local sandbox on http://127.0.0.1:%d — start the UI with "
            "`npm --prefix app/web run dev` and open the Vite URL"
            % arguments.port,
            file=sys.stderr,
        )
        uvicorn.run(app, host="127.0.0.1", port=arguments.port)
    finally:
        close_all(structured_clients)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
