# SPDX-License-Identifier: GPL-3.0-only

"""Column schemas flowing through a concrete workflow, mirroring the runner.

A schema maps column name to measurement scale (None when unknown); the
geometry column carries the geometry kind (point, line, polygon, or None
when mixed) for vector data and the data kind (raster, image_collection)
otherwise. Bound layers seed it from their annotations; every allow-listed
operation transforms it exactly as ``geopandas_runner.py`` transforms the
frame, so field parameters and expressions can be checked against the
columns that will exist, and numeric statistics against the scale of the
column they aggregate.
"""

from __future__ import annotations

import ast
import re
from typing import Mapping

from data_pipeline.models import CatalogLayer, DataKind

Schema = Mapping[str, str | None]

GEOMETRY = "geometry"
NUMERIC_SCALES = frozenset({"numeric"})
ORDERED_SCALES = NUMERIC_SCALES | {"ordinal"}

# Which source parameter a field parameter's column names must exist on.
FIELD_SOURCES: Mapping[tuple[str, str], str] = {
    ("geopandas:dissolve", "by"): "input",
    ("geopandas:renamefield", "field"): "input",
    ("geopandas:orderby", "by"): "input",
    ("geopandas:joinattributes", "input_field"): "input",
    ("geopandas:joinattributes", "join_field"): "join",
    ("geopandas:sjoin", "join_fields"): "join",
    ("geopandas:countpointsinpolygon", "class_field"): "points",
    ("geopandas:countpointsinpolygon", "value_field"): "points",
    ("geopandas:sjoinnearest", "fields_to_copy"): "target",
    ("geopandas:aggregate", "by"): "input",
    ("geopandas:aggregate", "field"): "input",
    ("rasterio:zonalstatistics", "band"): "input",
    ("rasterio:samplepoints", "band"): "input",
    ("gee:reduceregions", "band"): "input",
    ("gee:export", "band"): "input",
}
# Which source parameter an expression parameter's columns come from, and
# the pseudo-columns the runner adds before evaluating it.
EXPRESSION_SOURCES: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "geopandas:filterbyexpression": ("input", ()),
    "geopandas:calculatefield": ("input", ("_area", "_length")),
}
# Names an expression may use that are not columns: type names passed to
# astype and the like.
_EXPRESSION_BUILTINS = frozenset({"int", "float", "str", "bool", "True", "False", "None"})
# Which (source, field parameter) a statistic parameter applies to.
STATISTIC_FIELDS: Mapping[str, tuple[str, str]] = {
    "geopandas:aggregate": ("input", "field"),
    "geopandas:countpointsinpolygon": ("points", "value_field"),
    "rasterio:zonalstatistics": ("input", "band"),
    "gee:reduceregions": ("input", "band"),
}
# The measurement scales a statistic needs its field to have.
STATISTIC_SCALES: Mapping[str, frozenset[str]] = {
    "sum": NUMERIC_SCALES,
    "mean": NUMERIC_SCALES,
    "min": ORDERED_SCALES,
    "max": ORDERED_SCALES,
}


def layer_schema(layer: CatalogLayer) -> dict[str, str | None]:
    annotated = (
        {}
        if layer.enriched is None
        else {item.name: item.measurement_scale.value for item in layer.enriched.attributes}
    )
    schema: dict[str, str | None] = {
        attribute.name: annotated.get(attribute.name)
        for attribute in layer.attributes
    }
    schema[GEOMETRY] = (
        geometry_kind(layer.geometry_types)
        if layer.kind is DataKind.VECTOR
        else layer.kind.value
    )
    return schema


def first_band_scale(schema: Schema) -> str | None:
    """The scale of a raster schema's first band (its default band)."""

    return next((scale for name, scale in schema.items() if name != GEOMETRY), None)


def data_kind(schema: Schema) -> str:
    """The data kind a schema describes: vector unless the geometry slot
    carries a raster or image-collection marker."""

    kind = schema.get(GEOMETRY)
    return (
        str(kind)
        if kind in (DataKind.RASTER, DataKind.IMAGE_COLLECTION, DataKind.IMAGE)
        else "vector"
    )


def geometry_kind(geometry_types: tuple[str, ...]) -> str | None:
    """The kind of geometry a layer carries; None when mixed or unknown."""

    geometries = set(geometry_types)
    for kind, members in (
        ("point", {"Point", "MultiPoint"}),
        ("line", {"LineString", "MultiLineString"}),
        ("polygon", {"Polygon", "MultiPolygon"}),
    ):
        if geometries and geometries <= members:
            return kind
    return None


def fields(value: object) -> list[str]:
    return [item.strip() for item in str(value).split(",") if item.strip()]


def expression_columns(expression: str) -> set[str] | None:
    """Column names an expression reads; None when it cannot be parsed.

    The pandas query/eval dialect is Python syntax for the subset the
    planner is told to use; template placeholders are neutralised first.
    Names in call position (functions) are not columns.
    """

    source = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "0", expression)
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError:
        return None
    callees = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id not in callees
        and node.id not in _EXPRESSION_BUILTINS
    }


def apply(
    algorithm_id: str,
    sources: Mapping[str, Schema],
    literals: Mapping[str, object],
) -> dict[str, dict[str, str | None]]:
    """Output-name -> schema for one operation over resolved inputs."""

    def prefixed(schema: Schema, prefix: object | None) -> dict[str, str | None]:
        return {
            (name if prefix is None or name == GEOMETRY else f"{prefix}{name}"): (
                scale
            )
            for name, scale in schema.items()
        }

    def without_geometry(schema: Schema) -> dict[str, str | None]:
        return {name: scale for name, scale in schema.items() if name != GEOMETRY}

    def joined(
        left: Schema,
        right: Schema,
        suffixes: tuple[str, str],
        keep: frozenset[str] = frozenset(),
    ) -> dict[str, str | None]:
        """Merge two schemas the way pandas does: columns present on both
        sides (other than ``keep``) get the left/right suffixes."""

        shared = {name for name in left if name in right and name not in keep}
        merged: dict[str, str | None] = {
            (f"{name}{suffixes[0]}" if name in shared else name): scale
            for name, scale in left.items()
        }
        merged.update(
            {
                (f"{name}{suffixes[1]}" if name in shared else name): scale
                for name, scale in right.items()
            }
        )
        return merged

    def restricted(schema: Schema, names: object | None) -> dict[str, str | None]:
        if names is None:
            return dict(schema)
        return {
            name: schema.get(name)
            for name in (*fields(names), GEOMETRY)
            if name in schema
        }

    match algorithm_id:
        case "geopandas:filterbyexpression":
            return {"output": dict(sources["input"]), "fail_output": dict(sources["input"])}
        case "geopandas:selectbylocation" | "geopandas:selectwithindistance":
            return {
                "output": dict(sources["input"]),
                "non_matching": dict(sources["input"]),
            }
        case "geopandas:clip" | "geopandas:dissolve" | "geopandas:orderby":
            return {"output": dict(sources["input"])}
        case "geopandas:buffer":
            return {"output": {**sources["input"], GEOMETRY: "polygon"}}
        case "geopandas:centroids":
            return {"output": {**sources["input"], GEOMETRY: "point"}}
        case "geopandas:overlay":
            return {"output": {**sources["overlay"], **sources["input"], GEOMETRY: "polygon"}}
        case "geopandas:mergelayers":
            kinds = {sources["input"].get(GEOMETRY), sources["input_2"].get(GEOMETRY)}
            return {
                "output": {
                    **sources["input_2"],
                    **sources["input"],
                    GEOMETRY: kinds.pop() if len(kinds) == 1 else None,
                }
            }
        case "geopandas:calculatefield":
            return {"output": {**sources["input"], str(literals["field"]): None}}
        case "geopandas:addgeometryattributes":
            return {
                "output": {**sources["input"], "area": "numeric", "perimeter": "numeric"}
            }
        case "geopandas:renamefield":
            schema = dict(sources["input"])
            scale = schema.pop(str(literals["field"]), None)
            schema[str(literals["new_name"])] = scale
            return {"output": schema}
        case "geopandas:joinattributes":
            join = without_geometry(sources["join"])
            join_field = str(literals["join_field"])
            prefix = literals.get("prefix")
            attributes = {
                (name if prefix is None or name == join_field else f"{prefix}{name}"): (
                    scale
                )
                for name, scale in join.items()
            }
            input_field = str(literals["input_field"])
            keep = frozenset({input_field, join_field}) if input_field == join_field else frozenset()
            return {"output": joined(sources["input"], attributes, ("_x", "_y"), keep)}
        case "geopandas:sjoin":
            join = without_geometry(
                restricted(sources["join"], literals.get("join_fields"))
            )
            return {
                "output": joined(
                    sources["input"], prefixed(join, literals.get("prefix")), ("_left", "_right")
                ),
                "non_matching": dict(sources["input"]),
            }
        case "geopandas:countpointsinpolygon":
            merged = (
                sources["points"].get(str(literals["value_field"]))
                if literals.get("statistic", "count") == "sum"
                else "numeric"
            )
            return {
                "output": {
                    **sources["polygons"],
                    str(literals.get("field", "object_count")): merged,
                }
            }
        case "geopandas:sjoinnearest":
            target = without_geometry(
                restricted(sources["target"], literals.get("fields_to_copy"))
            )
            return {
                "output": {
                    **joined(
                        sources["input"],
                        prefixed(target, literals.get("prefix")),
                        ("_left", "_right"),
                    ),
                    str(literals.get("distance_field", "distance")): "numeric",
                },
                "non_matching": dict(sources["input"]),
            }
        case "geopandas:aggregate":
            aggregated: dict[str, str | None] = {GEOMETRY: None}
            by = literals.get("by")
            if by is not None:
                aggregated[str(by)] = sources["input"].get(str(by))
            field_scale = sources["input"].get(str(literals["field"]))
            aggregated[str(literals["output_field"])] = (
                "numeric" if literals["statistic"] == "count" else field_scale
            )
            return {"output": aggregated}
        case "rasterio:clip":
            return {"output": dict(sources["input"])}
        case "rasterio:bandmath":
            return {
                "output": {
                    str(literals.get("output_band", "value")): "numeric",
                    GEOMETRY: sources["input"][GEOMETRY],
                }
            }
        case "rasterio:zonalstatistics":
            return {
                "output": {
                    **sources["zones"],
                    str(literals.get("output_field", "value")): "numeric",
                }
            }
        case "gee:filter":
            return {"output": dict(sources["input"])}
        case "gee:composite":
            return {"output": {**sources["input"], GEOMETRY: DataKind.IMAGE.value}}
        case "gee:bandmath":
            return {
                "output": {
                    str(literals.get("output_band", "value")): "numeric",
                    GEOMETRY: DataKind.IMAGE.value,
                }
            }
        case "gee:reduceregions":
            return {
                "output": {
                    **sources["zones"],
                    str(literals.get("output_field", "value")): "numeric",
                }
            }
        case "gee:export":
            band = literals.get("band")
            scale = (
                first_band_scale(sources["input"])
                if band is None
                else sources["input"].get(str(band))
            )
            return {
                "output": {
                    str(band or next(iter(sources["input"]))): scale,
                    GEOMETRY: DataKind.RASTER.value,
                }
            }
        case "rasterio:samplepoints":
            band = literals.get("band")
            scale = (
                first_band_scale(sources["input"])
                if band is None
                else sources["input"].get(str(band))
            )
            return {
                "output": {**sources["points"], str(literals.get("output_field", "value")): scale}
            }
    raise KeyError(f"No column effect for {algorithm_id}.")
