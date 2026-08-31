# SPDX-License-Identifier: GPL-3.0-only

"""Semantic alignment of the GeoPandas operations with the frozen oracle:
explicit zero counts, strict-within boundary handling, retained nearest
ties, null placement in ordering, and the pandas query dialect."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, Polygon

from geoqa_agent.geopandas_runner import GeoPandasRunner

CRS = "EPSG:28992"
RUNNER = GeoPandasRunner()


def frame(path, columns, geometries):
    layer = gpd.GeoDataFrame(columns, geometry=geometries, crs=CRS)
    layer.to_parquet(path)
    return path


def test_count_points_in_polygon_keeps_zero_and_excludes_boundary(tmp_path):
    polygons = frame(
        tmp_path / "polygons.parquet",
        {"name": ["with-points", "boundary-only", "empty"]},
        [
            Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
            Polygon([(20, 0), (30, 0), (30, 10), (20, 10)]),
            Polygon([(40, 0), (50, 0), (50, 10), (40, 10)]),
        ],
    )
    # Two distinct ids inside the first polygon (one id twice), and one
    # point exactly on the second polygon's boundary.
    points = frame(
        tmp_path / "points.parquet",
        {"id": ["a", "a", "b", "c"]},
        [Point(1, 1), Point(2, 2), Point(3, 3), Point(20, 5)],
    )
    output = tmp_path / "counts.parquet"
    RUNNER.run(
        "geopandas:countpointsinpolygon",
        {
            "polygons": polygons,
            "points": points,
            "class_field": "id",
            "field": "object_count",
            "output": output,
        },
    )
    counts = gpd.read_parquet(output)
    assert list(counts["name"]) == ["with-points", "boundary-only", "empty"]
    # Distinct by id, boundary point excluded, absent groups explicit zero.
    assert list(counts["object_count"]) == [2, 0, 0]


def test_sjoin_nearest_retains_equidistant_ties(tmp_path):
    source = frame(tmp_path / "source.parquet", {"id": ["s1"]}, [Point(0, 0)])
    target = frame(
        tmp_path / "target.parquet",
        {"id": ["near-east", "near-west", "far"]},
        [Point(10, 0), Point(-10, 0), Point(50, 0)],
    )
    output = tmp_path / "pairs.parquet"
    RUNNER.run(
        "geopandas:sjoinnearest",
        {
            "input": source,
            "target": target,
            "fields_to_copy": "id",
            "discard_nonmatching": False,
            "prefix": "target_",
            "neighbors": 1,
            "distance_field": "distance_m",
            "output": output,
        },
    )
    pairs = gpd.read_parquet(output)
    assert sorted(pairs["target_id"]) == ["near-east", "near-west"]
    assert list(pairs["distance_m"]) == [10.0, 10.0]


def test_sjoin_nearest_keeps_integer_identities_intact(tmp_path):
    # Integer target ids must survive the append of unmatched sources:
    # naive concat would upcast them to float ("2717049" -> "2717049.0").
    source = frame(
        tmp_path / "source.parquet",
        {"id": [1, 2]},
        [Point(0, 0), Point(100, 0)],
    )
    target = frame(
        tmp_path / "target.parquet", {"id": [2717049]}, [Point(1, 0)]
    )
    output = tmp_path / "pairs.parquet"
    RUNNER.run(
        "geopandas:sjoinnearest",
        {
            "input": source,
            "target": target,
            "fields_to_copy": "id",
            "discard_nonmatching": False,
            "prefix": "target_",
            "neighbors": 1,
            "max_distance": 10.0,
            "distance_field": "distance_m",
            "output": output,
        },
    )
    pairs = gpd.read_parquet(output).sort_values("id")
    assert list(pairs["id"]) == [1, 2]
    matched, unmatched = pairs.iloc[0], pairs.iloc[1]
    assert matched["target_id"] == 2717049 and str(matched["target_id"]) == "2717049"
    assert pd.isna(unmatched["target_id"]) and pd.isna(unmatched["distance_m"])


def test_order_by_places_nulls_first(tmp_path):
    layer = frame(
        tmp_path / "layer.parquet",
        {"value": [2.0, None, 1.0]},
        [Point(0, 0), Point(1, 1), Point(2, 2)],
    )
    output = tmp_path / "ordered.parquet"
    RUNNER.run(
        "geopandas:orderby",
        {
            "input": layer,
            "by": "value",
            "ascending": True,
            "nulls_first": True,
            "output": output,
        },
    )
    ordered = gpd.read_parquet(output)
    assert pd.isna(ordered["value"].iloc[0])
    assert list(ordered["value"].iloc[1:]) == [1.0, 2.0]


def test_filter_by_expression_uses_query_dialect(tmp_path):
    layer = frame(
        tmp_path / "layer.parquet",
        {
            "object_count": [0, 3],
            "begin": ["2020-01-01", "2030-01-01"],
        },
        [Point(0, 0), Point(1, 1)],
    )
    output = tmp_path / "matched.parquet"
    fail_output = tmp_path / "rest.parquet"
    RUNNER.run(
        "geopandas:filterbyexpression",
        {
            "input": layer,
            "expression": "object_count == 0 and begin <= '2026-08-30T00:00:00Z'",
            "output": output,
            "fail_output": fail_output,
        },
    )
    assert list(gpd.read_parquet(output)["object_count"]) == [0]
    assert list(gpd.read_parquet(fail_output)["object_count"]) == [3]
