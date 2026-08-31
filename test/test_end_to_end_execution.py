# SPDX-License-Identifier: GPL-3.0-only

"""Full pipeline in process: question to accepted answer.

The workflow is freely composed (scripted planner), validated by the real
type-checking validator, executed in process by the GeoPandas runner during
authorization, and the candidate answer is checked against an independent
shapely oracle.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from itertools import count
import math

from conftest import (
    COUNT_INTERPRETATION,
    COUNT_PLAN,
    HALL_POINTS,
    NEAREST_INTERPRETATION,
    NEAREST_PLAN,
    POOL_POINTS,
    SUPPORT_POLYGONS,
    ScriptedClient,
)
from app.api.question_sessions import (
    QuestionSessionExecutionSink,
    QuestionSessionService,
)
from geoqa_agent.candidate_answer import CandidateAnswerBuilder
from geoqa_agent.execution import (
    ExecutionJobStatus,
    ExecutionWorker,
    ObjectStorePinnedInputMaterializer,
)
from geoqa_agent.geopandas_runner import GeoPandasRunner, runtime_provenance
from geoqa_agent.tool_registry import load_tool_registry


def shapely_count_oracle() -> dict[tuple[str, int], int]:
    """Independently compute distinct points strictly within each support."""

    counts = {identity: 0 for identity in SUPPORT_POLYGONS}
    for point in POOL_POINTS.values():
        for identity, polygon in SUPPORT_POLYGONS.items():
            if point.within(polygon):
                counts[identity] += 1
    return counts


def nearest_oracle() -> set[tuple[str, str, float]]:
    """Independent planar nearest pairs; all exact ties retained."""

    pairs: set[tuple[str, str, float]] = set()
    for source_id, source in POOL_POINTS.items():
        best = min(
            math.hypot(source.x - hall.x, source.y - hall.y)
            for hall in HALL_POINTS.values()
        )
        for target_id, hall in HALL_POINTS.items():
            distance = math.hypot(source.x - hall.x, source.y - hall.y)
            if abs(distance - best) < 1e-9:
                pairs.add((source_id, target_id, distance))
    return pairs


def build_service(storage, client, *, session_id="session-e2e"):
    clock = lambda: datetime.now(UTC)  # noqa: E731
    job_numbers = count(1)
    worker = ExecutionWorker(
        storage=storage,
        tool_registry=load_tool_registry(),
        input_materializer=ObjectStorePinnedInputMaterializer(
            storage, max_input_bytes=10 * 1024 * 1024
        ),
        runner=GeoPandasRunner(),
        clock=clock,
        runtime_provenance=runtime_provenance("test"),
        session_sink=QuestionSessionExecutionSink(
            storage,
            candidate_answer_builder=CandidateAnswerBuilder(
                storage=storage,
                evaluated_at=clock,
            ),
        ),
    )
    return QuestionSessionService(
        storage=storage,
        structured_client=client,
        tool_registry=load_tool_registry(),
        clock=clock,
        session_id_factory=lambda: session_id,
        execution_worker=worker,
        job_id_factory=lambda: f"job-{session_id}-{next(job_numbers)}",
    )


def test_nearest_question_end_to_end(storage, catalog_version):
    service = build_service(
        storage, ScriptedClient(NEAREST_INTERPRETATION, NEAREST_PLAN)
    )
    # Creation plans, validates, and executes in one request.
    session, etag = service.create(
        owner_principal_id="tester",
        question="What is the nearest sports hall to each swimming pool?",
    )
    draft = session.draft_versions[-1]
    assert draft.validation.status == "pass", draft.validation.diagnostics
    assert session.execution_result is not None
    assert (
        session.execution_result.status is ExecutionJobStatus.SUCCEEDED
    ), session.execution_result.failure
    answer = session.candidate_answer
    assert answer is not None, session.candidate_answer_failure

    got = {
        (row.source_id, row.target_id, row.distance_m)
        for row in answer.result_table
    }
    # The equidistant pair (p1, h1) and (p1, h2) must both be present.
    assert got == nearest_oracle()


def test_count_selection_is_not_limited_to_zero(storage, catalog_version):
    # "More than one" selects nonzero counts; the answer builder must
    # accept any coherent subset of the result table, not only zeros.
    interpretation = copy.deepcopy(COUNT_INTERPRETATION)
    interpretation["task_specification"]["required_output"] = (
        "neighborhoods with more than one swimming pool"
    )
    plan = copy.deepcopy(COUNT_PLAN)
    plan["concrete_workflow"]["steps"][2]["parameters"][1]["value"] = (
        "object_count > 1"
    )
    service = build_service(
        storage, ScriptedClient(interpretation, plan), session_id="session-multi"
    )
    session, _ = service.create(
        owner_principal_id="tester",
        question="Which neighborhoods have more than one swimming pool?",
    )
    answer = session.candidate_answer
    assert answer is not None, session.candidate_answer_failure
    expected = tuple(
        sorted(
            identity
            for identity, count in shapely_count_oracle().items()
            if count > 1
        )
    )
    assert answer.selected_identities == expected
    assert answer.selected_geometry.feature_count == len(expected)


def test_answer_construction_failure_is_auto_repaired(storage, catalog_version):
    # A plan that validates but exposes 'id' instead of 'source_id' in the
    # result table: execution succeeds, answer construction rejects, and
    # the request replans with the failure as context — no user click.
    broken = copy.deepcopy(NEAREST_PLAN)
    step = copy.deepcopy(broken["concrete_workflow"]["steps"][0])
    step["outputs"][0]["ref"] = "nearest_pairs"
    broken["concrete_workflow"]["steps"] = [step]
    client = ScriptedClient(NEAREST_INTERPRETATION, [broken, NEAREST_PLAN])
    service = build_service(storage, client, session_id="session-repair")

    session, _ = service.create(
        owner_principal_id="tester",
        question="What is the nearest sports hall to each swimming pool?",
    )

    assert len(session.draft_versions) == 2
    assert session.draft_versions[-1].trigger == "auto_repair"
    assert session.candidate_answer is not None, session.candidate_answer_failure
    assert "answer_construction_failure" in client.planning_inputs[1]
    assert "result-shape-mismatch" in client.planning_inputs[1]


def test_count_question_end_to_end(storage, catalog_version):
    service = build_service(
        storage, ScriptedClient(COUNT_INTERPRETATION, COUNT_PLAN)
    )
    # Creation plans, validates, and executes in one request; the returned
    # session already carries the terminal job and its Candidate Answer.
    session, etag = service.create(
        owner_principal_id="tester",
        question="Which neighborhoods have no swimming pools?",
    )
    draft = session.draft_versions[-1]
    assert draft.validation.status == "pass", draft.validation.diagnostics
    assert session.execution_result is not None
    assert (
        session.execution_result.status is ExecutionJobStatus.SUCCEEDED
    ), session.execution_result.failure
    answer = session.candidate_answer
    assert answer is not None, session.candidate_answer_failure

    oracle = shapely_count_oracle()
    got = {
        (row.identificatie, row.volgnummer): row.count
        for row in answer.result_table
    }
    assert got == oracle
    zero_expected = tuple(
        sorted(identity for identity, count in oracle.items() if count == 0)
    )
    assert answer.selected_identities == zero_expected
    unmatched = {
        diagnostic.category: diagnostic.count for diagnostic in answer.diagnostics
    }
    outside = sum(
        1
        for point in POOL_POINTS.values()
        if not any(point.within(polygon) for polygon in SUPPORT_POLYGONS.values())
    )
    assert unmatched.get("unmatched") == outside
    assert str(answer.selected_geometry.feature_count) == str(len(zero_expected))
