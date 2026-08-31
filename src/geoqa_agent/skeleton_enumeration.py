# SPDX-License-Identifier: GPL-3.0-only

"""Type-directed enumeration of abstract workflow skeletons.

The validator's CCD compatibility judgement is reused here as a generator:
forward chaining from the bound layers' CCD types over the abstraction
vocabulary yields every well-typed abstract chain (bounded depth and count)
that ends in the task family's goal type. The planner receives these
skeletons as type-legal candidates to choose from — never as required
answers — and every selected skeleton still passes the full validator.

Bounded synthesis in the tradition of QuAnGIS/APE (their runs: length <= 8,
<= 20 solutions); this enumerator searches the abstract level only, where
observed chains are 1-3 steps long, so a small depth bound suffices.
CCT inference is deliberately left to the validator: all failures observed
so far were CCD-level, and keeping the enumerator CCD-only keeps it simple.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from rdflib.term import URIRef

from data_pipeline.models import CatalogVersion
from geoqa_agent.ccd import ccd
from geoqa_agent.governance import task_family
from geoqa_agent.namespace import CCD
from geoqa_agent.polytype import Polytype
from geoqa_agent.question_interpretation import SupportedInterpretation
from geoqa_agent.semantic_validation import AbstractionCatalog
from geoqa_agent.workflow_planning import (
    AbstractWorkflow,
    AbstractWorkflowStep,
    bound_ccd_types,
)

# Acceptable CCD types of a chain's final output, per task family. Each
# family lists every legitimate ending: a count chain may stop at the
# per-support count table (tessellation-typed) or continue into a selected
# subset (plain-region-typed by the select abstractions' declarations).
FAMILY_GOAL_TYPES: Mapping[str, tuple[frozenset[URIRef], ...]] = {
    "count": (
        frozenset({CCD.CountA, CCD.ObjectQ, CCD.VectorTessellationA}),
        frozenset({CCD.CountA, CCD.ObjectQ, CCD.PlainVectorRegionA}),
    ),
    "nearest": (
        frozenset({CCD.ObjectQ, CCD.PointA, CCD.RatioA}),
    ),
}

MAX_DEPTH = 4
MAX_SKELETONS = 20
MAX_EXPANSIONS = 5000
# Menu diversity: chains differing only in a preprocessor step crowd out
# structurally distinct endings, so each final abstraction gets a quota.
MAX_PER_ENDING = 3


@dataclass(frozen=True)
class _Signature:
    """One abstraction's typed contract in enumeration-ready form."""

    abstraction: URIRef
    input_types: tuple[frozenset[URIRef], ...]
    output_types: frozenset[URIRef]


@dataclass(frozen=True)
class _Chain:
    """A partial skeleton: applied steps plus every ref now available."""

    steps: tuple[AbstractWorkflowStep, ...]
    # Ref name -> CCD types, in creation order (bindings first).
    available: tuple[tuple[str, frozenset[URIRef]], ...]


def compatible(
    actual: frozenset[URIRef],
    expected: frozenset[URIRef],
) -> bool:
    """The validator's bidirectional per-dimension CCD compatibility."""

    if not actual:
        return True  # no type evidence cannot rule an input out
    projected_actual = Polytype.project(ccd.dimensions, actual).root_empty()
    projected_expected = Polytype.project(ccd.dimensions, expected).root_empty()
    return bool(
        projected_actual.subtype(projected_expected)
        or projected_expected.subtype(projected_actual)
    )


def enumerate_skeletons(
    bindings: Mapping[str, frozenset[URIRef]],
    family: str,
    abstractions: AbstractionCatalog,
    *,
    max_depth: int = MAX_DEPTH,
    max_skeletons: int = MAX_SKELETONS,
    max_expansions: int = MAX_EXPANSIONS,
) -> tuple[AbstractWorkflow, ...]:
    """Breadth-first enumeration of goal-reaching well-typed skeletons.

    Shortest chains come first. A skeleton qualifies when its final output
    is compatible with one of the family's goal types, every binding ref is
    consumed, and no intermediate output is left dangling. Bounds make the
    search finite; hitting ``max_expansions`` truncates instead of failing.
    """

    goals = FAMILY_GOAL_TYPES[family]
    vocabulary = _vocabulary(abstractions)
    frontier = [_Chain(steps=(), available=tuple(bindings.items()))]
    # Goal-reaching chains with their sort key: (depth, -goal exactness).
    ranked: list[tuple[tuple[int, int], _Chain]] = []
    seen: set[tuple[tuple[str, tuple[str, ...]], ...]] = set()
    expansions = 0

    for depth in range(1, max_depth + 1):
        extended_frontier: list[_Chain] = []
        for chain in frontier:
            for signature in vocabulary:
                for input_refs in _assignments(chain, signature):
                    expansions += 1
                    if expansions > max_expansions:
                        return _best(ranked, max_skeletons)
                    extended = _extend(chain, signature, input_refs)
                    key = tuple(
                        (step.abstraction_id, step.input_refs)
                        for step in extended.steps
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    extended_frontier.append(extended)
                    if _is_goal(extended, bindings, goals):
                        ranked.append(
                            ((depth, -_exactness(extended, goals)), extended)
                        )
        # Finish each depth completely so the cap never cuts a depth
        # mid-alphabet; then shortest and most goal-exact chains win.
        if len(ranked) >= max_skeletons:
            break
        frontier = extended_frontier
    return _best(ranked, max_skeletons)


def skeletons_for_interpretation(
    interpretation: SupportedInterpretation,
    catalog: CatalogVersion,
    abstractions: AbstractionCatalog,
) -> tuple[AbstractWorkflow, ...]:
    """Enumerate for one grounded interpretation; the escalation entry point."""

    layers = {
        (layer.dataset_id, layer.layer_id): layer for layer in catalog.layers
    }
    binding_types = {
        binding.capability_input_ref: bound_ccd_types(
            layers[(binding.dataset_id, binding.layer_id)]
        )
        for binding in interpretation.bindings
    }
    family = task_family(interpretation.task_specification)
    return enumerate_skeletons(binding_types, family, abstractions)


def _vocabulary(abstractions: AbstractionCatalog) -> tuple[_Signature, ...]:
    """The vector abstraction contracts the planner also sees."""

    signatures = []
    for signature in abstractions.vector_signatures():
        abstraction = URIRef(signature.abstraction_id)
        signatures.append(
            _Signature(
                abstraction=abstraction,
                input_types=abstractions.declared_input_types(abstraction),
                output_types=abstractions.declared_output_types(abstraction),
            )
        )
    return tuple(signatures)


def _assignments(
    chain: _Chain,
    signature: _Signature,
) -> tuple[tuple[str, ...], ...]:
    """Every wiring of distinct available refs onto the declared inputs."""

    options: list[tuple[str, ...]] = [()]
    for expected in signature.input_types:
        options = [
            (*prefix, ref)
            for prefix in options
            for ref, actual in chain.available
            if ref not in prefix and compatible(actual, expected)
        ]
    return tuple(options)


def _extend(
    chain: _Chain,
    signature: _Signature,
    input_refs: tuple[str, ...],
) -> _Chain:
    index = len(chain.steps) + 1
    step_id = f"s{index}_{_local_name(signature.abstraction)}"
    step = AbstractWorkflowStep(
        step_id=step_id,
        abstraction_id=str(signature.abstraction),
        input_refs=input_refs,
        output_ref=f"{step_id}_out",
    )
    return _Chain(
        steps=(*chain.steps, step),
        available=(*chain.available, (step.output_ref, signature.output_types)),
    )


def _is_goal(
    chain: _Chain,
    bindings: Mapping[str, frozenset[URIRef]],
    goals: tuple[frozenset[URIRef], ...],
) -> bool:
    final = chain.steps[-1]
    final_types = dict(chain.available)[final.output_ref]
    if not any(compatible(final_types, goal) for goal in goals):
        return False
    consumed = {ref for step in chain.steps for ref in step.input_refs}
    if not set(bindings) <= consumed:
        return False
    # No dangling intermediates: every earlier output feeds a later step.
    return all(
        step.output_ref in consumed
        for step in chain.steps[:-1]
    )


def _exactness(chain: _Chain, goals: tuple[frozenset[URIRef], ...]) -> int:
    """How many goal types the final output carries verbatim (best goal)."""

    final_types = dict(chain.available)[chain.steps[-1].output_ref]
    return max(len(final_types & goal) for goal in goals)


def _best(
    ranked: list[tuple[tuple[int, int], _Chain]],
    max_skeletons: int,
) -> tuple[AbstractWorkflow, ...]:
    ordered = sorted(range(len(ranked)), key=lambda i: (ranked[i][0], i))
    picked: list[_Chain] = []
    per_ending: dict[str, int] = {}
    for i in ordered:
        chain = ranked[i][1]
        ending = chain.steps[-1].abstraction_id
        if per_ending.get(ending, 0) >= MAX_PER_ENDING:
            continue
        per_ending[ending] = per_ending.get(ending, 0) + 1
        picked.append(chain)
        if len(picked) >= max_skeletons:
            break
    return tuple(_workflow(chain) for chain in picked)


def _workflow(chain: _Chain) -> AbstractWorkflow:
    return AbstractWorkflow(
        steps=chain.steps,
        final_output_ref=chain.steps[-1].output_ref,
    )


def _local_name(uri: URIRef) -> str:
    return str(uri).rsplit("#", 1)[-1]
