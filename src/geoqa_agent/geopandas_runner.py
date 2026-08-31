# SPDX-License-Identifier: GPL-3.0-only

"""In-process GeoPandas implementations of the allow-listed operations.

Each operation reads its GeoParquet inputs from the paths the execution
worker resolved, computes with GeoPandas, and writes its sink outputs back
as GeoParquet. Contracts in ``tool_registry.py`` mirror these semantics.
"""

from __future__ import annotations

from pathlib import Path
from time import monotonic
from typing import Callable, Literal, Mapping, cast

import geopandas as gpd
import pandas as pd
import shapely

from geoqa_agent.execution import ExecutionRuntimeProvenance, OperationRunResult


def runtime_provenance(code_commit: str) -> ExecutionRuntimeProvenance:
    """Version identity of the in-process GeoPandas runtime."""

    return ExecutionRuntimeProvenance(
        geopandas=gpd.__version__,
        shapely=shapely.__version__,
        code_commit=code_commit,
    )


class GeoPandasRunner:
    """Run one allow-listed operation over GeoParquet files in process."""

    def run(
        self,
        algorithm_id: str,
        parameters: Mapping[str, object],
    ) -> OperationRunResult:
        operation = _OPERATIONS.get(algorithm_id)
        if operation is None:
            raise ValueError(f"Operation is not implemented: {algorithm_id}")
        started = monotonic()
        summary = operation(parameters)
        return OperationRunResult(
            stdout=f"{algorithm_id}: {summary}",
            stderr="",
            elapsed_seconds=monotonic() - started,
        )


def _read(parameters: Mapping[str, object], name: str) -> gpd.GeoDataFrame:
    return gpd.read_parquet(Path(str(parameters[name])))


def _write(
    parameters: Mapping[str, object],
    name: str,
    frame: gpd.GeoDataFrame,
) -> int:
    """Write one sink output if the step declared it; return the row count."""

    destination = parameters.get(name)
    if destination is not None:
        frame.reset_index(drop=True).to_parquet(Path(str(destination)))
    return len(frame)


def _fields(value: object) -> list[str]:
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _prefixed(
    frame: gpd.GeoDataFrame,
    prefix: object | None,
) -> gpd.GeoDataFrame:
    if prefix is None:
        return frame
    return frame.rename(
        columns={
            name: f"{prefix}{name}"
            for name in frame.columns
            if name != frame.geometry.name
        }
    )


def _with_unmatched(
    joined: gpd.GeoDataFrame,
    unmatched: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Append unmatched input rows without degrading joined column types.

    Concatenation fills the joined-only columns with NaN, which silently
    upcasts integer columns (identities!) to float; nullable Int64 keeps
    matched identities intact.
    """

    if unmatched.empty:
        return joined
    output = pd.concat([joined, unmatched]).sort_index(kind="stable")
    for name in joined.columns:
        if name not in unmatched.columns and pd.api.types.is_integer_dtype(
            joined[name]
        ):
            output[name] = output[name].astype("Int64")
    return output


def _filter_by_expression(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    matched = frame.query(str(parameters["expression"]))
    _write(parameters, "output", matched)
    _write(parameters, "fail_output", frame.loc[~frame.index.isin(matched.index)])
    return f"matched={len(matched)} of {len(frame)}"


def _select_by_location(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    reference = _read(parameters, "reference")
    joined = gpd.sjoin(
        frame,
        reference[[reference.geometry.name]],
        predicate=str(parameters["predicate"]),
        how="inner",
    )
    matched = frame.loc[frame.index.isin(joined.index)]
    _write(parameters, "output", matched)
    _write(parameters, "non_matching", frame.loc[~frame.index.isin(joined.index)])
    return f"matched={len(matched)} of {len(frame)}"


def _select_within_distance(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    reference = _read(parameters, "reference")
    mask = (
        frame.geometry.distance(reference.geometry.union_all())
        <= float(str(parameters["distance"]))
    )
    _write(parameters, "output", frame.loc[mask])
    _write(parameters, "non_matching", frame.loc[~mask])
    return f"matched={int(mask.sum())} of {len(frame)}"


def _clip(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    clipped = gpd.clip(frame, _read(parameters, "overlay"))
    rows = _write(parameters, "output", clipped)
    return f"rows={rows}"


def _buffer(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    result = frame.copy()
    result[result.geometry.name] = frame.geometry.buffer(
        float(str(parameters["distance"]))
    )
    rows = _write(parameters, "output", result)
    return f"rows={rows}"


def _overlay(parameters: Mapping[str, object]) -> str:
    result = gpd.overlay(
        _read(parameters, "input"),
        _read(parameters, "overlay"),
        how=cast(
            Literal[
                "intersection",
                "union",
                "identity",
                "symmetric_difference",
                "difference",
            ],
            str(parameters["how"]),
        ),
    )
    rows = _write(parameters, "output", result)
    return f"rows={rows}"


def _dissolve(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    by = parameters.get("by")
    result = frame.dissolve(by=None if by is None else str(by))
    if by is not None:
        result = result.reset_index()
    rows = _write(parameters, "output", result)
    return f"groups={rows}"


def _merge_layers(parameters: Mapping[str, object]) -> str:
    merged = pd.concat(
        [_read(parameters, "input"), _read(parameters, "input_2")],
        ignore_index=True,
    )
    rows = _write(parameters, "output", merged)
    return f"rows={rows}"


def _centroids(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    result = frame.copy()
    result[result.geometry.name] = frame.geometry.centroid
    rows = _write(parameters, "output", result)
    return f"rows={rows}"


def _calculate_field(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    augmented = frame.assign(_area=frame.geometry.area, _length=frame.geometry.length)
    result = frame.copy()
    result[str(parameters["field"])] = augmented.eval(str(parameters["expression"]))
    rows = _write(parameters, "output", result)
    return f"rows={rows}"


def _add_geometry_attributes(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    result = frame.assign(area=frame.geometry.area, perimeter=frame.geometry.length)
    rows = _write(parameters, "output", result)
    return f"rows={rows}"


def _rename_field(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    field = str(parameters["field"])
    if field not in frame.columns:
        raise ValueError(f"Field to rename does not exist: {field}")
    result = frame.rename(columns={field: str(parameters["new_name"])})
    rows = _write(parameters, "output", result)
    return f"rows={rows}"


def _order_by(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    result = frame.sort_values(
        str(parameters["by"]),
        ascending=bool(parameters["ascending"]),
        na_position="first" if parameters["nulls_first"] else "last",
        kind="stable",
    )
    rows = _write(parameters, "output", result)
    return f"rows={rows}"


def _join_attributes(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    join = _read(parameters, "join")
    join_field = str(parameters["join_field"])
    attributes = join.drop(columns=join.geometry.name)
    prefix = parameters.get("prefix")
    if prefix is not None:
        attributes = attributes.rename(
            columns={
                name: f"{prefix}{name}"
                for name in attributes.columns
                if name != join_field
            }
        )
    result = frame.merge(
        attributes,
        left_on=str(parameters["input_field"]),
        right_on=join_field,
        how="left",
    )
    rows = _write(parameters, "output", result)
    return f"rows={rows}"


def _sjoin(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    join = _read(parameters, "join")
    fields = parameters.get("join_fields")
    if fields is not None:
        join = join[[*_fields(fields), join.geometry.name]]
    joined = gpd.sjoin(
        frame,
        _prefixed(join, parameters.get("prefix")),
        predicate=str(parameters["predicate"]),
        how="inner",
    )
    if parameters["method"] == "first":
        joined = joined.loc[~joined.index.duplicated(keep="first")]
    joined = joined.drop(columns="index_right")
    unmatched = frame.loc[~frame.index.isin(joined.index)]
    output = (
        joined
        if parameters["discard_nonmatching"]
        else _with_unmatched(joined, unmatched)
    )
    _write(parameters, "output", output)
    _write(parameters, "non_matching", unmatched)
    return f"joined_count={len(joined)} non_matching={len(unmatched)}"


def _count_points_in_polygon(parameters: Mapping[str, object]) -> str:
    polygons = _read(parameters, "polygons")
    points = _read(parameters, "points")
    joined = gpd.sjoin(
        points,
        polygons[[polygons.geometry.name]],
        predicate="within",
        how="inner",
    )
    class_field = parameters.get("class_field")
    grouped = joined.groupby("index_right")
    counts = (
        grouped.size() if class_field is None else grouped[str(class_field)].nunique()
    )
    result = polygons.copy()
    # Every polygon keeps a row: absent groups become explicit zero counts.
    result[str(parameters["field"])] = (
        counts.reindex(polygons.index, fill_value=0).astype(int)
    )
    rows = _write(parameters, "output", result)
    return f"polygons={rows} points_within={len(joined)}"


def _sjoin_nearest(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    target = _read(parameters, "target")
    fields = parameters.get("fields_to_copy")
    if fields is not None:
        target = target[[*_fields(fields), target.geometry.name]]
    max_distance = parameters.get("max_distance")
    joined = gpd.sjoin_nearest(
        frame,
        _prefixed(target, parameters.get("prefix")),
        how="inner",
        max_distance=None if max_distance is None else float(str(max_distance)),
        distance_col=str(parameters["distance_field"]),
    )
    joined = joined.drop(columns="index_right")
    unmatched = frame.loc[~frame.index.isin(joined.index)]
    output = (
        joined
        if parameters["discard_nonmatching"]
        else _with_unmatched(joined, unmatched)
    )
    _write(parameters, "output", output)
    _write(parameters, "non_matching", unmatched)
    return f"joined_count={len(joined)} unjoinable_count={len(unmatched)}"


def _aggregate(parameters: Mapping[str, object]) -> str:
    frame = _read(parameters, "input")
    by = parameters.get("by")
    field = str(parameters["field"])
    result = frame.dissolve(
        by=None if by is None else str(by),
        aggfunc={field: str(parameters["statistic"])},
    )
    if by is not None:
        result = result.reset_index()
    result = result.rename(columns={field: str(parameters["output_field"])})
    rows = _write(parameters, "output", result)
    return f"groups={rows}"


_OPERATIONS: Mapping[str, Callable[[Mapping[str, object]], str]] = {
    "geopandas:filterbyexpression": _filter_by_expression,
    "geopandas:selectbylocation": _select_by_location,
    "geopandas:selectwithindistance": _select_within_distance,
    "geopandas:clip": _clip,
    "geopandas:buffer": _buffer,
    "geopandas:overlay": _overlay,
    "geopandas:dissolve": _dissolve,
    "geopandas:mergelayers": _merge_layers,
    "geopandas:centroids": _centroids,
    "geopandas:calculatefield": _calculate_field,
    "geopandas:addgeometryattributes": _add_geometry_attributes,
    "geopandas:renamefield": _rename_field,
    "geopandas:orderby": _order_by,
    "geopandas:joinattributes": _join_attributes,
    "geopandas:sjoin": _sjoin,
    "geopandas:countpointsinpolygon": _count_points_in_polygon,
    "geopandas:sjoinnearest": _sjoin_nearest,
    "geopandas:aggregate": _aggregate,
}
