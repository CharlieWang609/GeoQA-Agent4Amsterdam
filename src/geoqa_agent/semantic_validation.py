# SPDX-License-Identifier: GPL-3.0-only

"""Load and type-infer vendored semantic tool abstractions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rdflib import Graph
from rdflib.namespace import RDF, RDFS
from rdflib.term import Node, URIRef
from transforge.expr import ApplicationError, Source
from transforge.lang import ParseError, TypeAnnotationError
from transforge.type import TypeInstance, TypingError

from geoqa_agent.cct import cct
from geoqa_agent.namespace import CCT, TOOL, n3

ABSTRACT_TTL = (
    Path(__file__).resolve().parents[2] / "ontology" / "tools" / "abstract.ttl"
)

DiagnosticCode = Literal[
    "incompatible-input-type",
    "invalid-cct-expression",
]


class AbstractionDefinitionError(ValueError):
    """Raised when an abstraction lacks a required unique declaration."""


@dataclass(frozen=True)
class InferenceDiagnostic:
    """A stable explanation of a CCT type-inference failure."""

    code: DiagnosticCode
    message: str
    input_index: int | None = None
    expected_type: str | None = None
    actual_type: str | None = None


@dataclass(frozen=True)
class AbstractionInference:
    """CCT inference evidence alongside the expert-authored CCD output."""

    abstraction: URIRef
    expression: str
    passed: bool
    inferred_type: str | None
    declared_inputs: tuple[frozenset[URIRef], ...]
    declared_output: frozenset[URIRef]
    diagnostics: tuple[InferenceDiagnostic, ...]


@dataclass(frozen=True)
class AbstractionSignature:
    """One abstraction's typed contract, exposed to the planning model."""

    abstraction_id: str
    name: str
    description: str
    cct_expression: str
    input_ccd_types: tuple[tuple[str, ...], ...]
    output_ccd_types: tuple[str, ...]


# Representations the vector-only catalog can never provide; abstractions
# touching them are left out of the planner's vocabulary.
_NON_VECTOR_CCD_NAMES = frozenset(
    {"FieldRaster", "RasterA", "Coverage", "Contour", "PointMeasures"}
)


class AbstractionCatalog:
    """Read the small abstraction contract needed by semantic validation."""

    def __init__(self, path: Path = ABSTRACT_TTL):
        self.graph = Graph()
        self.graph.parse(path, format="ttl")

    def infer(
        self,
        abstraction: URIRef,
        *input_types: TypeInstance,
    ) -> AbstractionInference:
        """Infer an abstraction output, optionally checking concrete inputs."""
        expression = str(
            self._single_object(abstraction, CCT.expression, "cct:expression")
        ).strip()
        output = self._single_object(abstraction, TOOL.output, "tool:output")
        declared_output = frozenset(
            output_type
            for output_type in self.graph.objects(output, RDF.type)
            if isinstance(output_type, URIRef)
        )
        declared_inputs = self._declared_inputs(abstraction)
        sources = tuple(Source(input_type) for input_type in input_types)

        try:
            # With concrete input types the expression is checked against
            # them; without any, transforge falls back to declared defaults.
            parsed = cct.parse(expression, *sources, defaults=not sources)
            inferred_type = parsed.type.fix().normalize()
        except TypeAnnotationError as error:
            diagnostic = self._input_diagnostic(abstraction, error)
        except (ApplicationError, ParseError, TypingError) as error:
            diagnostic = InferenceDiagnostic(
                code="invalid-cct-expression",
                message=f"{n3(abstraction)} CCT expression is invalid: {error}",
            )
        else:
            return AbstractionInference(
                abstraction=abstraction,
                expression=expression,
                passed=True,
                inferred_type=inferred_type.text(with_constraints=True),
                declared_inputs=declared_inputs,
                declared_output=declared_output,
                diagnostics=(),
            )

        return AbstractionInference(
            abstraction=abstraction,
            expression=expression,
            passed=False,
            inferred_type=None,
            declared_inputs=declared_inputs,
            declared_output=declared_output,
            diagnostics=(diagnostic,),
        )

    def vector_signatures(self) -> tuple[AbstractionSignature, ...]:
        """List every abstraction whose contract a vector catalog can satisfy."""

        signatures = []
        for abstraction in sorted(
            self.graph.subjects(RDF.type, TOOL.Abstraction),
            key=str,
        ):
            if not isinstance(abstraction, URIRef):
                continue
            declared_inputs = self._declared_inputs(abstraction)
            output = self._single_object(abstraction, TOOL.output, "tool:output")
            output_types = frozenset(
                output_type
                for output_type in self.graph.objects(output, RDF.type)
                if isinstance(output_type, URIRef)
            )
            names = {
                _local_name(item)
                for input_types in (*declared_inputs, output_types)
                for item in input_types
            }
            if names & _NON_VECTOR_CCD_NAMES:
                continue
            description = " ".join(
                sorted(
                    str(comment)
                    for comment in self.graph.objects(abstraction, RDFS.comment)
                )
            )
            expression = str(
                self._single_object(abstraction, CCT.expression, "cct:expression")
            ).strip()
            signatures.append(
                AbstractionSignature(
                    abstraction_id=str(abstraction),
                    name=_local_name(abstraction),
                    description=description,
                    cct_expression=expression,
                    input_ccd_types=tuple(
                        tuple(sorted(_local_name(item) for item in input_types))
                        for input_types in declared_inputs
                    ),
                    output_ccd_types=tuple(
                        sorted(_local_name(item) for item in output_types)
                    ),
                )
            )
        return tuple(signatures)

    def declared_input_types(
        self,
        abstraction: URIRef,
    ) -> tuple[frozenset[URIRef], ...]:
        """Return the expert-declared CCD types of the abstraction's inputs."""

        return self._declared_inputs(abstraction)

    def declared_output_types(self, abstraction: URIRef) -> frozenset[URIRef]:
        """Return the expert-declared CCD types of an abstraction's output."""

        output = self._single_object(abstraction, TOOL.output, "tool:output")
        return frozenset(
            output_type
            for output_type in self.graph.objects(output, RDF.type)
            if isinstance(output_type, URIRef)
        )

    def _declared_inputs(
        self,
        abstraction: URIRef,
    ) -> tuple[frozenset[URIRef], ...]:
        """Collect declared CCD input types ordered by their tool:id."""

        inputs: list[tuple[int, frozenset[URIRef]]] = []
        for input_node in self.graph.objects(abstraction, TOOL.input):
            identifier = self._single_object(input_node, TOOL.id, "tool:id")
            input_types = frozenset(
                input_type
                for input_type in self.graph.objects(input_node, RDF.type)
                if isinstance(input_type, URIRef)
            )
            inputs.append((int(str(identifier)), input_types))
        return tuple(input_types for _, input_types in sorted(inputs))

    def _single_object(
        self,
        subject: Node,
        predicate: URIRef,
        label: str,
    ) -> Node:
        values = tuple(self.graph.objects(subject, predicate))
        if len(values) != 1:
            raise AbstractionDefinitionError(
                f"{n3(subject)} must declare exactly one {label}; "
                f"found {len(values)}."
            )
        return values[0]

    def has_abstraction(self, abstraction: URIRef) -> bool:
        return (abstraction, RDF.type, TOOL.Abstraction) in self.graph

    @staticmethod
    def _input_diagnostic(
        abstraction: URIRef,
        error: TypeAnnotationError,
    ) -> InferenceDiagnostic:
        input_index = error.input
        expected_type = str(error.type)
        actual_type = str(error.expr.type)
        return InferenceDiagnostic(
            code="incompatible-input-type",
            message=(
                f"{n3(abstraction)} input {input_index} has CCT type "
                f"{actual_type}, but its expression requires {expected_type}."
            ),
            input_index=input_index,
            expected_type=expected_type,
            actual_type=actual_type,
        )


def _local_name(uri: URIRef) -> str:
    text = str(uri)
    for separator in ("#", "/"):
        if separator in text:
            text = text.rsplit(separator, 1)[1]
    return text
