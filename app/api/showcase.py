# SPDX-License-Identifier: GPL-3.0-only

"""Worked examples: accepted Question Sessions frozen for anonymous reading.

Publishing renders every document the browser needs to show a session —
the session itself, its Answer Map, its observation listing and the
observation views — once, and stores them under ``showcase/``. Nothing
there expires and nothing is computed when a visitor opens an example, so
the examples cost nothing to serve and outlive the 7-day sandbox
artifacts they were rendered from.
"""

from __future__ import annotations

import json
from typing import Any, cast

from app.api.answer_maps import build_answer_map
from app.api.raster_views import build_observation_listing, build_observation_view
from app.api.session_models import QuestionSession, QuestionSessionSummary
from app.api.session_repository import SESSION_PREFIX, SessionNotFoundError
from data_pipeline.serialization import canonical_json
from data_pipeline.storage import ObjectStore


SHOWCASE_PREFIX = "showcase"
# Replaces every GitHub principal id in the frozen documents.
SHOWCASE_PRINCIPAL = "showcase"
INDEX_KEY = f"{SHOWCASE_PREFIX}/index.json"


class ShowcaseNotFoundError(LookupError):
    """No worked example (or no such document of one) is published there."""


def publish_showcase(storage: ObjectStore, session_id: str) -> QuestionSession:
    """Freeze one accepted sandbox session into a worked example; publishing
    the same session again replaces its documents in place."""

    stored = storage.read(f"{SESSION_PREFIX}/{session_id}.json")
    if stored is None:
        raise SessionNotFoundError(session_id)
    session = QuestionSession.model_validate_json(stored.data)
    decision = session.result_decision
    if decision is None or decision.decision != "accepted":
        raise ValueError(
            f"Session {session_id} has no accepted Candidate Answer to publish."
        )
    listing = build_observation_listing(storage, session)
    documents: dict[str, object] = {
        "session.json": _without_principals(session.model_dump(mode="json")),
        "answer-map.json": build_answer_map(storage, session),
        "observations.json": listing,
    }
    private_views = f"/api/question-sessions/{session_id}/observations/"
    for item in cast(list[dict[str, Any]], listing["items"]):
        if not str(item["view_url"]).startswith(private_views):
            continue  # a Catalog layer view, anonymous already
        ref = str(item["ref"])
        item["view_url"] = f"/api/{SHOWCASE_PREFIX}/{session_id}/observations/{ref}"
        documents[f"observations/{ref}.json"] = build_observation_view(
            storage, session, ref
        )
    for name, document in documents.items():
        _replace(storage, f"{SHOWCASE_PREFIX}/{session_id}/{name}", document)
    published = list_showcase_ids(storage)
    if session_id not in published:
        _replace(storage, INDEX_KEY, [*published, session_id])
    return session


def list_showcase_ids(storage: ObjectStore) -> list[str]:
    stored = storage.read(INDEX_KEY)
    return [] if stored is None else [str(item) for item in json.loads(stored.data)]


def list_showcase(storage: ObjectStore) -> list[QuestionSessionSummary]:
    """The published examples in publication order."""

    return [
        QuestionSessionSummary.of(
            QuestionSession.model_validate(
                read_showcase_document(storage, session_id, "session.json")
            )
        )
        for session_id in list_showcase_ids(storage)
    ]


def read_showcase_document(
    storage: ObjectStore,
    session_id: str,
    name: str,
) -> dict[str, Any]:
    stored = storage.read(f"{SHOWCASE_PREFIX}/{session_id}/{name}")
    if stored is None:
        raise ShowcaseNotFoundError(f"{session_id}/{name}")
    document: dict[str, Any] = json.loads(stored.data)
    return document


def _replace(storage: ObjectStore, key: str, document: object) -> None:
    current = storage.read(key)
    storage.compare_and_swap(
        key,
        canonical_json(document),
        None if current is None else current.etag,
    )


def _without_principals(value: Any) -> Any:
    """The document with every ``*principal_id`` replaced: the examples are
    public, the reviewer's GitHub identity is not."""

    if isinstance(value, dict):
        return {
            key: SHOWCASE_PRINCIPAL if key.endswith("principal_id") else _without_principals(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_principals(item) for item in value]
    return value
