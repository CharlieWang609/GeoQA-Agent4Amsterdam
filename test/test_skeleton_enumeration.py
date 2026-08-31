# SPDX-License-Identifier: GPL-3.0-only

"""Type-directed skeleton enumeration and the planning escalation path."""

from __future__ import annotations

import copy

from conftest import (
    ABSTR,
    COUNT_INTERPRETATION,
    COUNT_PLAN,
    ScriptedClient,
)
from rdflib.term import URIRef

from test_end_to_end_execution import build_service
from geoqa_agent.namespace import CCD
from geoqa_agent.semantic_validation import AbstractionCatalog
from geoqa_agent.skeleton_enumeration import (
    MAX_SKELETONS,
    enumerate_skeletons,
)

ABSTRACTIONS = AbstractionCatalog()
COUNT_BINDINGS = {
    "supports": frozenset({CCD.ObjectQ, CCD.VectorTessellationA, CCD.NominalA}),
    "counted_objects": frozenset({CCD.ObjectQ, CCD.PointA, CCD.NominalA}),
}
NEAREST_BINDINGS = {
    "source_points": frozenset({CCD.ObjectQ, CCD.PointA, CCD.NominalA}),
    "target_points": frozenset({CCD.ObjectQ, CCD.PointA, CCD.NominalA}),
}


def abstraction_names(workflow):
    return [step.abstraction_id.rsplit("#", 1)[-1] for step in workflow.steps]


def test_count_enumeration_leads_with_the_counting_join():
    skeletons = enumerate_skeletons(COUNT_BINDINGS, "count", ABSTRACTIONS)
    assert 0 < len(skeletons) <= MAX_SKELETONS
    first = skeletons[0]
    assert abstraction_names(first) == ["SpatialJoinCountTess"]
    # The wiring the free planner keeps getting wrong: points first, then
    # the tessellation.
    assert first.steps[0].input_refs == ("counted_objects", "supports")


def test_nearest_enumeration_offers_both_directions():
    skeletons = enumerate_skeletons(NEAREST_BINDINGS, "nearest", ABSTRACTIONS)
    single_step_wirings = {
        skeleton.steps[0].input_refs
        for skeleton in skeletons
        if abstraction_names(skeleton) == ["NearPointObjects"]
    }
    assert single_step_wirings == {
        ("source_points", "target_points"),
        ("target_points", "source_points"),
    }


def test_every_skeleton_is_well_formed():
    for bindings, family in ((COUNT_BINDINGS, "count"), (NEAREST_BINDINGS, "nearest")):
        for skeleton in enumerate_skeletons(bindings, family, ABSTRACTIONS):
            consumed = {
                ref for step in skeleton.steps for ref in step.input_refs
            }
            assert set(bindings) <= consumed
            produced = [step.output_ref for step in skeleton.steps]
            assert skeleton.final_output_ref == produced[-1]
            assert all(ref in consumed for ref in produced[:-1])
            assert all(
                ABSTRACTIONS.has_abstraction(URIRef(step.abstraction_id))
                for step in skeleton.steps
            )
            assert len(skeleton.steps) <= 4


def test_escalation_supplies_skeletons_after_free_budget(storage, catalog_version):
    bad_plan = copy.deepcopy(COUNT_PLAN)
    bad_plan["abstract_workflow"]["steps"][0][
        "abstraction_id"
    ] = f"{ABSTR}MadeUpOperation"
    client = ScriptedClient(
        COUNT_INTERPRETATION,
        [bad_plan, bad_plan, bad_plan, COUNT_PLAN],
    )
    service = build_service(storage, client)
    session, _ = service.create(
        owner_principal_id="tester",
        question="Which neighborhoods have no swimming pools?",
    )
    draft = session.draft_versions[-1]
    assert draft.validation.status == "pass", draft.validation.diagnostics
    repair = draft.planning_repair
    assert repair is not None
    assert repair.escalated
    assert repair.attempt_count == 4
    # Free-composition attempts run without candidates; the escalated
    # attempt receives the enumerated menu.
    assert "composition_candidates" not in client.planning_inputs[2]
    assert "composition_candidates" in client.planning_inputs[3]
    assert "SpatialJoinCountTess" in client.planning_inputs[3]
