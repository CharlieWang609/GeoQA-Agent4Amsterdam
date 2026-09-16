# SPDX-License-Identifier: GPL-3.0-only

"""The allow-listed GeoPandas operation contracts shared by planning and execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from data_pipeline.serialization import canonical_json, sha256

ParameterRole = Literal["data_binding", "scalar_config"]
DataKindName = Literal["vector", "raster", "image_collection", "image"]
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
    # Geometry kinds (point, line, polygon) a data binding must carry; None
    # accepts any geometry. Only meaningful for vector bindings.
    geometry: tuple[str, ...] | None = None
    # What a data binding must be: vector features, a raster, or a remote
    # image collection.
    data_kind: DataKindName = "vector"


@dataclass(frozen=True)
class OutputContract:
    """One observable operation output and its effect on the input data."""

    name: str
    value_type: str
    required: bool
    effect: str
    crs_effect: str | None
    # What a sink output is, hence which file the step writes for it.
    data_kind: DataKindName = "vector"


@dataclass(frozen=True)
class OperationContract:
    """Complete execution contract for one allow-listed GeoPandas operation."""

    algorithm_id: str
    description: str
    parameters: tuple[ParameterContract, ...]
    outputs: tuple[OutputContract, ...]

    def parameter(self, name: str) -> ParameterContract:
        """Return a named input parameter contract."""
        for parameter in self.parameters:
            if parameter.name == name:
                return parameter
        raise KeyError(f"{self.algorithm_id} has no parameter named {name}.")

    def output(self, name: str) -> OutputContract:
        """Return a named output contract."""
        for output in self.outputs:
            if output.name == name:
                return output
        raise KeyError(f"{self.algorithm_id} has no output named {name}.")


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
                        "geometry": parameter.geometry,
                        "data_kind": parameter.data_kind,
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
                        "data_kind": output.data_kind,
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


def _source(
    name: str,
    *,
    geometry: tuple[str, ...] | None = None,
    data_kind: DataKindName = "vector",
    required: bool = True,
) -> ParameterContract:
    return ParameterContract(
        name, "source", required, "data_binding", None, None, None, geometry, data_kind
    )


def _raster(name: str, *, required: bool = True) -> ParameterContract:
    return _source(name, data_kind="raster", required=required)


def _remote_sink(name: str, effect: str, kind: DataKindName) -> OutputContract:
    return OutputContract(name, "sink", True, effect, "preserve-input", kind)


def _raster_sink(name: str, effect: str) -> OutputContract:
    return OutputContract(name, "sink", True, effect, "preserve-input", "raster")


def _sink(name: str, effect: str, *, required: bool = True) -> OutputContract:
    return OutputContract(name, "sink", required, effect, "preserve-input")


def _operation_contracts() -> tuple[OperationContract, ...]:
    """The hand-maintained allow-list: each contract mirrors the exact
    behaviour of its implementation in ``geopandas_runner.py``. Expression
    parameters use the pandas ``query``/``eval`` dialect."""

    return (
        OperationContract(
            algorithm_id="geopandas:filterbyexpression",
            description="Extracts features matching a pandas query expression (attribute predicates, boolean logic, null checks).",
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
            description="Extracts features by spatial predicate (intersects, within, contains, ...) against a reference layer.",
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
            description="Extracts features lying within a given distance of a reference layer's features.",
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
            description="Clips a vector layer by the polygons of an overlay layer.",
            parameters=(_source("input"), _source("overlay", geometry=('polygon',))),
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
            description="Vector buffer; distance in layer CRS units (EPSG:28992 = metres).",
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
            description="Set operation of two vector layers: intersection, difference (erase), union, symmetric_difference, or identity.",
            parameters=(
                _source("input", geometry=('polygon',)),
                _source("overlay", geometry=('polygon',)),
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
            description="Dissolves features into single features, optionally grouped by attribute.",
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
            description="Concatenates two vector layers into one.",
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
            description="Point centroids of features.",
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
            description="Computes a new attribute from a pandas eval expression; _area and _length pseudo-columns carry geometry measurements.",
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
            description="Adds geometry measurements (area, perimeter) as attributes.",
            parameters=(_source("input", geometry=('polygon',)),),
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
            description="Renames an attribute field.",
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
            description="Stably sorts vector features by one attribute with explicit null placement.",
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
            description="Joins another layer's attributes by key field.",
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
            description="Joins attributes from features in another layer by spatial predicate (one-to-many or first match); the non-matching output retains the unmatched features.",
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
            description="Spatial join with a merge rule: adds to every polygon the count of the objects whose centroid lies strictly within it (optionally distinct by a class field), or the sum of one of their numeric attributes, including explicit zeros.",
            parameters=(
                _source("polygons", geometry=('polygon',)),
                _source("points"),
                ParameterContract(
                    "class_field", "field", False, "scalar_config", None, None, None
                ),
                ParameterContract(
                    "statistic",
                    "enum",
                    False,
                    "scalar_config",
                    "count",
                    None,
                    ("count", "sum"),
                ),
                ParameterContract(
                    "value_field", "field", False, "scalar_config", None, None, None
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
                    "Preserves every polygon and adds, under field, the merge "
                    "rule over the points whose centroid lies strictly within "
                    "it (a non-point object is located at its centroid): "
                    "statistic=count counts them (distinct by class_field when "
                    "given); statistic=sum adds up their numeric value_field. "
                    "Polygons without points get an explicit zero.",
                ),
            ),
        ),
        OperationContract(
            algorithm_id="geopandas:sjoinnearest",
            description="Joins each feature to its nearest neighbour(s) in another layer, adding the distance and retaining all exact ties.",
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
                    "fields plus the Cartesian distance to the nearest part of "
                    "the target geometry (point, line or polygon); all exactly "
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
            description="Aggregates features (grouped or all-into-one) with one per-field statistic such as sum, mean, min, max or count.",
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
        OperationContract(
            algorithm_id="rasterio:clip",
            description="Crops a raster to the polygons of a mask layer; pixels outside the polygons become nodata.",
            parameters=(_raster("input"), _source("mask", geometry=("polygon",))),
            outputs=(_raster_sink("output", "raster cropped to the mask polygons"),),
        ),
        OperationContract(
            algorithm_id="rasterio:bandmath",
            description="Computes a new single-band raster from a pandas eval expression over the input's band names (a second raster's bands as other_<band>, on the same grid); comparisons yield 1/0, so it also reclassifies (landcover == 10). Nodata in any input pixel stays nodata.",
            parameters=(
                _raster("input"),
                _raster("input_2", required=False),
                ParameterContract("expression", "string", True, "scalar_config", None, None, None),
                ParameterContract("output_band", "string", False, "scalar_config", "value", None, None),
            ),
            outputs=(_raster_sink("output", "one numeric band named output_band"),),
        ),
        OperationContract(
            algorithm_id="rasterio:zonalstatistics",
            description="Summarises a raster band over each polygon of a zones layer (pixels whose centre falls in the zone, nodata excluded) into a new numeric column; zones without valid pixels get null. The way a raster chain becomes a value per unit.",
            parameters=(
                _raster("input"),
                _source("zones", geometry=("polygon",)),
                ParameterContract("band", "string", False, "scalar_config", None, None, None),
                ParameterContract(
                    "statistic", "string", False, "scalar_config", "mean", None,
                    ("mean", "sum", "min", "max", "count"),
                ),
                ParameterContract("output_field", "string", False, "scalar_config", "value", None, None),
            ),
            outputs=(_sink("output", "zones with the statistic column"),),
        ),
        OperationContract(
            algorithm_id="rasterio:samplepoints",
            description="Reads the raster band value under each point of a point layer into a new column (null outside the raster or on nodata). The way a raster chain becomes a value per object.",
            parameters=(
                _raster("input"),
                _source("points", geometry=("point",)),
                ParameterContract("band", "string", False, "scalar_config", None, None, None),
                ParameterContract("output_field", "string", False, "scalar_config", "value", None, None),
            ),
            outputs=(_sink("output", "points with the sampled column"),),
        ),
        OperationContract(
            algorithm_id="gee:filter",
            description="Restricts an Earth Engine image collection to a date range (ISO dates, end exclusive) and optionally to scenes at or below a cloud percentage; the collection stays remote.",
            parameters=(
                _source("input", data_kind="image_collection"),
                ParameterContract("start_date", "string", True, "scalar_config", None, None, None),
                ParameterContract("end_date", "string", True, "scalar_config", None, None, None),
                ParameterContract("max_cloud_percent", "number", False, "scalar_config", None, "percent", None),
            ),
            outputs=(_remote_sink("output", "the filtered collection", "image_collection"),),
        ),
        OperationContract(
            algorithm_id="gee:composite",
            description="Reduces a filtered image collection to one image per pixel (median, mean, min or max over time), masking cloudy pixels first where the collection has a scene classification. The step that turns a collection into an image.",
            parameters=(
                _source("input", data_kind="image_collection"),
                ParameterContract("method", "string", False, "scalar_config", "median", None, ("median", "mean", "min", "max")),
                ParameterContract("mask_clouds", "boolean", False, "scalar_config", True, None, None),
            ),
            outputs=(_remote_sink("output", "one composite image with the collection's bands", "image"),),
        ),
        OperationContract(
            algorithm_id="gee:bandmath",
            description="Computes one new band of a remote image from an arithmetic expression over its band names, for example (B8 - B4) / (B8 + B4) for NDVI; comparisons yield 1/0 (label == 1). With input_2 (another image, for example a second period's composite) its bands are available as other_<band>, so ndvi - other_ndvi is a change image.",
            parameters=(
                _source("input", data_kind="image"),
                _source("input_2", data_kind="image", required=False),
                ParameterContract("expression", "string", True, "scalar_config", None, None, None),
                ParameterContract("output_band", "string", False, "scalar_config", "value", None, None),
            ),
            outputs=(_remote_sink("output", "an image with the single band output_band", "image"),),
        ),
        OperationContract(
            algorithm_id="gee:reduceregions",
            description="Summarises a remote image band over each polygon of a zones layer in the cloud (mean, sum, min, max or count of pixels at the catalog pixel size) into a new numeric column; the way a cloud chain becomes a value per unit.",
            parameters=(
                _source("input", data_kind="image"),
                _source("zones", geometry=("polygon",)),
                ParameterContract("band", "string", False, "scalar_config", None, None, None),
                ParameterContract(
                    "statistic", "string", False, "scalar_config", "mean", None,
                    ("mean", "sum", "min", "max", "count"),
                ),
                ParameterContract("output_field", "string", False, "scalar_config", "value", None, None),
            ),
            outputs=(_sink("output", "zones with the statistic column"),),
        ),
        OperationContract(
            algorithm_id="gee:export",
            description="Downloads one band of a remote image as a local raster on the catalog grid, so rasterio operations can continue with it.",
            parameters=(
                _source("input", data_kind="image"),
                ParameterContract("band", "string", False, "scalar_config", None, None, None),
            ),
            outputs=(_raster_sink("output", "the exported band as a raster"),),
        ),
    )
