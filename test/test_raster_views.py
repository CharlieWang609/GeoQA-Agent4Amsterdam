# SPDX-License-Identifier: GPL-3.0-only

"""Raster views: GeoTIFFs and Earth Engine recipes become one PNG overlay
each on a Web Mercator grid, with the legend that reads it; a session lists
the raster and satellite artifacts behind its execution."""

from __future__ import annotations

import base64
import json
import zlib
from types import SimpleNamespace

import numpy as np
from conftest import geotiff_bytes

from app.api.raster_views import (
    CLASS_TABLES,
    build_observation_listing,
    build_observation_view,
    colorize,
    encode_png,
    render_geotiff,
)
from data_pipeline.storage import InMemoryObjectStore


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return width, height


def test_png_encoder_round_trips_dimensions_and_pixels():
    rgba = np.zeros((2, 3, 4), dtype="uint8")
    rgba[0, 0] = (255, 0, 0, 255)
    data = encode_png(rgba)
    assert png_size(data) == (3, 2)
    idat_length = int.from_bytes(data[33:37], "big")
    raw = zlib.decompress(data[41 : 41 + idat_length])
    assert raw[:5] == b"\x00\xff\x00\x00\xff"


def test_a_class_band_gets_its_palette_and_only_present_classes_in_the_legend():
    values = np.array([[10.0, 50.0], [80.0, -9999.0]], dtype="float32")
    valid = values != -9999.0
    rgba, style = colorize({"landcover": values}, valid)
    assert style["type"] == "classes" and [item["value"] for item in style["classes"]] == [10, 50, 80]
    assert tuple(rgba[0, 0]) == (0x00, 0x64, 0x00, 255) and rgba[1, 1, 3] == 0


def test_a_continuous_band_is_stretched_between_its_percentiles():
    values = np.linspace(0.0, 1.0, 100, dtype="float32").reshape(10, 10)
    rgba, style = colorize({"ndvi": values}, np.ones((10, 10), dtype=bool))
    assert style["type"] == "ramp" and style["band"] == "ndvi"
    assert 0.0 <= style["min"] < 0.05 and 0.95 < style["max"] <= 1.0
    assert tuple(rgba[0, 0, :3]) == (0x44, 0x01, 0x54) and tuple(rgba[9, 9, :3]) == (0xFD, 0xE7, 0x25)


def test_the_visible_sentinel_bands_render_as_true_colour():
    band = np.full((2, 2), 1500.0, dtype="float32")
    rgba, style = colorize({"B4": band, "B3": band, "B2": band * 2}, np.ones((2, 2), dtype=bool))
    assert style == {"type": "rgb", "bands": ["B4", "B3", "B2"]}
    assert tuple(rgba[0, 0]) == (127, 127, 255, 255)


def test_a_geotiff_becomes_a_mercator_overlay_with_lonlat_bounds():
    values = np.tile(np.arange(30, dtype="float32"), (10, 1))
    view = render_geotiff(geotiff_bytes(values, band="elevation", origin=(120000.0, 487000.0), size=100.0))
    assert view["kind"] == "raster" and view["bands"] == ["elevation"] and view["style"]["type"] == "ramp"
    (west, south), (east, north) = view["bounds"]
    assert 4.8 < west < east < 5.0 and 52.3 < south < north < 52.4
    assert view["width"] == 800 and 250 < view["height"] < 290
    image = base64.b64decode(view["image"].split(",", 1)[1])
    assert png_size(image) == (view["width"], view["height"])
    assert view["scenes"] is None


def session_with_outputs(storage: InMemoryObjectStore):
    values = np.ones((4, 4), dtype="float32")
    storage.put_immutable("execution-jobs/job-1/outputs/clipped.tif", geotiff_bytes(values, band="elevation"))
    descriptor = {
        "kind": "image", "asset": "COPERNICUS/S2_SR_HARMONIZED", "bands": ["ndvi"], "extent": [0, 0, 40, 40],
        "crs": "EPSG:28992", "pixel_size": 10.0, "cloud_property": None, "cloud_mask": None,
        "operations": [{"op": "filter", "start_date": "2025-06-01", "end_date": "2025-09-01", "max_cloud_percent": None}],
    }
    storage.put_immutable("execution-jobs/job-1/outputs/ndvi.json", json.dumps(descriptor).encode())
    storage.put_immutable("execution-jobs/job-1/outputs/table.parquet", b"")
    steps = [
        {"step_id": "clip", "algorithm_id": "rasterio:clip", "parameters": [], "outputs": [{"name": "output", "ref": "clipped", "kind": "sink"}]},
        {
            "step_id": "ndvi", "algorithm_id": "gee:bandmath",
            "parameters": [{"name": "expression", "source": "literal", "value": "(B8 - B4) / (B8 + B4)"}, {"name": "output", "source": "literal", "value": None}],
            "outputs": [{"name": "output", "ref": "ndvi", "kind": "sink"}],
        },
        {"step_id": "zonal", "algorithm_id": "rasterio:zonalstatistics", "parameters": [], "outputs": [{"name": "output", "ref": "table", "kind": "sink"}]},
    ]
    draft = SimpleNamespace(
        draft_version_id="draft-version-1",
        bindings=({"dataset_id": "gebieden", "layer_id": "buurten", "capability_input_ref": "supports", "role": "unit"},),
        concrete_workflow={"steps": steps},
    )
    job = SimpleNamespace(
        job_id="job-1",
        output_locations={"clipped": "memory://x/clipped.tif", "ndvi": "memory://x/ndvi.json", "table": "memory://x/table.parquet"},
    )
    return SimpleNamespace(
        session_id="session-1", draft_versions=[draft], execution_result=job,
        execution_authorization=SimpleNamespace(draft_version_id="draft-version-1"),
    )


def test_a_session_lists_its_raster_and_image_outputs_but_not_tables(storage, catalog_version, monkeypatch):
    session = session_with_outputs(storage)
    listing = build_observation_listing(storage, session)  # type: ignore[arg-type]
    assert [(item["ref"], item["origin"], item["kind"]) for item in listing["items"]] == [
        ("clipped", "step", "raster"),
        ("ndvi", "step", "image"),
    ]
    assert listing["items"][1]["label"] == "bandmath (expression=(B8 - B4) / (B8 + B4))"
    assert listing["items"][1]["view_url"] == "/api/question-sessions/session-1/observations/ndvi"

    view = build_observation_view(storage, session, "clipped")  # type: ignore[arg-type]
    assert view["kind"] == "raster" and view["label"] == "clipped"

    # An Earth Engine view is rendered once and then served from storage.
    rendered = {"kind": "image", "bands": ["ndvi"], "style": {}, "bounds": [], "width": 1, "height": 1, "image": "", "scenes": []}
    monkeypatch.setattr("app.api.raster_views.initialize", lambda: None)
    monkeypatch.setattr("app.api.raster_views.render_descriptor", lambda ee, document: dict(rendered))
    assert build_observation_view(storage, session, "ndvi")["scenes"] == []  # type: ignore[arg-type]
    monkeypatch.setattr("app.api.raster_views.render_descriptor", lambda ee, document: 1 / 0)
    assert build_observation_view(storage, session, "ndvi")["bands"] == ["ndvi"]  # type: ignore[arg-type]
    assert [key for key in storage.list_keys("raster-views/")] and CLASS_TABLES["label"][1][2] == "Trees"
