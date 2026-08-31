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
import os
import pickle
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402

from app.api.main import create_question_session_app  # noqa: E402
from app.api.question_sessions import QuestionSessionExecutionSink  # noqa: E402
from data_pipeline.storage import InMemoryObjectStore  # noqa: E402
from geoqa_agent.candidate_answer import CandidateAnswerBuilder  # noqa: E402
from geoqa_agent.execution import (  # noqa: E402
    ExecutionWorker,
    ObjectStorePinnedInputMaterializer,
)
from geoqa_agent.geopandas_runner import (  # noqa: E402
    GeoPandasRunner,
    runtime_provenance,
)
from geoqa_agent.structured_artifacts import OpenAIResponsesClient  # noqa: E402
from geoqa_agent.tool_registry import load_tool_registry  # noqa: E402
from data_pipeline.showcase_catalog import ShowcaseCatalogIngestion  # noqa: E402
from metadata_annotation import MetadataAnnotationJob  # noqa: E402


def live_store(api_key: str) -> tuple[InMemoryObjectStore, str]:
    """Ingest the five showcase layers from the WFS and annotate them."""

    import httpx

    store = InMemoryObjectStore()
    with httpx.Client(timeout=120) as web:
        version = ShowcaseCatalogIngestion(
            store, web, clock=lambda: datetime.now(UTC)
        ).ingest()
    print(f"ingested live catalog {version[:12]}", file=sys.stderr)
    with OpenAIResponsesClient(api_key=api_key) as client:
        annotated = MetadataAnnotationJob(store, client).enrich_current()
    print(f"annotated catalog {annotated[:12]}", file=sys.stderr)
    return store, annotated

DEFAULT_STORE = Path(".local/catalog-store.pkl")


def build_store(arguments: argparse.Namespace, api_key: str) -> InMemoryObjectStore:
    if arguments.load_store.exists():
        objects, catalog_version = pickle.loads(
            arguments.load_store.read_bytes()
        )
        store = InMemoryObjectStore()
        for key, data in objects.items():
            store.put_immutable(key, data)
        print(f"loaded cached catalog {catalog_version[:12]}", file=sys.stderr)
        return store
    store, catalog_version = live_store(api_key)
    arguments.load_store.parent.mkdir(parents=True, exist_ok=True)
    arguments.load_store.write_bytes(
        pickle.dumps((dict(store._objects), catalog_version))
    )
    print(
        f"ingested live catalog {catalog_version[:12]}; "
        f"cached at {arguments.load_store}",
        file=sys.stderr,
    )
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

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit(
            "OPENAI_API_KEY is required (interpretation and planning are real)."
        )
    store = build_store(arguments, api_key)
    clock = lambda: datetime.now(UTC)  # noqa: E731
    execution_worker = ExecutionWorker(
        storage=store,
        tool_registry=load_tool_registry(),
        input_materializer=ObjectStorePinnedInputMaterializer(
            store,
            max_input_bytes=200 * 1024 * 1024,
        ),
        runner=GeoPandasRunner(),
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
    with OpenAIResponsesClient(api_key=api_key) as client:
        app = create_question_session_app(
            storage=store,
            structured_client=client,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
