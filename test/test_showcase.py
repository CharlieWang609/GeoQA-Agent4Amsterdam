# SPDX-License-Identifier: GPL-3.0-only

"""Worked examples: an accepted session frozen and served without an account."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from itertools import count

import pytest
from conftest import ScriptedClient
from fastapi.testclient import TestClient
from test_end_to_end_execution import ZONAL_INTERPRETATION, ZONAL_PLAN, build_worker

from app.api.main import create_question_session_app
from app.api.showcase import list_showcase_ids, publish_showcase
from geoqa_agent.structured_artifacts import DEFAULT_MODEL
from geoqa_agent.tool_registry import load_tool_registry


OWNER = {"X-MS-CLIENT-PRINCIPAL-ID": "tester"}
QUESTION = "What is the mean elevation of each neighborhood?"


def clipped_zonal_plan() -> dict:
    """The zonal plan with a clip in front, so the execution leaves a
    raster output that the observation listing shows."""

    plan = copy.deepcopy(ZONAL_PLAN)
    steps = plan["concrete_workflow"]["steps"]
    steps.insert(
        0,
        {
            "step_id": "clip-elevation",
            "algorithm_id": "rasterio:clip",
            "parameters": [
                {"name": "input", "source": "ref", "value": "elevation"},
                {"name": "mask", "source": "ref", "value": "supports"},
            ],
            "outputs": [{"name": "output", "ref": "clipped_elevation", "kind": "sink"}],
        },
    )
    steps[1]["parameters"][0] = {"name": "input", "source": "ref", "value": "clipped_elevation"}
    return plan


def build_client(storage, client) -> TestClient:
    clock = lambda: datetime.now(UTC)  # noqa: E731
    jobs = count(1)
    app = create_question_session_app(
        storage=storage,
        structured_clients={DEFAULT_MODEL: client},
        tool_registry=load_tool_registry(),
        clock=clock,
        session_id_factory=lambda: "session-showcase",
        execution_worker=build_worker(storage, clock),
        job_id_factory=lambda: f"job-{next(jobs)}",
    )
    return TestClient(app)


def answered_session(web: TestClient) -> str:
    created = web.post("/api/question-sessions", json={"question": QUESTION}, headers=OWNER)
    assert created.status_code == 201, created.text
    session = created.json()
    assert session["draft_versions"][-1]["validation"]["status"] == "pass", session["draft_versions"][-1]["validation"]
    assert session["candidate_answer"] is not None, session["candidate_answer_failure"]
    return str(session["session_id"])


def accept(web: TestClient, session_id: str) -> None:
    decided = web.post(
        f"/api/question-sessions/{session_id}/result-decision",
        json={"decision": "accepted"},
        headers=OWNER,
    )
    assert decided.status_code == 200, decided.text


def test_an_accepted_session_is_published_for_anonymous_reading(storage, catalog_version):
    web = build_client(storage, ScriptedClient(ZONAL_INTERPRETATION, clipped_zonal_plan()))
    session_id = answered_session(web)
    accept(web, session_id)
    assert web.get("/api/showcase").json() == []

    publish_showcase(storage, session_id)

    listing = web.get("/api/showcase")
    assert listing.status_code == 200
    assert listing.headers["Cache-Control"] == "public, max-age=300"
    assert [(item["session_id"], item["question"], item["has_result_decision"]) for item in listing.json()] == [
        (session_id, QUESTION, True)
    ]

    example = web.get(f"/api/showcase/{session_id}")
    assert example.status_code == 200
    owned = web.get(f"/api/question-sessions/{session_id}", headers=OWNER).json()
    assert example.json()["candidate_answer"] == owned["candidate_answer"]
    assert example.json()["result_decision"]["decision"] == "accepted"
    # Every GitHub principal is gone from the public copy.
    assert example.json()["owner_principal_id"] == "showcase"
    assert example.json()["result_decision"]["actor_principal_id"] == "showcase"
    assert example.json()["execution_authorization"]["actor_principal_id"] == "showcase"
    assert "tester" not in example.text

    answer_map = web.get(f"/api/showcase/{session_id}/answer-map").json()
    assert [feature["properties"]["value"] for feature in answer_map["features"]] == pytest.approx([0.5, 1.5, 2.5])

    observations = web.get(f"/api/showcase/{session_id}/observations").json()["items"]
    assert [(item["origin"], item["view_url"]) for item in observations] == [
        ("input", "/api/catalog-layers/ahn/dsm/raster-view"),
        ("step", f"/api/showcase/{session_id}/observations/clipped_elevation"),
    ]
    view = web.get(f"/api/showcase/{session_id}/observations/clipped_elevation")
    assert view.status_code == 200
    assert view.json()["kind"] == "raster"

    # The frozen documents never expire, unlike the sandbox artifacts they
    # were rendered from.
    assert not [key for key in storage._expiries if key.startswith("showcase/")]
    assert [key for key in storage._expiries if key.startswith("question-sessions/")]

    # The owned routes stay behind the account, and unknown examples are 404.
    assert web.get(f"/api/question-sessions/{session_id}").status_code == 401
    assert web.get("/api/showcase/unknown").status_code == 404
    assert web.get(f"/api/showcase/{session_id}/observations/unknown").status_code == 404


def test_only_accepted_answers_qualify_and_republishing_replaces_in_place(storage, catalog_version):
    web = build_client(storage, ScriptedClient(ZONAL_INTERPRETATION, ZONAL_PLAN))
    session_id = answered_session(web)
    with pytest.raises(ValueError, match="accepted"):
        publish_showcase(storage, session_id)

    accept(web, session_id)
    publish_showcase(storage, session_id)
    publish_showcase(storage, session_id)
    assert list_showcase_ids(storage) == [session_id]
    assert len(web.get("/api/showcase").json()) == 1
