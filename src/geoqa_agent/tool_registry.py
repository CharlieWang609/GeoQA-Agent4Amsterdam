# SPDX-License-Identifier: GPL-3.0-only

"""The allow-listed GeoPandas operation contracts shared by planning and execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from data_pipeline.serialization import canonical_json, sha256

ParameterRole = Literal["data_binding", "scalar_config"]
BindingSource = Literal["ref", "literal", "template"]
OutputKind = Literal["sink", "result"]

SPATIAL_PREDICATES = (
    "intersects",
    "within",
    "contains",
    "crosses",
    "touches",
    "overlaps",
)


class ToolRegistryDefinitionError(ValueError):
    """Raised when executable contracts are inconsistent."""


class CapabilityNotExecutableError(LookupError):
    """Raised when an operation has no executable contract."""


@dataclass(frozen=True)
class ParameterContract:
    """One operation input parameter and the constraints needed before execution."""

    name: str
    value_type: str
    required: bool
    role: ParameterRole
    default: object | None
    unit: str | None
    allowed_values: tuple[object, ...] | None


@dataclass(frozen=True)
class OutputContract:
    """One observable operation output and its effect on the input data."""

    name: str
    value_type: str
    required: bool
    effect: str
    crs_effect: str | None


@dataclass(frozen=True)
class OperationContract:
    """Complete execution contract for one allow-listed GeoPandas operation."""

    algorithm_id: str
    parameters: tuple[ParameterContract, ...]
    outputs: tuple[OutputContract, ...]

    def parameter(self, name: str) -> ParameterContract:
        """Return a named input parameter contract."""
        for parameter in self.parameters:
            if parameter.name == name:
                return parameter
        raise KeyError(f"{self.algorithm_id} has no parameter named {name}.")


@dataclass(frozen=True)
class ParameterBinding:
    """A concrete step parameter sourced from data, a literal, or a template."""

    name: str
    source: BindingSource
    value: object


@dataclass(frozen=True)
class OutputBinding:
    """An operation output bound to a workflow reference."""

    name: str
    ref: str
    kind: OutputKind


@dataclass(frozen=True)
class ToolRegistry:
    """The operation allow-list shared by planning, validation, and execution."""

    algorithms: tuple[OperationContract, ...]

    @property
    def version(self) -> str:
        """Return a stable identity for the executable contracts."""
        document = [
            {
                "algorithm_id": algorithm.algorithm_id,
                "parameters": [
                    {
                        "name": parameter.name,
                        "value_type": parameter.value_type,
                        "required": parameter.required,
                        "role": parameter.role,
                        "default": parameter.default,
                        "unit": parameter.unit,
                        "allowed_values": parameter.allowed_values,
                    }
                    for parameter in algorithm.parameters
                ],
                "outputs": [
                    {
                        "name": output.name,
                        "value_type": output.value_type,
                        "required": output.required,
                        "effect": output.effect,
                        "crs_effect": output.crs_effect,
                    }
                    for output in algorithm.outputs
                ],
            }
            for algorithm in self.algorithms
        ]
        return f"sha256:{sha256(canonical_json(document))}"

    @property
    def executable_algorithm_ids(self) -> tuple[str, ...]:
        return tuple(sorted(contract.algorithm_id for contract in self.algorithms))

    def algorithm(self, algorithm_id: str) -> OperationContract:
        """Resolve only operations with complete executable contracts."""
        for contract in self.algorithms:
            if contract.algorithm_id == algorithm_id:
                return contract
        raise CapabilityNotExecutableError(
            f"{algorithm_id} has no executable parameter contract."
        )


def load_tool_registry() -> ToolRegistry:
    """Load the hand-maintained GeoPandas operation allow-list."""

    return ToolRegistry(algorithms=_operation_contracts())


def _source(name: str) -> ParameterContract:
    return ParameterContract(name, "source", True, "data_binding", None, None, None)


def _sink(name: str, effect: str, *, required: bool = True) -> OutputContract:
    return OutputContract(name, "sink", required, effect, "preserve-input")


def _operation_contracts() -> tuple[OperationContract, ...]:
    """The hand-maintained allow-list: each contract mirrors the exact
    behaviour of its implementation in ``geopandas_runner.py``. Expression
    parameters use the pandas ``query``/``eval`` dialect."""

    return (
        OperationContract(
            algorithm_id="geopandas:filterbyexpression",
            parameters=(
                _source("input"),
                ParameterContract(
                    "expression", "expression", True, "scalar_config", None, None, None
                ),
            ),
            outputs=(
                _sink(
                    "output",
                    "Preserves matching feature geometries, attributes, and CRS.",
                ),
                _sink(
                    "fail_output",
                    "Preserves non-matching features for diagnostics.",
                    required=False,
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:selectbylocation",
            parameters=(
                _source("input"),
                _source("reference"),
                ParameterContract(
                    "predicate",
                    "enum",
                    True,
                    "scalar_config",
                    "intersects",
                    None,
                    SPATIAL_PREDICATES,
                ),
            ),
            outputs=(
                _sink(
                    "output",
                    "Preserves input features satisfying the spatial predicate "
                    "against any reference feature.",
                ),
                _sink(
                    "non_matching",
                    "Preserves input features with no spatial match.",
                    required=False,
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:selectwithindistance",
            parameters=(
                _source("input"),
                _source("reference"),
                ParameterContract(
                    "distance", "number", True, "scalar_config", None, "metre", None
                ),
            ),
            outputs=(
                _sink(
                    "output",
                    "Preserves input features within the distance of any "
                    "reference feature.",
                ),
                _sink(
                    "non_matching",
                    "Preserves input features beyond the distance.",
                    required=False,
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:clip",
            parameters=(_source("input"), _source("overlay")),
            outputs=(
                _sink(
                    "output",
                    "Clips input geometries to the overlay polygons, keeping "
                    "attributes.",
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:buffer",
            parameters=(
                _source("input"),
                ParameterContract(
                    "distance", "number", True, "scalar_config", None, "metre", None
                ),
            ),
            outputs=(
                _sink(
                    "output",
                    "Replaces each geometry with its polygon buffer; attributes "
                    "are preserved.",
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:overlay",
            parameters=(
                _source("input"),
                _source("overlay"),
                ParameterContract(
                    "how",
                    "enum",
                    True,
                    "scalar_config",
                    "intersection",
                    None,
                    (
                        "intersection",
                        "difference",
                        "union",
                        "symmetric_difference",
                        "identity",
                    ),
                ),
            ),
            outputs=(
                _sink(
                    "output",
                    "Set-operation of the two layers; attributes of both sides "
                    "are carried on intersecting parts.",
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:dissolve",
            parameters=(
                _source("input"),
                ParameterContract(
                    "by", "field", False, "scalar_config", None, None, None
                ),
            ),
            outputs=(
                _sink(
                    "output",
                    "Unions geometries into one feature per group (or one "
                    "overall feature without 'by').",
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:mergelayers",
            parameters=(_source("input"), _source("input_2")),
            outputs=(
                _sink(
                    "output",
                    "Concatenates the two layers' rows; columns are unioned.",
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:centroids",
            parameters=(_source("input"),),
            outputs=(
                _sink(
                    "output",
                    "Replaces each geometry with its point centroid; attributes "
                    "are preserved.",
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:calculatefield",
            parameters=(
                _source("input"),
                ParameterContract(
                    "field", "string", True, "scalar_config", None, None, None
                ),
                ParameterContract(
                    "expression", "expression", True, "scalar_config", None, None, None
                ),
            ),
            outputs=(
                _sink(
                    "output",
                    "Adds (or overwrites) one attribute computed by a pandas "
                    "eval expression; pseudo-columns _area and _length carry "
                    "the geometry measurements in CRS units.",
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:addgeometryattributes",
            parameters=(_source("input"),),
            outputs=(
                _sink(
                    "output",
                    "Adds 'area' and 'perimeter' attributes in CRS units "
                    "(EPSG:28992 = metres).",
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:renamefield",
            parameters=(
                _source("input"),
                ParameterContract(
                    "field", "field", True, "scalar_config", None, None, None
                ),
                ParameterContract(
                    "new_name", "string", True, "scalar_config", None, None, None
                ),
            ),
            outputs=(
                _sink(
                    "output",
                    "Preserves geometry, rows, CRS, and all attributes while "
                    "renaming one field.",
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:orderby",
            parameters=(
                _source("input"),
                ParameterContract(
                    "by", "field", True, "scalar_config", None, None, None
                ),
                ParameterContract(
                    "ascending",
                    "boolean",
                    True,
                    "scalar_config",
                    True,
                    None,
                    (False, True),
                ),
                ParameterContract(
                    "nulls_first",
                    "boolean",
                    True,
                    "scalar_config",
                    False,
                    None,
                    (False, True),
                ),
            ),
            outputs=(
                _sink(
                    "output",
                    "Preserves geometry, attributes, and CRS while stably "
                    "ordering rows by one column.",
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:joinattributes",
            parameters=(
                _source("input"),
                _source("join"),
                ParameterContract(
                    "input_field", "field", True, "scalar_config", None, None, None
                ),
                ParameterContract(
                    "join_field", "field", True, "scalar_config", None, None, None
                ),
                ParameterContract(
                    "prefix", "string", False, "scalar_config", None, None, None
                ),
            ),
            outputs=(
                _sink(
                    "output",
                    "Left-joins the join layer's non-geometry attributes onto "
                    "input rows by key equality.",
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:sjoin",
            parameters=(
                _source("input"),
                ParameterContract(
                    "predicate",
                    "enum",
                    True,
                    "scalar_config",
                    "intersects",
                    None,
                    SPATIAL_PREDICATES,
                ),
                _source("join"),
                ParameterContract(
                    "method",
                    "enum",
                    True,
                    "scalar_config",
                    "one_to_many",
                    None,
                    ("one_to_many", "first"),
                ),
                ParameterContract(
                    "discard_nonmatching",
                    "boolean",
                    True,
                    "scalar_config",
                    False,
                    None,
                    (False, True),
                ),
                ParameterContract(
                    "prefix", "string", False, "scalar_config", None, None, None
                ),
                ParameterContract(
                    "join_fields", "field", False, "scalar_config", None, None, None
                ),
            ),
            outputs=(
                _sink(
                    "output",
                    "Preserves input features (only matching ones when "
                    "discard_nonmatching) with the join layer's attributes "
                    "attached per spatial match.",
                ),
                _sink(
                    "non_matching",
                    "Preserves input features with no spatial match for "
                    "diagnostics.",
                ),
                OutputContract(
                    "joined_count",
                    "number",
                    True,
                    "Reports the number of matched input records.",
                    None,
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:countpointsinpolygon",
            parameters=(
                _source("polygons"),
                _source("points"),
                ParameterContract(
                    "class_field", "field", False, "scalar_config", None, None, None
                ),
                ParameterContract(
                    "field",
                    "string",
                    True,
                    "scalar_config",
                    "object_count",
                    None,
                    None,
                ),
            ),
            outputs=(
                _sink(
                    "output",
                    "Preserves every polygon and adds an explicit "
                    "strictly-within point count (distinct by class_field when "
                    "given), including zero.",
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:sjoinnearest",
            parameters=(
                _source("input"),
                _source("target"),
                ParameterContract(
                    "fields_to_copy", "field", False, "scalar_config", None, None, None
                ),
                ParameterContract(
                    "discard_nonmatching",
                    "boolean",
                    True,
                    "scalar_config",
                    False,
                    None,
                    (False, True),
                ),
                ParameterContract(
                    "prefix", "string", False, "scalar_config", None, None, None
                ),
                ParameterContract(
                    "neighbors",
                    "number",
                    True,
                    "scalar_config",
                    1,
                    None,
                    (1,),
                ),
                ParameterContract(
                    "max_distance",
                    "distance",
                    False,
                    "scalar_config",
                    None,
                    "metre",
                    None,
                ),
                ParameterContract(
                    "distance_field",
                    "string",
                    True,
                    "scalar_config",
                    "distance",
                    None,
                    None,
                ),
            ),
            outputs=(
                _sink(
                    "output",
                    "Preserves source geometry and adds the copied target "
                    "fields plus the Cartesian distance; all exactly "
                    "equidistant nearest targets are retained as separate rows.",
                ),
                _sink(
                    "non_matching",
                    "Preserves source features without an eligible target "
                    "match.",
                ),
                OutputContract(
                    "joined_count",
                    "number",
                    True,
                    "Reports the number of source features joined.",
                    None,
                ),
                OutputContract(
                    "unjoinable_count",
                    "number",
                    True,
                    "Reports the number of source features without a match.",
                    None,
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:aggregate",
            parameters=(
                _source("input"),
                ParameterContract(
                    "by", "field", False, "scalar_config", None, None, None
                ),
                ParameterContract(
                    "field", "field", True, "scalar_config", None, None, None
                ),
                ParameterContract(
                    "statistic",
                    "enum",
                    True,
                    "scalar_config",
                    None,
                    None,
                    ("count", "sum", "mean", "min", "max"),
                ),
                ParameterContract(
                    "output_field", "string", True, "scalar_config", None, None, None
                ),
            ),
            outputs=(
                _sink(
                    "output",
                    "One feature per group (unioned geometry) carrying the "
                    "statistic of the field under output_field.",
                ),
            ),
        ),
    )
