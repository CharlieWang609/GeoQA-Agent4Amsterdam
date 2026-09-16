# SPDX-License-Identifier: GPL-3.0-only

"""Browser-safe views of raster and Earth Engine data: one PNG on a Web
Mercator grid per raster Layer, workflow step output or image collection,
plus the legend to read it and, for satellite data, the scenes it covers.

A view is a JSON document the map pane places as an image overlay. Local
GeoTIFFs are reprojected here; Earth Engine descriptors are replayed and
their pixels pulled with ``computePixels`` (viewer-role permission only),
then cached by descriptor hash because the cloud round trip is seconds.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from datetime import UTC, date, datetime, timedelta
from typing import Any, Mapping

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_bounds

from app.api.session_models import QuestionSession
from data_pipeline.catalog import CatalogReader
from data_pipeline.geoparquet import CANONICAL_CRS
from data_pipeline.models import CatalogLayer, DataKind
from data_pipeline.serialization import canonical_json, sha256
from data_pipeline.storage import ObjectStore
from geoqa_agent.gee_runner import build, initialize

MERCATOR_CRS = "EPSG:3857"
DISPLAY_CRS = "EPSG:4326"
MAX_VIEW_PIXELS = 800
MAX_SCENES = 500
NODATA = -9999.0
VIEW_CACHE_TTL = timedelta(days=30)
# The catalog entry of an image collection has no dates of its own; its
# default view is the most recent window with the collection's cloud filter.
DEFAULT_WINDOW = timedelta(days=90)
DEFAULT_MAX_CLOUD_PERCENT = 30.0

TRUE_COLOUR_BANDS = ("B4", "B3", "B2")
TRUE_COLOUR_MAX = 3000.0
RAMP = ("#440154", "#3b528b", "#21918c", "#5ec962", "#fde725")
# Class palettes by band name: Sentinel-2 scene classification, Dynamic
# World label, ESA WorldCover map codes.
CLASS_TABLES: Mapping[str, tuple[tuple[int, str, str], ...]] = {
    "SCL": (
        (1, "#ff0004", "Saturated or defective"),
        (2, "#868686", "Dark area"),
        (3, "#774b0a", "Cloud shadow"),
        (4, "#10d22c", "Vegetation"),
        (5, "#ffff52", "Bare soil"),
        (6, "#0000ff", "Water"),
        (7, "#818181", "Unclassified"),
        (8, "#c0c0c0", "Cloud, medium probability"),
        (9, "#f1f1f1", "Cloud, high probability"),
        (10, "#bac5eb", "Thin cirrus"),
        (11, "#52fff9", "Snow or ice"),
    ),
    "label": (
        (0, "#419bdf", "Water"),
        (1, "#397d49", "Trees"),
        (2, "#88b053", "Grass"),
        (3, "#7a87c6", "Flooded vegetation"),
        (4, "#e49635", "Crops"),
        (5, "#dfc35a", "Shrub and scrub"),
        (6, "#c4281b", "Built"),
        (7, "#a59b8f", "Bare"),
        (8, "#b39fe1", "Snow and ice"),
    ),
    "landcover": (
        (10, "#006400", "Tree cover"),
        (20, "#ffbb22", "Shrubland"),
        (30, "#ffff4c", "Grassland"),
        (40, "#f096ff", "Cropland"),
        (50, "#fa0000", "Built-up"),
        (60, "#b4b4b4", "Bare or sparse vegetation"),
        (70, "#f0f0f0", "Snow and ice"),
        (80, "#0064c8", "Permanent water"),
        (90, "#0096a0", "Herbaceous wetland"),
        (95, "#00cf75", "Mangroves"),
        (100, "#fae6a0", "Moss and lichen"),
    ),
}
VIEWABLE_SUFFIXES = {".tif": DataKind.RASTER, ".json": DataKind.IMAGE}


class RasterViewUnavailableError(ValueError):
    """The layer or output cannot be rendered as a raster view."""


class ObservationNotFoundError(LookupError):
    """The session has no viewable output under that ref."""


# --- Catalog -----------------------------------------------------------------


def build_catalog_raster_view(
    storage: ObjectStore,
    layer: CatalogLayer,
    *,
    today: date | None = None,
) -> dict[str, object]:
    """Render one raster or image-collection Layer of the Catalog."""

    if layer.raster is None:
        raise RasterViewUnavailableError("vector layers have a feature preview, not a raster view")
    digest = layer.content_hash.removeprefix("sha256:")
    if layer.raster.kind is DataKind.RASTER:
        stored = storage.read(f"datasets/{layer.dataset_id}/{layer.layer_id}/{digest}.tif")
        assert stored is not None
        return {**render_geotiff(stored.data), "label": layer.raw.dataset_title or layer.layer_id}
    stored = storage.read(f"datasets/{layer.dataset_id}/{layer.layer_id}/{digest}.json")
    assert stored is not None
    document = _json(stored.data)
    end = today or datetime.now(UTC).date()
    start = end - DEFAULT_WINDOW
    document["operations"] = [
        {
            "op": "filter",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "max_cloud_percent": DEFAULT_MAX_CLOUD_PERCENT if document.get("cloud_property") else None,
        }
    ]
    view = cached_descriptor_view(storage, document)
    return {**view, "label": f"{layer.raw.dataset_title or layer.layer_id}: {start.isoformat()} to {end.isoformat()}"}


# --- Session observations ----------------------------------------------------


def build_observation_listing(storage: ObjectStore, session: QuestionSession) -> dict[str, object]:
    """Every raster or satellite artifact behind the session's execution:
    the bound Catalog layers of those kinds and the persisted step outputs."""

    job = session.execution_result
    if job is None:
        return {"items": []}
    draft = _executed_draft(session)
    catalog = CatalogReader(storage).current()
    layers = {(layer.dataset_id, layer.layer_id): layer for layer in catalog.layers}
    items: list[dict[str, object]] = []
    for binding in draft.bindings:
        layer = layers.get((str(binding.get("dataset_id")), str(binding.get("layer_id"))))
        if layer is None or layer.raster is None:
            continue
        items.append(
            {
                "ref": str(binding.get("capability_input_ref")),
                "origin": "input",
                "step_id": None,
                "algorithm_id": None,
                "kind": layer.raster.kind.value,
                "bands": [band.name for band in layer.raster.bands],
                "label": layer.raw.dataset_title or layer.layer_id,
                "view_url": f"/api/catalog-layers/{layer.dataset_id}/{layer.layer_id}/raster-view",
            }
        )
    workflow = draft.concrete_workflow or {}
    for step in workflow.get("steps", []) if isinstance(workflow, Mapping) else []:
        if not isinstance(step, Mapping):
            continue
        for output in step.get("outputs", []):
            if not isinstance(output, Mapping):
                continue
            ref = str(output.get("ref"))
            location = job.output_locations.get(ref)
            if location is None or _suffix(location) not in VIEWABLE_SUFFIXES:
                continue
            items.append(
                {
                    "ref": ref,
                    "origin": "step",
                    "step_id": str(step.get("step_id")),
                    "algorithm_id": str(step.get("algorithm_id")),
                    "kind": _output_kind(storage, job.job_id, ref, location),
                    "bands": [],
                    "label": _step_label(step),
                    "view_url": f"/api/question-sessions/{session.session_id}/observations/{ref}",
                }
            )
    return {"items": items}


def build_observation_view(storage: ObjectStore, session: QuestionSession, ref: str) -> dict[str, object]:
    job = session.execution_result
    location = None if job is None else job.output_locations.get(ref)
    if job is None or location is None or _suffix(location) not in VIEWABLE_SUFFIXES:
        raise ObservationNotFoundError(ref)
    stored = storage.read(f"execution-jobs/{job.job_id}/outputs/{ref}{_suffix(location)}")
    assert stored is not None
    if _suffix(location) == ".tif":
        return {**render_geotiff(stored.data), "label": ref}
    return {**cached_descriptor_view(storage, _json(stored.data)), "label": ref}


def _executed_draft(session: QuestionSession) -> Any:
    authorization = session.execution_authorization
    assert authorization is not None
    return next(draft for draft in session.draft_versions if draft.draft_version_id == authorization.draft_version_id)


def _output_kind(storage: ObjectStore, job_id: str, ref: str, location: str) -> str:
    if _suffix(location) == ".tif":
        return DataKind.RASTER.value
    stored = storage.read(f"execution-jobs/{job_id}/outputs/{ref}.json")
    assert stored is not None
    return str(_json(stored.data)["kind"])


def _step_label(step: Mapping[str, Any]) -> str:
    """The operation and the parameters that decide what the image is."""

    algorithm = str(step.get("algorithm_id", "")).split(":", 1)[-1]
    literals = [
        f"{parameter.get('name')}={parameter.get('value')}"
        for parameter in step.get("parameters", [])
        if isinstance(parameter, Mapping)
        and parameter.get("source") == "literal"
        and parameter.get("value") is not None
        and parameter.get("name") not in {"output", "output_field"}
    ]
    return f"{algorithm} ({', '.join(literals)})" if literals else algorithm


def _suffix(location: str) -> str:
    return "." + location.rsplit(".", 1)[-1] if "." in location.rsplit("/", 1)[-1] else ""


# --- Rendering ---------------------------------------------------------------


def render_geotiff(data: bytes) -> dict[str, object]:
    """One-band GeoTIFF on the catalog grid -> Web Mercator view."""

    with MemoryFile(data) as memory, memory.open() as source:
        band = source.descriptions[0] or "value"
        grid = _mercator_grid(tuple(source.bounds), source.crs.to_string())
        integer = np.issubdtype(source.dtypes[0], np.integer)
        destination = np.full((grid["height"], grid["width"]), NODATA, dtype="float32")
        reproject(
            source=rasterio.band(source, 1),
            destination=destination,
            src_nodata=source.nodata,
            dst_transform=grid["transform"],
            dst_crs=MERCATOR_CRS,
            dst_nodata=NODATA,
            resampling=Resampling.nearest if integer else Resampling.bilinear,
        )
    return _view({band: destination}, destination != NODATA, grid, kind=DataKind.RASTER.value, bands=[band], scenes=None)


def cached_descriptor_view(storage: ObjectStore, document: Mapping[str, Any]) -> dict[str, object]:
    """Render an Earth Engine descriptor once per content; later requests
    for the same recipe read the stored view."""

    key = f"raster-views/{sha256(canonical_json({'descriptor': document, 'version': 1}))}.json"
    stored = storage.read(key)
    if stored is not None:
        return _json(stored.data)
    view = render_descriptor(initialize(), document)
    storage.put_immutable(key, canonical_json(view))
    storage.set_expiry(key, datetime.now(UTC) + VIEW_CACHE_TTL)
    return view


def render_descriptor(ee: Any, document: Mapping[str, Any]) -> dict[str, object]:
    """Replay the recipe and pull its pixels on the Web Mercator grid; a
    collection is shown as a mosaic with the newest scene on top."""

    grid = _mercator_grid(tuple(document["extent"]), str(document["crs"]))
    result = build(ee, document)
    if document["kind"] == DataKind.IMAGE_COLLECTION.value:
        result = result.mosaic()
    bands = _display_bands(list(document["bands"]))
    pixels = ee.data.computePixels(
        {
            "expression": result.select(bands).toFloat().unmask(NODATA),
            "fileFormat": "NUMPY_NDARRAY",
            "grid": {
                "dimensions": {"width": grid["width"], "height": grid["height"]},
                "affineTransform": {
                    "scaleX": grid["pixel_size"],
                    "shearX": 0,
                    "translateX": grid["bounds"][0],
                    "shearY": 0,
                    "scaleY": -grid["pixel_size"],
                    "translateY": grid["bounds"][3],
                },
                "crsCode": MERCATOR_CRS,
            },
        }
    )
    arrays = {band: np.asarray(pixels[band], dtype="float32") for band in bands}
    valid = np.all([array != NODATA for array in arrays.values()], axis=0)
    return _view(arrays, valid, grid, kind=str(document["kind"]), bands=list(document["bands"]), scenes=_scenes(ee, document))


def _scenes(ee: Any, document: Mapping[str, Any]) -> list[dict[str, object]] | None:
    """The scenes the recipe reads: the collection before its composite.
    An unfiltered collection is every scene since launch, not listed."""

    operations = list(document["operations"])
    before_composite = []
    for operation in operations:
        if operation["op"] == "composite":
            break
        before_composite.append(operation)
    if not any(operation["op"] == "filter" for operation in before_composite):
        return None
    collection = build(ee, {**document, "operations": before_composite}).limit(MAX_SCENES)
    cloud_property = document.get("cloud_property")

    def describe(image: Any) -> Any:
        return ee.Feature(
            None,
            {
                "id": image.get("system:index"),
                "time": image.get("system:time_start"),
                "cloud": image.get(cloud_property) if cloud_property else None,
            },
        )

    rows = collection.map(describe).getInfo()["features"]
    scenes = [
        {
            "id": str(row["properties"]["id"]),
            "date": datetime.fromtimestamp(int(row["properties"]["time"]) / 1000, UTC).date().isoformat(),
            "cloud_percent": row["properties"].get("cloud"),
        }
        for row in rows
    ]
    return sorted(scenes, key=lambda scene: (scene["date"], scene["id"]))


def _display_bands(bands: list[str]) -> list[str]:
    """True colour when the visible bands are there, else the coded class
    band (a land-cover label) if any, else the first band."""

    if all(band in bands for band in TRUE_COLOUR_BANDS):
        return list(TRUE_COLOUR_BANDS)
    return [next((band for band in bands if band in CLASS_TABLES), bands[0])]


def _view(
    arrays: Mapping[str, np.ndarray],
    valid: np.ndarray,
    grid: Mapping[str, Any],
    *,
    kind: str,
    bands: list[str],
    scenes: list[dict[str, object]] | None,
) -> dict[str, object]:
    rgba, style = colorize(arrays, valid)
    west, south, east, north = transform_bounds(MERCATOR_CRS, DISPLAY_CRS, *grid["bounds"])
    return {
        "kind": kind,
        "bands": bands,
        "style": style,
        "bounds": [[west, south], [east, north]],
        "width": grid["width"],
        "height": grid["height"],
        "image": "data:image/png;base64," + base64.b64encode(encode_png(rgba)).decode("ascii"),
        "scenes": scenes,
    }


def colorize(arrays: Mapping[str, np.ndarray], valid: np.ndarray) -> tuple[np.ndarray, dict[str, object]]:
    """RGBA pixels plus the legend that explains them: true colour for the
    Sentinel-2 visible bands, a class palette for coded bands, a
    percentile-stretched ramp for anything else."""

    height, width = valid.shape
    rgba = np.zeros((height, width, 4), dtype="uint8")
    rgba[..., 3] = np.where(valid, 255, 0)
    names = list(arrays)
    if names == list(TRUE_COLOUR_BANDS):
        for channel, band in enumerate(TRUE_COLOUR_BANDS):
            rgba[..., channel] = np.clip(arrays[band] / TRUE_COLOUR_MAX * 255, 0, 255).astype("uint8")
        return rgba, {"type": "rgb", "bands": list(TRUE_COLOUR_BANDS)}
    band = names[0]
    values = arrays[band]
    classes = CLASS_TABLES.get(band)
    if classes is not None:
        codes = np.rint(values).astype("int64")
        present = set(np.unique(codes[valid]).tolist())
        for code, colour, _ in classes:
            rgba[codes == code, :3] = _rgb(colour)
        return rgba, {
            "type": "classes",
            "band": band,
            "classes": [
                {"value": code, "color": colour, "label": label}
                for code, colour, label in classes
                if code in present
            ],
        }
    if valid.any():
        low, high = (float(item) for item in np.percentile(values[valid], (2, 98)))
    else:
        low, high = 0.0, 1.0
    if high <= low:
        high = low + 1.0
    position = np.clip((values - low) / (high - low), 0.0, 1.0)
    stops = np.array([_rgb(colour) for colour in RAMP], dtype="float32")
    scaled = position * (len(RAMP) - 1)
    index = np.clip(np.floor(scaled).astype("int64"), 0, len(RAMP) - 2)
    fraction = (scaled - index)[..., None]
    rgba[..., :3] = (stops[index] * (1 - fraction) + stops[index + 1] * fraction).astype("uint8")
    return rgba, {"type": "ramp", "band": band, "min": low, "max": high, "colors": list(RAMP)}


def _mercator_grid(bounds: tuple[float, ...], crs: str) -> dict[str, Any]:
    """The display grid: the extent in Web Mercator at most MAX_VIEW_PIXELS
    wide, square pixels."""

    west, south, east, north = transform_bounds(crs, MERCATOR_CRS, *bounds)
    pixel_size = max((east - west), (north - south)) / MAX_VIEW_PIXELS
    width = max(1, round((east - west) / pixel_size))
    height = max(1, round((north - south) / pixel_size))
    return {
        "bounds": (west, north - height * pixel_size, west + width * pixel_size, north),
        "width": width,
        "height": height,
        "pixel_size": pixel_size,
        "transform": from_origin(west, north, pixel_size, pixel_size),
    }


def _rgb(colour: str) -> tuple[int, int, int]:
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def _json(data: bytes) -> dict[str, Any]:
    return json.loads(data)


def encode_png(rgba: np.ndarray) -> bytes:
    """Minimal RGBA PNG writer (no image library in the runtime)."""

    height, width = rgba.shape[:2]
    raw = b"".join(b"\x00" + rgba[row].tobytes() for row in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(raw, 6)),
            chunk(b"IEND", b""),
        )
    )
