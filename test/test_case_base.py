# SPDX-License-Identifier: GPL-3.0-only

"""The 4R loop: retain accepted workflows, retrieve them structurally,
replay without a model call, and demote rejected replays."""

from __future__ import annotations

import copy

from conftest import (
    COUNT_INTERPRETATION,
    COUNT_PLAN,
    ScriptedClient,
)
from test_end_to_end_execution import build_service, shapely_count_oracle
from geoqa_agent.case_base import CASE_PREFIX


def accepted_session(storage, client, question, *, session_id):
    """Drive one session from question to an accepted answer."""

    service = build_service(storage, client, session_id=session_id)
    session, etag = service.create(owner_principal_id="tester", question=question)
    assert session.draft_versions[-1].validation.status == "pass"
    assert session.candidate_answer is not None
    session, etag = service.decide_result(
        session.session_id,
        owner_principal_id="tester",
        decision="accepted",
        feedback=None,
        expected_etag=etag,
    )
    return service, session, etag


def test_accepted_workflow_is_retained_and_replayed(storage, catalog_version):
    client_one = ScriptedClient(COUNT_INTERPRETATION, COUNT_PLAN)
    accepted_session(
        storage,
        client_one,
        "Which neighborhoods have no swimming pools?",
        session_id="session-first",
    )
    assert len(client_one.planning_inputs) == 1
    assert storage.list_keys(f"{CASE_PREFIX}/")

    # A second, differently worded question with the same task structure
    # replays the case: zero planning calls, straight to a valid draft.
    client_two = ScriptedClient(COUNT_INTERPRETATION, COUNT_PLAN)
    service = build_service(storage, client_two, session_id="session-second")
    session, etag = service.create(
        owner_principal_id="tester",
        question="List the neighborhoods without any swimming pool.",
    )
    draft = session.draft_versions[-1]
    assert draft.planning_source == "retrieval"
    assert draft.source_case_ref is not None
    assert draft.validation.status == "pass"
    assert client_two.planning_inputs == []

    # The replayed draft executed within the same request.
    answer = session.candidate_answer
    assert answer is not None, session.candidate_answer_failure
    got = {
        (row.identificatie, row.volgnummer): row.count
        for row in answer.result_table
    }
    assert got == shapely_count_oracle()

    # Rejecting the replay removes the case it came from.
    session, _ = service.decide_result(
        session.session_id,
        owner_principal_id="tester",
        decision="rejected",
        feedback="wrong reading",
        expected_etag=etag,
    )
    assert storage.read(draft.source_case_ref) is None


def test_near_hit_feeds_examples_not_replay(storage, catalog_version):
    client_one = ScriptedClient(COUNT_INTERPRETATION, COUNT_PLAN)
    accepted_session(
        storage,
        client_one,
        "Which neighborhoods have no swimming pools?",
        session_id="session-pools",
    )

    # Same structure over a different counted layer: no replay, but the
    # accepted pool workflow arrives as a worked example.
    hall_interpretation = copy.deepcopy(COUNT_INTERPRETATION)
    hall_interpretation["role_requirements"][1]["semantic_label"] = "sports hall"
    hall_interpretation["task_specification"]["counted_objects"][
        "semantic_label"
    ] = "sports hall"
    client_two = ScriptedClient(hall_interpretation, COUNT_PLAN)
    service = build_service(storage, client_two, session_id="session-halls")
    session, _ = service.create(
        owner_principal_id="tester",
        question="Which neighborhoods have no sports halls?",
    )
    draft = session.draft_versions[-1]
    assert draft.planning_source == "composition"
    assert len(client_two.planning_inputs) == 1
    assert "case_examples" in client_two.planning_inputs[0]
    assert "no swimming pools" in client_two.planning_inputs[0]
