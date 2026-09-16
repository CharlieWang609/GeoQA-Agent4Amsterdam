# SPDX-License-Identifier: GPL-3.0-only

"""Object ingestion over non-point geometries: type gate and support gate."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from data_pipeline.geoparquet import (
    build_object_geoparquet,
    build_support_geoparquet,
)

CRS = "EPSG:28992"
SCHEMA = {"schema": {"properties": {}}}
RETRIEVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def polygon(x0, y0, x1, y1):
    return {
        "type": "Polygon",
        "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]],
    }


def feature(identity, geometry):
    return {"type": "Feature", "properties": {"id": identity}, "geometry": geometry}


SUPPORT = json.dumps(
    {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "identificatie": "A",
                    "volgnummer": 1,
                    "begin_geldigheid": "2020-01-01T00:00:00+00:00",
                    "eind_geldigheid": None,
                },
                "geometry": polygon(120000, 480000, 121000, 481000),
            }
        ],
    }
).encode()


def test_polygon_objects_are_gated_by_type_and_support_intersection():
    support = build_support_geoparquet(
        SUPPORT, original_crs=CRS, source_schema=SCHEMA, retrieved_at=RETRIEVED_AT
    )
    payload = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                feature("inside", polygon(120100, 480100, 120200, 480200)),
                feature("straddling", polygon(120900, 480900, 121100, 481100)),
                feature("outside", polygon(125000, 485000, 125100, 485100)),
                feature("line", {"type": "LineString", "coordinates": [[120100, 480100], [120200, 480200]]}),
            ],
        }
    ).encode()
    prepared = build_object_geoparquet(
        payload,
        original_crs=CRS,
        source_schema=SCHEMA,
        retrieved_at=RETRIEVED_AT,
        support_geoparquet=support.data,
        record_ref_prefix="veld",
        accepted_geometry_types=frozenset({"Polygon", "MultiPolygon"}),
    )
    assert prepared.vector.feature_count == 2
    assert prepared.vector.geometry_types == ("Polygon",)
    diagnostics = {
        item.category: item.record_refs for item in prepared.quality.diagnostics
    }
    assert diagnostics["boundary"] == ("veld.straddling",)
    assert diagnostics["out_of_support_coordinate"] == ("veld.outside",)
    assert diagnostics["invalid_geometry"] == ("veld.line",)
