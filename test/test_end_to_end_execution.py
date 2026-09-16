# SPDX-License-Identifier: GPL-3.0-only

"""Full pipeline in process: question to accepted answer.

The workflow is freely composed (scripted planner), validated by the real
validator, executed in process by the GeoPandas runner during
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
    attribute,
    geoparquet_bytes,
    make_layer,
)
from shapely.geometry import Polygon

from data_pipeline.catalog import CatalogPublisher, CatalogReader
from app.api.question_sessions import QuestionSessionService
from app.api.session_repository import QuestionSessionExecutionSink
from geoqa_agent.candidate_answer import CandidateAnswerBuilder
from geoqa_agent.execution import (
    ExecutionJobStatus,
    ExecutionWorker,
)
from geoqa_agent.runners import default_runner, runtime_provenance
from geoqa_agent.structured_artifacts import DEFAULT_MODEL
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


def build_worker(storage, clock):
    return ExecutionWorker(
        storage=storage,
        max_input_bytes=10 * 1024 * 1024,
        runner=default_runner(),
        tool_registry=load_tool_registry(),
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


def build_service(storage, client, *, session_id="session-e2e"):
    clock = lambda: datetime.now(UTC)  # noqa: E731
    job_numbers = count(1)
    return QuestionSessionService(
        storage=storage,
        structured_clients={DEFAULT_MODEL: client},
        tool_registry=load_tool_registry(),
        clock=clock,
        session_id_factory=lambda: session_id,
        execution_worker=build_worker(storage, clock),
        job_id_factory=lambda: f"job-{session_id}-{next(job_numbers)}",
    )


def test_nearest_question_end_to_end(storage, catalog_version):
    service = build_service(
        storage, ScriptedClient(NEAREST_INTERPRETATION, NEAREST_PLAN)
    )
    # Creation plans, validates, and executes in one request.
    session = service.create(
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

    got = {(row.keys[0], row.keys[1], row.value) for row in answer.result_table}
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
    session = service.create(
        owner_principal_id="tester",
        question="Which neighborhoods have more than one swimming pool?",
    )
    answer = session.candidate_answer
    assert answer is not None, session.candidate_answer_failure
    expected = tuple(
        sorted(
            (identificatie, str(volgnummer))
            for (identificatie, volgnummer), count in shapely_count_oracle().items()
            if count > 1
        )
    )
    assert answer.selected_keys == expected
    assert answer.selected_geometry.feature_count == len(expected)


def test_result_contract_columns_are_repaired_before_execution(storage, catalog_version):
    # A plan that exposes 'id' instead of 'source_points_id' in the result
    # table fails validation with the contract columns named, and the
    # planning repair loop fixes it before anything runs.
    broken = copy.deepcopy(NEAREST_PLAN)
    step = copy.deepcopy(broken["concrete_workflow"]["steps"][0])
    step["outputs"][0]["ref"] = "nearest_pairs"
    broken["concrete_workflow"]["steps"] = [step]
    client = ScriptedClient(NEAREST_INTERPRETATION, [broken, NEAREST_PLAN])
    service = build_service(storage, client, session_id="session-repair")

    session = service.create(
        owner_principal_id="tester",
        question="What is the nearest sports hall to each swimming pool?",
    )

    assert len(session.draft_versions) == 1
    draft = session.draft_versions[-1]
    assert draft.validation.status == "pass"
    assert draft.planning_repair is not None
    assert draft.planning_repair.attempt_count == 2
    assert session.candidate_answer is not None, session.candidate_answer_failure
    assert "result-contract columns ['source_points_id']" in client.planning_inputs[1]


def test_count_question_end_to_end(storage, catalog_version):
    service = build_service(
        storage, ScriptedClient(COUNT_INTERPRETATION, COUNT_PLAN)
    )
    # Creation plans, validates, and executes in one request; the returned
    # session already carries the terminal job and its Candidate Answer.
    session = service.create(
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

    oracle = {
        (identificatie, str(volgnummer)): count
        for (identificatie, volgnummer), count in shapely_count_oracle().items()
    }
    got = {row.keys: int(row.value) for row in answer.result_table}
    assert got == oracle
    zero_expected = tuple(
        sorted(identity for identity, count in oracle.items() if count == 0)
    )
    assert answer.selected_keys == zero_expected
    unmatched = {
        diagnostic.category: diagnostic.count for diagnostic in answer.diagnostics
    }
    outside = sum(
        1
        for point in POOL_POINTS.values()
        if not any(point.within(polygon) for polygon in SUPPORT_POLYGONS.values())
    )
    assert unmatched.get("unmatched_points") == outside
    assert str(answer.selected_geometry.feature_count) == str(len(zero_expected))


ZONAL_INTERPRETATION = {
    **COUNT_INTERPRETATION,
    "task_specification": {
        **COUNT_INTERPRETATION["task_specification"],
        "required_output": "mean elevation per neighborhood",
        "roles": [
            COUNT_INTERPRETATION["task_specification"]["roles"][0],
            {
                "role": "elevation",
                "semantic_label": "elevation model",
                "identity_fields": [],
                "data_kind": "raster",
                "geometry_types": [],
            },
        ],
        "goal": {"per": ["supports"], "value_name": "mean_elevation", "value_scale": "numeric", "aggregation": "mean", "value_attribute": "elevation", "selection": False},
        "constraints": [],
        "target_transformation": ["mean elevation per neighborhood"],
    },
}
ZONAL_PLAN = {
    "concrete_workflow": {
        "steps": [
            {
                "step_id": "mean-elevation",
                "algorithm_id": "rasterio:zonalstatistics",
                "parameters": [
                    {"name": "input", "source": "ref", "value": "elevation"},
                    {"name": "zones", "source": "ref", "value": "supports"},
                    {"name": "statistic", "source": "literal", "value": "mean"},
                    {"name": "output_field", "source": "literal", "value": "mean_elevation"},
                ],
                "outputs": [{"name": "output", "ref": "support_elevation", "kind": "sink"}],
            }
        ],
        "final_output_ref": "support_elevation",
        "result_table_ref": "support_elevation",
        "diagnostic_refs": [],
    },
}


def test_raster_zonal_mean_end_to_end(storage, catalog_version):
    service = build_service(storage, ScriptedClient(ZONAL_INTERPRETATION, ZONAL_PLAN))
    session = service.create(
        owner_principal_id="tester",
        question="What is the mean elevation of each neighborhood?",
    )
    draft = session.draft_versions[-1]
    assert draft.validation.status == "pass", draft.validation.diagnostics
    assert session.execution_result is not None
    assert session.execution_result.status is ExecutionJobStatus.SUCCEEDED, session.execution_result.failure
    answer = session.candidate_answer
    assert answer is not None, session.candidate_answer_failure
    got = {row.keys: round(row.value, 6) for row in answer.result_table}
    assert got == {("B1", "1"): 0.5, ("B2", "1"): 1.5, ("B3", "2"): 2.5}


def test_count_answer_follows_the_support_identity_fields(storage, catalog_version):
    # A support layer whose identity is a single ``code`` column: the answer
    # must key its rows by the task's identity fields, not by the
    # neighbourhood columns of the original showcase question.
    districts = {
        "D1": Polygon([(0, 0), (200, 0), (200, 100), (0, 100)]),
        "D2": Polygon([(200, 0), (300, 0), (300, 100), (200, 100)]),
    }
    layer = make_layer(
        storage,
        "gebieden",
        "wijken",
        label="district",
        geometry=("Polygon",),
        identity=("code",),
        attrs=[attribute("code"), attribute("naam")],
        data=geoparquet_bytes(
            {"code": list(districts), "naam": [f"Wijk {k}" for k in districts]},
            list(districts.values()),
        ),
    )
    current = CatalogReader(storage).current()
    CatalogPublisher(storage).publish_snapshot((*current.layers, layer))
    interpretation = copy.deepcopy(COUNT_INTERPRETATION)
    interpretation["task_specification"]["required_output"] = "districts with zero swimming pools"
    interpretation["task_specification"]["roles"][0].update(
        {"semantic_label": "district", "identity_fields": ["code"]}
    )
    service = build_service(storage, ScriptedClient(interpretation, COUNT_PLAN))

    session = service.create(
        owner_principal_id="tester",
        question="Which districts have no swimming pools?",
    )

    answer = session.candidate_answer
    assert answer is not None, session.candidate_answer_failure
    assert answer.selected_geometry.feature_identity_fields == ("code",)
    assert {row.keys: int(row.value) for row in answer.result_table} == {
        ("D1",): 3,
        ("D2",): 0,
    }
    assert answer.selected_keys == (("D2",),)
