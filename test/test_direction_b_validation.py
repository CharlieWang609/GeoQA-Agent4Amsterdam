# SPDX-License-Identifier: GPL-3.0-only

"""Free workflow composition is judged by types, not by an answer key."""

from __future__ import annotations

import copy
import json

import pytest

from conftest import (
    ABSTR,
    COUNT_INTERPRETATION,
    COUNT_PLAN,
    NEAREST_INTERPRETATION,
    NEAREST_PLAN,
    ScriptedClient,
)
from geoqa_agent.question_interpretation import (
    QuestionInterpretationAndMatchingService,
    SupportedInterpretation,
    UnsupportedInterpretation,
)
from geoqa_agent.tool_registry import load_tool_registry
from geoqa_agent.workflow_planning import (
    ValidationStatus,
    WorkflowPlanningService,
    WorkflowValidator,
)

REGISTRY = load_tool_registry()


def plan_and_validate(storage, catalog_version, interpretation_data, plan_data):
    client = ScriptedClient(interpretation_data, plan_data)
    interpreter = QuestionInterpretationAndMatchingService(
        storage=storage, client=client
    )
    interpretation = interpreter.interpret(
        "test question", catalog_version=catalog_version
    )
    assert isinstance(interpretation, SupportedInterpretation), interpretation
    planner = WorkflowPlanningService(
        storage=storage, client=client, tool_registry=REGISTRY
    )
    draft = planner.propose(interpretation)
    validator = WorkflowValidator(storage=storage, tool_registry=REGISTRY)
    return client, draft, validator.validate(draft)


def test_count_composition_passes(storage, catalog_version):
    client, draft, result = plan_and_validate(
        storage, catalog_version, COUNT_INTERPRETATION, COUNT_PLAN
    )
    assert result.status is ValidationStatus.PASS, [
        d.message for d in result.diagnostics
    ]
    # The planning input is a vocabulary, never an answer.
    planning_input = json.loads(client.planning_inputs[0])
    assert len(planning_input["abstractions"]) == 55
    assert len(planning_input["operations"]) == 18
    assert "expected" not in client.planning_inputs[0]
    assert "verbatim" not in client.planning_inputs[0].lower()


def test_grouping_accepts_any_boundary_input_order(storage, catalog_version):
    # A concretization may touch the tessellation before the points (for
    # example an active-supports filter first); only the ref set is checked.
    plan = copy.deepcopy(COUNT_PLAN)
    step = plan["concrete_workflow"]["steps"][0]
    by_name = {parameter["name"]: parameter for parameter in step["parameters"]}
    step["parameters"] = [
        by_name["join"],
        by_name["input"],
        by_name["predicate"],
        by_name["method"],
        by_name["discard_nonmatching"],
        by_name["prefix"],
    ]
    _, _, result = plan_and_validate(
        storage, catalog_version, COUNT_INTERPRETATION, plan
    )
    assert result.status is ValidationStatus.PASS, [
        d.message for d in result.diagnostics
    ]


def test_nearest_composition_passes(storage, catalog_version):
    _, _, result = plan_and_validate(
        storage, catalog_version, NEAREST_INTERPRETATION, NEAREST_PLAN
    )
    assert result.status is ValidationStatus.PASS, [
        d.message for d in result.diagnostics
    ]


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        pytest.param(
            lambda plan: (
                plan["abstract_workflow"]["steps"][0].update(
                    input_refs=["supports", "counted_objects"]
                ),
                plan["concrete_workflow"]["steps"][0]["parameters"][0].update(
                    value="supports"
                ),
                plan["concrete_workflow"]["steps"][0]["parameters"][2].update(
                    value="counted_objects"
                ),
                plan["concrete_workflow"]["steps"][1]["parameters"][0].update(
                    value="counted_objects"
                ),
            ),
            "incompatible-type",
            id="point-layer-into-tessellation-input",
        ),
        pytest.param(
            lambda plan: plan["abstract_workflow"]["steps"][0].update(
                abstraction_id=f"{ABSTR}MadeUpOperation"
            ),
            "unavailable-algorithm",
            id="unknown-abstraction",
        ),
        pytest.param(
            lambda plan: plan["concrete_workflow"]["steps"][2].update(
                algorithm_id="geopandas:notanoperation"
            ),
            "unavailable-algorithm",
            id="unlisted-operation",
        ),
        pytest.param(
            lambda plan: plan["concrete_workflow"]["steps"][2]["parameters"]
            .__setitem__(
                1,
                {
                    "name": "expression",
                    "source": "template",
                    "value": "x < {nonexistent_placeholder}",
                },
            ),
            "invalid-parameter",
            id="unknown-template-placeholder",
        ),
        pytest.param(
            lambda plan: plan["concrete_workflow"]["steps"][0]["parameters"][1]
            .update(value=99),
            "invalid-parameter",
            id="disallowed-enum-value",
        ),
        pytest.param(
            lambda plan: plan["concrete_workflow"].update(
                diagnostic_refs=["never_produced"]
            ),
            "disconnected-reference",
            id="undeclared-output-ref",
        ),
        pytest.param(
            lambda plan: plan["concrete_workflow"].update(
                diagnostic_refs=["joined_count"]
            ),
            "disconnected-reference",
            id="result-kind-ref-retained",
        ),
    ],
)
def test_bad_compositions_are_blocked(storage, catalog_version, mutate, expected_code):
    plan = copy.deepcopy(COUNT_PLAN)
    mutate(plan)
    _, _, result = plan_and_validate(
        storage, catalog_version, COUNT_INTERPRETATION, plan
    )
    assert result.status is ValidationStatus.FAIL
    assert any(d.code.value == expected_code for d in result.diagnostics), [
        (d.code.value, d.message) for d in result.diagnostics
    ]


def test_declared_cutoff_must_reach_the_plan(storage, catalog_version):
    interpretation = copy.deepcopy(NEAREST_INTERPRETATION)
    interpretation["task_specification"]["distance"]["maximum_distance_m"] = 500

    ignored = copy.deepcopy(NEAREST_PLAN)
    _, _, result = plan_and_validate(
        storage, catalog_version, interpretation, ignored
    )
    assert result.status is ValidationStatus.FAIL
    assert any(d.code.value == "missing-parameter" for d in result.diagnostics)

    bound = copy.deepcopy(NEAREST_PLAN)
    bound["concrete_workflow"]["steps"][0]["parameters"].append(
        {"name": "max_distance", "source": "literal", "value": 500}
    )
    _, _, result = plan_and_validate(
        storage, catalog_version, interpretation, bound
    )
    assert result.status is ValidationStatus.PASS, [
        d.message for d in result.diagnostics
    ]


def test_ambiguous_interpretation_is_unsupported(storage, catalog_version):
    data = copy.deepcopy(COUNT_INTERPRETATION)
    data["unresolved_ambiguities"] = ["which kind of pool is meant"]
    client = ScriptedClient(data, COUNT_PLAN)
    interpreter = QuestionInterpretationAndMatchingService(
        storage=storage, client=client
    )
    interpretation = interpreter.interpret(
        "ambiguous question", catalog_version=catalog_version
    )
    assert isinstance(interpretation, UnsupportedInterpretation)
    assert interpretation.failed_roles[0].role == "interpretation"


def test_wrong_label_fails_grounding_with_candidates(storage, catalog_version):
    data = copy.deepcopy(COUNT_INTERPRETATION)
    data["role_requirements"][1]["semantic_label"] = "ice rink"
    client = ScriptedClient(data, COUNT_PLAN)
    interpreter = QuestionInterpretationAndMatchingService(
        storage=storage, client=client
    )
    interpretation = interpreter.interpret(
        "count ice rinks", catalog_version=catalog_version
    )
    assert isinstance(interpretation, UnsupportedInterpretation)
    failed = interpretation.failed_roles[0]
    assert failed.role == "counted_objects"
    assert failed.closest_candidates  # ranked near-misses for the repair loop
