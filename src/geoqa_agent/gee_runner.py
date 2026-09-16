# SPDX-License-Identifier: GPL-3.0-only

"""Earth Engine operations: remote recipes, computed only when reduced or
exported.

A ``gee:*`` step reads a descriptor (the asset, its bands, the extent and
the operations applied so far), appends its own operation and writes the
new descriptor; nothing touches the cloud until ``gee:reduceregions`` pulls
one statistic per vector zone or ``gee:export`` downloads a GeoTIFF onto the
catalog grid. The descriptor is content-addressed like every other output,
so a plan's remote computation is reproducible from its artifacts alone.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Mapping

import geopandas as gpd
import httpx
import numpy as np

from data_pipeline.geoparquet import CANONICAL_CRS
from data_pipeline.serialization import canonical_json
from data_pipeline.models import DataKind
from geoqa_agent.execution import OperationRunResult

# Statistic name -> ee.Reducer factory name.
REDUCERS = {"mean": "mean", "sum": "sum", "min": "min", "max": "max", "count": "count"}
# Per-pixel cloud masks by rule name: which scene-classification codes to drop.
CLOUD_MASKS: Mapping[str, tuple[str, tuple[int, ...]]] = {
    "s2_scl": ("SCL", (1, 3, 8, 9, 10, 11)),
}
DOWNLOAD_LIMIT_BYTES = 50 * 1024 * 1024


def credentials_configured() -> bool:
    return bool(os.environ.get("GEE_SERVICE_ACCOUNT_KEY") or os.environ.get("GEE_SERVICE_ACCOUNT_KEY_FILE"))


def initialize() -> Any:
    """Initialise the Earth Engine client from the service-account key in
    the environment (the JSON text, or a file path) and return the module."""

    import ee

    if os.environ.get("GEE_SERVICE_ACCOUNT_KEY"):
        key = json.loads(os.environ["GEE_SERVICE_ACCOUNT_KEY"])
        credentials = ee.ServiceAccountCredentials(key["client_email"], key_data=os.environ["GEE_SERVICE_ACCOUNT_KEY"])
    else:
        path = os.environ["GEE_SERVICE_ACCOUNT_KEY_FILE"]
        key = json.loads(Path(path).read_text())
        credentials = ee.ServiceAccountCredentials(key["client_email"], path)
    ee.Initialize(credentials, project=key["project_id"])
    return ee


class GeeRunner:
    """Run one allow-listed Earth Engine operation."""

    def __init__(self) -> None:
        self._ee: Any = None

    def run(
        self,
        algorithm_id: str,
        parameters: Mapping[str, object],
    ) -> OperationRunResult:
        operation = _OPERATIONS.get(algorithm_id)
        if operation is None:
            raise ValueError(f"Operation is not implemented: {algorithm_id}")
        started = monotonic()
        summary = operation(self, parameters)
        return OperationRunResult(
            stdout=f"{algorithm_id}: {summary}",
            stderr="",
            elapsed_seconds=monotonic() - started,
        )

    def engine(self) -> Any:
        if self._ee is None:
            self._ee = initialize()
        return self._ee


def read_descriptor(path: object) -> dict[str, Any]:
    return json.loads(Path(str(path)).read_text())


def write_descriptor(path: object, document: Mapping[str, Any]) -> None:
    Path(str(path)).write_bytes(canonical_json(document))


def _extend(parameters: Mapping[str, object], operation: Mapping[str, Any], *, kind: str, bands: list[str] | None = None) -> str:
    document = read_descriptor(parameters["input"])
    document["operations"] = [*document["operations"], dict(operation)]
    document["kind"] = kind
    if bands is not None:
        document["bands"] = bands
    write_descriptor(parameters["output"], document)
    return f"{operation['op']} -> {kind} {document['bands']}"


def _filter(runner: GeeRunner, parameters: Mapping[str, object]) -> str:
    document = read_descriptor(parameters["input"])
    if document["kind"] != DataKind.IMAGE_COLLECTION.value:
        raise ValueError("gee:filter needs an image collection.")
    operation = {
        "op": "filter",
        "start_date": str(parameters["start_date"]),
        "end_date": str(parameters["end_date"]),
        "max_cloud_percent": parameters.get("max_cloud_percent"),
    }
    return _extend(parameters, operation, kind=DataKind.IMAGE_COLLECTION.value)


def _composite(runner: GeeRunner, parameters: Mapping[str, object]) -> str:
    document = read_descriptor(parameters["input"])
    if document["kind"] != DataKind.IMAGE_COLLECTION.value:
        raise ValueError("gee:composite needs an image collection.")
    operation = {
        "op": "composite",
        "method": str(parameters.get("method", "median")),
        "mask_clouds": bool(parameters.get("mask_clouds", True)),
    }
    return _extend(parameters, operation, kind=DataKind.IMAGE.value)


def _bandmath(runner: GeeRunner, parameters: Mapping[str, object]) -> str:
    document = read_descriptor(parameters["input"])
    if document["kind"] != DataKind.IMAGE.value:
        raise ValueError("gee:bandmath needs an image (composite first).")
    output_band = str(parameters.get("output_band", "value"))
    expression = str(parameters["expression"])
    available = set(document["bands"])
    operation: dict[str, Any] = {"op": "bandmath", "expression": expression, "output_band": output_band}
    if parameters.get("input_2") is not None:
        other = read_descriptor(parameters["input_2"])
        if other["kind"] != DataKind.IMAGE.value:
            raise ValueError("gee:bandmath input_2 needs an image (composite first).")
        # The second image is a recipe of its own, embedded so the result
        # stays a single self-contained descriptor.
        operation["other"] = other
        available |= {f"other_{band}" for band in other["bands"]}
    unknown = set(_names(expression)) - available
    if unknown:
        raise ValueError(f"expression names bands the image lacks: {sorted(unknown)}")
    return _extend(parameters, operation, kind=DataKind.IMAGE.value, bands=[output_band])


def _reduce_regions(runner: GeeRunner, parameters: Mapping[str, object]) -> str:
    """One statistic of the image band over each zone, computed remotely at
    the descriptor's pixel size in the canonical CRS."""

    ee = runner.engine()
    document = read_descriptor(parameters["input"])
    if document["kind"] != DataKind.IMAGE.value:
        raise ValueError("gee:reduceregions needs an image (composite first).")
    zones = gpd.read_parquet(Path(str(parameters["zones"])))
    band = str(parameters.get("band") or document["bands"][0])
    statistic = str(parameters.get("statistic", "mean"))
    field = str(parameters.get("output_field", "value"))
    image = build(ee, document).select([band])
    features = [
        ee.Feature(ee.Geometry(json.loads(gpd.GeoSeries([geometry], crs=CANONICAL_CRS).to_crs("EPSG:4326").to_json())["features"][0]["geometry"]), {"zone_index": index})
        for index, geometry in enumerate(zones.geometry)
        if geometry is not None and not geometry.is_empty
    ]
    reduced = image.reduceRegions(
        collection=ee.FeatureCollection(features),
        reducer=getattr(ee.Reducer, REDUCERS[statistic])(),
        scale=document["pixel_size"],
        crs=document["crs"],
    ).select(["zone_index", statistic], None, False)
    rows = reduced.getInfo()["features"]
    values = np.full(len(zones), np.nan)
    for row in rows:
        properties = row["properties"]
        if properties.get(statistic) is not None:
            values[int(properties["zone_index"])] = float(properties[statistic])
    output = zones.copy()
    output[field] = values
    output.reset_index(drop=True).to_parquet(Path(str(parameters["output"])))
    return f"zones={len(output)} band={band} {statistic}"


def _export(runner: GeeRunner, parameters: Mapping[str, object]) -> str:
    """Download one band of the image as a GeoTIFF on the catalog grid."""

    from data_pipeline.rasters import normalise_raster

    ee = runner.engine()
    document = read_descriptor(parameters["input"])
    if document["kind"] != DataKind.IMAGE.value:
        raise ValueError("gee:export needs an image (composite first).")
    band = str(parameters.get("band") or document["bands"][0])
    extent = (
        float(document["extent"][0]),
        float(document["extent"][1]),
        float(document["extent"][2]),
        float(document["extent"][3]),
    )
    image = build(ee, document).select([band]).toFloat()
    url = image.getDownloadURL(
        {
            "scale": document["pixel_size"],
            "crs": document["crs"],
            "region": ee.Geometry.Rectangle(list(extent), document["crs"], False),
            "format": "GEO_TIFF",
        }
    )
    with httpx.Client(timeout=600) as client:
        response = client.get(url)
        response.raise_for_status()
    if len(response.content) > DOWNLOAD_LIMIT_BYTES:
        raise ValueError("Exported image exceeds the download limit.")
    Path(str(parameters["output"])).write_bytes(
        normalise_raster(
            response.content,
            bounds=extent,
            band=band,
            pixel_size=float(document["pixel_size"]),
            nodata=-9999.0,
            value_type="number",
            resampling="bilinear",
        )
    )
    return f"band={band} bytes={len(response.content)}"


def build(ee: Any, document: Mapping[str, Any]) -> Any:
    """Replay a descriptor into an ee.Image (or ee.ImageCollection)."""

    extent = document["extent"]
    region = ee.Geometry.Rectangle(list(extent), document["crs"], False)
    collection = ee.ImageCollection(document["asset"]).filterBounds(region)
    result: Any = collection
    for operation in document["operations"]:
        if operation["op"] == "filter":
            result = result.filterDate(operation["start_date"], operation["end_date"])
            if operation.get("max_cloud_percent") is not None and document.get("cloud_property"):
                result = result.filter(
                    ee.Filter.lte(document["cloud_property"], float(operation["max_cloud_percent"]))
                )
        elif operation["op"] == "composite":
            mask = document.get("cloud_mask")
            if operation["mask_clouds"] and isinstance(mask, str) and mask in CLOUD_MASKS:
                band, codes = CLOUD_MASKS[mask]

                def masked(image: Any, band: str = band, codes: tuple[int, ...] = codes) -> Any:
                    scl = image.select(band)
                    keep = scl.neq(codes[0])
                    for code in codes[1:]:
                        keep = keep.And(scl.neq(code))
                    return image.updateMask(keep)

                result = result.map(masked)
            result = getattr(result, operation["method"])()
        elif operation["op"] == "bandmath":
            names = _names(operation["expression"])
            variables: dict[str, Any] = {name: result.select(name) for name in names if not name.startswith("other_")}
            if "other" in operation:
                other = build(ee, operation["other"])
                variables.update(
                    {name: other.select(name.removeprefix("other_")) for name in names if name.startswith("other_")}
                )
            result = result.expression(operation["expression"], variables).rename(operation["output_band"])
    return result


def _names(expression: str) -> list[str]:
    """Band names an expression references (identifiers that are not
    numbers or operators)."""

    return [
        name
        for name in dict.fromkeys(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expression))
        if name not in {"and", "or", "not", "True", "False"}
    ]


_OPERATIONS: Mapping[str, Callable[[GeeRunner, Mapping[str, object]], str]] = {
    "gee:filter": _filter,
    "gee:composite": _composite,
    "gee:bandmath": _bandmath,
    "gee:reduceregions": _reduce_regions,
    "gee:export": _export,
}
