# SPDX-License-Identifier: GPL-3.0-only

"""Free workflow composition is judged by contracts and dataflow, not by an answer key."""

from __future__ import annotations

import copy
import json

import pytest

from conftest import (
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
from geoqa_agent.workflow_models import DiagnosticCode, ValidationStatus
from geoqa_agent.workflow_validation import WorkflowValidator
from geoqa_agent.workflow_planning import WorkflowPlanningService

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


def test_count_plan_returning_the_whole_table_for_a_selection_task_fails(
    storage, catalog_version
):
    # The deployed failure: one counting step, the count table declared as
    # the final output, and every neighborhood reported as "zero pools".
    plan = copy.deepcopy(COUNT_PLAN)
    plan["concrete_workflow"]["steps"] = plan["concrete_workflow"]["steps"][:2]
    plan["concrete_workflow"]["final_output_ref"] = "support_counts"
    _, _, result = plan_and_validate(
        storage, catalog_version, COUNT_INTERPRETATION, plan
    )
    assert result.status is ValidationStatus.FAIL
    assert any(
        "select zero counts" in d.message and "final_output_ref" in d.message
        for d in result.diagnostics
    )


def test_count_composition_passes(storage, catalog_version):
    client, draft, result = plan_and_validate(
        storage, catalog_version, COUNT_INTERPRETATION, COUNT_PLAN
    )
    assert result.status is ValidationStatus.PASS, [
        d.message for d in result.diagnostics
    ]
    # The planning input is the operation vocabulary, never an answer.
    planning_input = json.loads(client.planning_inputs[0])
    assert len(planning_input["operations"]) == 27
    assert all(operation["description"] for operation in planning_input["operations"])
    assert "expected" not in client.planning_inputs[0]
    assert "verbatim" not in client.planning_inputs[0].lower()


def sum_plan(value_field):
    plan = copy.deepcopy(COUNT_PLAN)
    plan["concrete_workflow"]["steps"][1]["parameters"] += [
        {"name": "statistic", "source": "literal", "value": "sum"},
        {"name": "value_field", "source": "literal", "value": value_field},
    ]
    return plan


def test_a_stated_sum_cannot_be_answered_by_a_count(storage, catalog_version):
    interpretation = copy.deepcopy(COUNT_INTERPRETATION)
    interpretation["task_specification"]["goal"].update(
        aggregation="sum", value_attribute="capaciteit", selection=False
    )
    _, _, result = plan_and_validate(storage, catalog_version, interpretation, COUNT_PLAN)
    assert any(
        d.code is DiagnosticCode.MISSING_PARAMETER and "statistic=sum" in d.message
        for d in result.diagnostics
    ), [d.message for d in result.diagnostics]
    _, _, result = plan_and_validate(
        storage, catalog_version, interpretation, sum_plan("capaciteit")
    )
    assert not any(d.code is DiagnosticCode.MISSING_PARAMETER for d in result.diagnostics), [
        d.message for d in result.diagnostics
    ]


def test_sum_merge_rule_needs_a_numeric_value_field(storage, catalog_version):
    # Summing the numeric attribute types the output column; summing a
    # nominal attribute is blocked by the column flow.
    _, _, result = plan_and_validate(
        storage, catalog_version, COUNT_INTERPRETATION, sum_plan("capaciteit")
    )
    assert result.status is ValidationStatus.PASS, result.diagnostics
    _, _, result = plan_and_validate(
        storage, catalog_version, COUNT_INTERPRETATION, sum_plan("naam")
    )
    assert result.status is ValidationStatus.FAIL
    assert any(
        d.code is DiagnosticCode.INCOMPATIBLE_TYPE and "statistic=sum" in d.message
        for d in result.diagnostics
    )


def test_null_literal_for_an_optional_parameter_means_omitted(storage, catalog_version):
    plan = copy.deepcopy(COUNT_PLAN)
    for parameter in plan["concrete_workflow"]["steps"][1]["parameters"]:
        if parameter["name"] == "class_field":
            parameter["value"] = None
    _, _, result = plan_and_validate(
        storage, catalog_version, COUNT_INTERPRETATION, plan
    )
    assert result.status is ValidationStatus.PASS, result.diagnostics


def test_planning_schema_names_only_the_registry():
    from geoqa_agent.artifact_schemas import WORKFLOW_PLANNING_CONFIGURATION

    steps = WORKFLOW_PLANNING_CONFIGURATION.schema["properties"]
    assert set(steps) == {"concrete_workflow"}
    concrete = steps["concrete_workflow"]["properties"]["steps"]["items"]["properties"]
    assert set(concrete["algorithm_id"]["enum"]) == set(REGISTRY.executable_algorithm_ids)


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
            lambda plan: plan["concrete_workflow"]["steps"][1]["parameters"][0]
            .update(value="no_such_ref"),
            "disconnected-reference",
            id="unavailable-input-ref",
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
        pytest.param(
            lambda plan: plan["concrete_workflow"]["steps"][1]["parameters"][2]
            .update(value="no_such_column"),
            "invalid-parameter",
            id="field-parameter-not-a-column",
        ),
        pytest.param(
            lambda plan: plan["concrete_workflow"]["steps"][2]["parameters"][1]
            .update(value="objekt_count == 0"),
            "invalid-parameter",
            id="expression-over-a-missing-column",
        ),
        pytest.param(
            lambda plan: plan["concrete_workflow"]["steps"].insert(
                2,
                {
                    "step_id": "sum-names",
                    "algorithm_id": "geopandas:aggregate",
                    "parameters": [
                        {"name": "input", "source": "ref", "value": "support_counts"},
                        {"name": "field", "source": "literal", "value": "naam"},
                        {"name": "statistic", "source": "literal", "value": "sum"},
                        {"name": "output_field", "source": "literal", "value": "s"},
                    ],
                    "outputs": [{"name": "output", "ref": "name_sums", "kind": "sink"}],
                },
            ),
            "incompatible-type",
            id="sum-over-a-nominal-column",
        ),
        pytest.param(
            lambda plan: plan["concrete_workflow"]["steps"][1]["parameters"]
            .__setitem__(3, {"name": "field", "source": "literal", "value": "n"}),
            "structural-error",
            id="result-table-misses-the-contract-value-column",
        ),
        pytest.param(
            lambda plan: plan["concrete_workflow"]["steps"][1]["parameters"][0]
            .update(value="counted_objects"),
            "incompatible-type",
            id="point-layer-bound-as-polygons",
        ),
        pytest.param(
            lambda plan: (
                plan["concrete_workflow"]["steps"].insert(
                    1,
                    {
                        "step_id": "support-centroids",
                        "algorithm_id": "geopandas:centroids",
                        "parameters": [
                            {"name": "input", "source": "ref", "value": "supports"}
                        ],
                        "outputs": [
                            {"name": "output", "ref": "support_points", "kind": "sink"}
                        ],
                    },
                ),
                plan["concrete_workflow"]["steps"][2]["parameters"][0].update(
                    value="support_points"
                ),
            ),
            "incompatible-type",
            id="centroids-bound-as-polygons",
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


RASTER_ROLE = {
    "role": "elevation",
    "semantic_label": "elevation model",
    "identity_fields": [],
    "data_kind": "raster",
    "geometry_types": [],
}


def test_raster_role_grounds_but_cannot_feed_a_vector_operation(storage, catalog_version):
    interpretation = copy.deepcopy(COUNT_INTERPRETATION)
    interpretation["task_specification"]["roles"].append(RASTER_ROLE)
    plan = copy.deepcopy(COUNT_PLAN)
    plan["concrete_workflow"]["steps"][0]["parameters"][2].update(value="elevation")
    client, draft, result = plan_and_validate(
        storage, catalog_version, interpretation, plan
    )
    bindings = {item.role: item.layer_id for item in draft.data_bindings.value}
    assert bindings["elevation"] == "dsm"
    layer = json.loads(client.planning_inputs[0])["data_bindings"][2]["layer"]
    assert layer["data_kind"] == "raster" and layer["geometry_types"] == []
    assert layer["attributes"][0] == {
        "name": "elevation",
        "storage_type": "float32",
        "sample_values": [-2.0, 1.5, 12.0],
        "measurement_scale": "numeric",
    }
    assert result.status is ValidationStatus.FAIL
    assert any(
        d.code is DiagnosticCode.INCOMPATIBLE_TYPE and "raster data" in d.message
        for d in result.diagnostics
    ), [d.message for d in result.diagnostics]


IMAGERY_ROLE = {
    "role": "imagery",
    "semantic_label": "satellite imagery",
    "identity_fields": [],
    "data_kind": "image_collection",
    "geometry_types": [],
}


def test_cloud_chain_types_through_the_column_flow(storage, catalog_version):
    # A collection cannot be reduced directly; filter, composite and NDVI
    # band math make an image whose reduction yields the contract column.
    interpretation = copy.deepcopy(COUNT_INTERPRETATION)
    interpretation["task_specification"]["roles"].append(IMAGERY_ROLE)
    interpretation["task_specification"]["goal"].update(value_name="mean_ndvi", selection=False)
    interpretation["task_specification"]["target_transformation"] = ["mean NDVI per neighborhood"]
    steps = [
        {"step_id": "filter", "algorithm_id": "gee:filter", "parameters": [
            {"name": "input", "source": "ref", "value": "imagery"},
            {"name": "start_date", "source": "literal", "value": "2025-06-01"},
            {"name": "end_date", "source": "literal", "value": "2025-09-01"}],
         "outputs": [{"name": "output", "ref": "summer", "kind": "sink"}]},
        {"step_id": "composite", "algorithm_id": "gee:composite", "parameters": [
            {"name": "input", "source": "ref", "value": "summer"}],
         "outputs": [{"name": "output", "ref": "median", "kind": "sink"}]},
        {"step_id": "ndvi", "algorithm_id": "gee:bandmath", "parameters": [
            {"name": "input", "source": "ref", "value": "median"},
            {"name": "expression", "source": "literal", "value": "(B8 - B4) / (B8 + B4)"},
            {"name": "output_band", "source": "literal", "value": "ndvi"}],
         "outputs": [{"name": "output", "ref": "ndvi_image", "kind": "sink"}]},
        {"step_id": "reduce", "algorithm_id": "gee:reduceregions", "parameters": [
            {"name": "input", "source": "ref", "value": "ndvi_image"},
            {"name": "zones", "source": "ref", "value": "supports"},
            {"name": "output_field", "source": "literal", "value": "mean_ndvi"}],
         "outputs": [{"name": "output", "ref": "support_ndvi", "kind": "sink"}]},
    ]
    plan = {"concrete_workflow": {"steps": steps, "final_output_ref": "support_ndvi",
                                  "result_table_ref": "support_ndvi", "diagnostic_refs": []}}
    _, _, result = plan_and_validate(storage, catalog_version, interpretation, plan)
    assert result.status is ValidationStatus.PASS, [d.message for d in result.diagnostics]

    # A stated imagery period must reach the filter dates.
    dated = copy.deepcopy(interpretation)
    dated["task_specification"]["periods"] = [
        {"name": "summer_2025", "start": "2025-06-01", "end": "2025-09-01"}
    ]
    _, _, result = plan_and_validate(storage, catalog_version, dated, plan)
    assert result.status is ValidationStatus.PASS, [d.message for d in result.diagnostics]
    shifted = copy.deepcopy(plan)
    shifted["concrete_workflow"]["steps"][0]["parameters"][2]["value"] = "2025-10-01"
    _, _, result = plan_and_validate(storage, catalog_version, dated, shifted)
    assert any(
        d.code is DiagnosticCode.MISSING_PARAMETER and "summer_2025" in d.message
        for d in result.diagnostics
    ), [d.message for d in result.diagnostics]

    # Reducing the collection itself (no composite) is an image_collection
    # ref on an image parameter.
    short = copy.deepcopy(plan)
    short["concrete_workflow"]["steps"] = [steps[0], {**steps[3], "parameters": [
        {"name": "input", "source": "ref", "value": "summer"}, *steps[3]["parameters"][1:]]}]
    _, _, result = plan_and_validate(storage, catalog_version, interpretation, short)
    assert result.status is ValidationStatus.FAIL
    assert any("image_collection data" in d.message for d in result.diagnostics), [d.message for d in result.diagnostics]


def test_goal_cannot_be_keyed_by_a_raster_role(storage, catalog_version):
    interpretation = copy.deepcopy(COUNT_INTERPRETATION)
    interpretation["task_specification"]["roles"].append(RASTER_ROLE)
    interpretation["task_specification"]["goal"]["per"] = ["elevation"]
    client = ScriptedClient(interpretation, COUNT_PLAN)
    interpreter = QuestionInterpretationAndMatchingService(
        storage=storage, client=client
    )
    outcome = interpreter.interpret("mean elevation", catalog_version=catalog_version)
    assert isinstance(outcome, UnsupportedInterpretation)
    assert any(
        "identity fields" in reason
        for failure in outcome.failed_roles
        for reason in failure.rejection_reasons
    )


def test_declared_cutoff_must_reach_the_plan(storage, catalog_version):
    interpretation = copy.deepcopy(NEAREST_INTERPRETATION)
    interpretation["task_specification"]["quantities"] = [
        {"name": "maximum_distance_m", "value": 500, "unit": "metre"}
    ]

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
    data["task_specification"]["roles"][1]["semantic_label"] = "ice rink"
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
