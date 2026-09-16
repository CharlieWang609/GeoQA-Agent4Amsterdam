# SPDX-License-Identifier: GPL-3.0-only

"""In-process raster operations over GeoTIFF files, mirrored by the
column flow and the ``rasterio:*`` contracts of the tool registry.

Steps read and write files under the refs the executor assigns: rasters
as GeoTIFF, vector results as GeoParquet. A raster chain ends by summarising
onto vector zones or sampling at vector objects; only those two operations
produce vector outputs.
"""

from __future__ import annotations

from pathlib import Path
from time import monotonic
from typing import Callable, Mapping

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize
from rasterio.mask import mask as mask_raster

from geoqa_agent.execution import OperationRunResult

NODATA = -9999.0


class RasterioRunner:
    """Run one allow-listed raster operation in process."""

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


def _bands(source: rasterio.DatasetReader) -> dict[str, np.ndarray]:
    """Band name -> float array with nodata as NaN."""

    bands = {}
    for index in range(1, source.count + 1):
        name = source.descriptions[index - 1] or f"band_{index}"
        values = source.read(index).astype("float64")
        if source.nodata is not None:
            values[values == source.nodata] = np.nan
        bands[name] = values
    return bands


def _band(source: rasterio.DatasetReader, parameters: Mapping[str, object]) -> np.ndarray:
    bands = _bands(source)
    name = parameters.get("band")
    if name is None:
        return next(iter(bands.values()))
    if str(name) not in bands:
        raise ValueError(f"Raster has no band named {name!r}; it has {sorted(bands)}.")
    return bands[str(name)]


def _write_raster(
    destination: object,
    values: np.ndarray,
    *,
    like: rasterio.DatasetReader,
    band: str,
    transform: object | None = None,
) -> None:
    profile = like.profile.copy()
    profile.update(
        driver="GTiff",
        dtype="float32",
        count=1,
        nodata=NODATA,
        height=values.shape[0],
        width=values.shape[1],
        compress="deflate",
        tiled=True,
        blockxsize=256,
        blockysize=256,
        transform=transform if transform is not None else like.transform,
    )
    output = np.where(np.isnan(values), NODATA, values).astype("float32")
    with rasterio.open(Path(str(destination)), "w", **profile) as target:
        target.write(output, 1)
        target.set_band_description(1, band)


def _clip(parameters: Mapping[str, object]) -> str:
    polygons = gpd.read_parquet(Path(str(parameters["mask"])))
    with rasterio.open(Path(str(parameters["input"]))) as source:
        values, transform = mask_raster(
            source, list(polygons.geometry), crop=True, nodata=source.nodata, filled=True
        )
        band = source.descriptions[0] or "band_1"
        clipped = values[0].astype("float64")
        if source.nodata is not None:
            clipped[clipped == source.nodata] = np.nan
        _write_raster(parameters["output"], clipped, like=source, band=band, transform=transform)
    return f"pixels={clipped.size}"


def _bandmath(parameters: Mapping[str, object]) -> str:
    """Evaluate a pandas-eval expression over the input's bands (and, when
    given, a second raster's bands as other_<band>); a NaN in any input
    pixel yields nodata."""

    with rasterio.open(Path(str(parameters["input"]))) as source:
        namespace = _bands(source)
        if parameters.get("input_2") is not None:
            with rasterio.open(Path(str(parameters["input_2"]))) as other:
                if other.shape != source.shape or other.transform != source.transform:
                    raise ValueError("input_2 is not on the same grid as input.")
                namespace.update(
                    {f"other_{name}": values for name, values in _bands(other).items()}
                )
        result = pd.eval(str(parameters["expression"]), local_dict=namespace, engine="python")
        values = np.asarray(result, dtype="float64")
        if values.shape != source.shape:
            values = np.broadcast_to(values, source.shape).copy()
        invalid = np.zeros(source.shape, dtype=bool)
        for array in namespace.values():
            invalid |= np.isnan(array)
        values[invalid] = np.nan
        band = str(parameters.get("output_band", "value"))
        _write_raster(parameters["output"], values, like=source, band=band)
    return f"band={band}"


def _zonal_statistics(parameters: Mapping[str, object]) -> str:
    """One statistic of the band's valid pixels whose centres fall in each
    zone; zones without such pixels get a null."""

    zones = gpd.read_parquet(Path(str(parameters["zones"])))
    statistic = str(parameters.get("statistic", "mean"))
    field = str(parameters.get("output_field", "value"))
    with rasterio.open(Path(str(parameters["input"]))) as source:
        values = _band(source, parameters)
        zone_ids = rasterize(
            (
                (geometry, index + 1)
                for index, geometry in enumerate(zones.geometry)
                if geometry is not None and not geometry.is_empty
            ),
            out_shape=source.shape,
            transform=source.transform,
            fill=0,
            dtype="int32",
            all_touched=False,
        )
    valid = (zone_ids > 0) & ~np.isnan(values)
    ids = zone_ids[valid]
    samples = values[valid]
    size = len(zones) + 1
    counts = np.bincount(ids, minlength=size).astype("float64")
    if statistic == "count":
        result = counts
    elif statistic == "sum":
        result = np.bincount(ids, weights=samples, minlength=size)
    elif statistic == "mean":
        with np.errstate(invalid="ignore", divide="ignore"):
            result = np.bincount(ids, weights=samples, minlength=size) / counts
    elif statistic in ("min", "max"):
        result = np.full(size, np.nan)
        reduce = np.minimum if statistic == "min" else np.maximum
        if len(ids):
            result[np.unique(ids)] = np.inf if statistic == "min" else -np.inf
            reduce.at(result, ids, samples)
    else:
        raise ValueError(f"Unsupported statistic: {statistic}")
    result = np.where(counts > 0, result, np.nan)[1:]
    output = zones.copy()
    output[field] = result
    output.reset_index(drop=True).to_parquet(Path(str(parameters["output"])))
    return f"zones={len(output)}"


def _sample_points(parameters: Mapping[str, object]) -> str:
    points = gpd.read_parquet(Path(str(parameters["points"])))
    field = str(parameters.get("output_field", "value"))
    with rasterio.open(Path(str(parameters["input"]))) as source:
        values = _band(source, parameters)
        rows, cols = rasterio.transform.rowcol(
            source.transform, points.geometry.x.to_numpy(), points.geometry.y.to_numpy()
        )
        rows, cols = np.asarray(rows), np.asarray(cols)
        inside = (rows >= 0) & (rows < source.height) & (cols >= 0) & (cols < source.width)
        sampled = np.full(len(points), np.nan)
        sampled[inside] = values[rows[inside], cols[inside]]
    output = points.copy()
    output[field] = sampled
    output.reset_index(drop=True).to_parquet(Path(str(parameters["output"])))
    return f"points={len(output)}"


_OPERATIONS: Mapping[str, Callable[[Mapping[str, object]], str]] = {
    "rasterio:clip": _clip,
    "rasterio:bandmath": _bandmath,
    "rasterio:zonalstatistics": _zonal_statistics,
    "rasterio:samplepoints": _sample_points,
}
