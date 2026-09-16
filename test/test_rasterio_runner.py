# SPDX-License-Identifier: GPL-3.0-only

"""Semantic alignment of the raster operations: pixel-centre zoning with
nodata excluded, band math propagating nodata, sampling and clipping."""

from __future__ import annotations

import math

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point, Polygon

from geoqa_agent.rasterio_runner import RasterioRunner

CRS = "EPSG:28992"
RUNNER = RasterioRunner()


def raster(path, values, *, band="elevation", nodata=-9999.0, origin=(0.0, 100.0), size=10.0):
    values = np.asarray(values, dtype="float32")
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1, "width": values.shape[1],
        "height": values.shape[0], "crs": CRS, "nodata": nodata,
        "transform": from_origin(origin[0], origin[1], size, size),
    }
    with rasterio.open(path, "w", **profile) as target:
        target.write(values, 1)
        target.set_band_description(1, band)
    return path


def frame(path, columns, geometries):
    gpd.GeoDataFrame(columns, geometry=geometries, crs=CRS).to_parquet(path)
    return path


# A 10 x 10 grid of 10 m pixels over (0..100, 0..100); the value is the
# column index, one pixel is nodata.
GRID = np.tile(np.arange(10, dtype="float32"), (10, 1))
GRID[0, 0] = -9999.0


def test_zonal_statistics_use_pixel_centres_and_skip_nodata(tmp_path):
    elevation = raster(tmp_path / "dtm.tif", GRID)
    zones = frame(
        tmp_path / "zones.parquet",
        {"name": ["west", "east", "outside"]},
        [
            # Columns 0-4 (pixel centres x = 5..45); the shared edge x=50 is
            # a pixel edge, so no pixel is counted twice.
            Polygon([(0, 0), (50, 0), (50, 100), (0, 100)]),
            Polygon([(50, 0), (100, 0), (100, 100), (50, 100)]),
            Polygon([(500, 0), (600, 0), (600, 100), (500, 100)]),
        ],
    )
    output = tmp_path / "stats.parquet"
    RUNNER.run(
        "rasterio:zonalstatistics",
        {"input": elevation, "zones": zones, "statistic": "mean", "output_field": "mean_elevation", "output": output},
    )
    result = gpd.read_parquet(output)
    assert list(result["name"]) == ["west", "east", "outside"]
    # West: 49 valid pixels (one nodata) with values 0..4 -> mean 2.0204...
    assert math.isclose(result["mean_elevation"][0], (10 * 10 - 0) / 49, rel_tol=1e-9)
    assert result["mean_elevation"][1] == 7.0
    assert math.isnan(result["mean_elevation"][2])

    RUNNER.run(
        "rasterio:zonalstatistics",
        {"input": elevation, "zones": zones, "statistic": "count", "output_field": "n", "output": output},
    )
    assert list(gpd.read_parquet(output)["n"][:2]) == [49, 50]


def test_bandmath_reclassifies_and_propagates_nodata(tmp_path):
    classes = np.where(GRID >= 5, 10, 20).astype("float32")
    classes[0, 0] = -9999.0
    landcover = raster(tmp_path / "landcover.tif", classes, band="landcover")
    output = tmp_path / "trees.tif"
    RUNNER.run(
        "rasterio:bandmath",
        {"input": landcover, "expression": "landcover == 10", "output_band": "tree", "output": output},
    )
    with rasterio.open(output) as trees:
        assert trees.descriptions == ("tree",)
        values = trees.read(1)
        assert values[5, 3] == 0 and values[5, 7] == 1
        assert values[0, 0] == trees.nodata  # nodata stays nodata, not 0


def test_bandmath_over_two_rasters_on_one_grid(tmp_path):
    dtm = raster(tmp_path / "dtm.tif", GRID)
    dsm = raster(tmp_path / "dsm.tif", GRID + 3)
    output = tmp_path / "height.tif"
    RUNNER.run(
        "rasterio:bandmath",
        {"input": dsm, "input_2": dtm, "expression": "elevation - other_elevation", "output_band": "height", "output": output},
    )
    with rasterio.open(output) as height:
        values = height.read(1)
        assert values[4, 4] == 3 and values[0, 0] == height.nodata


def test_sample_points_reads_the_pixel_under_each_point(tmp_path):
    elevation = raster(tmp_path / "dtm.tif", GRID)
    points = frame(
        tmp_path / "points.parquet",
        {"id": ["a", "b", "c", "d"]},
        [Point(31, 50), Point(2, 95), Point(500, 500), Point(99, 1)],
    )
    output = tmp_path / "sampled.parquet"
    RUNNER.run(
        "rasterio:samplepoints",
        {"input": elevation, "points": points, "output_field": "elevation", "output": output},
    )
    sampled = gpd.read_parquet(output)["elevation"]
    assert sampled[0] == 3 and sampled[3] == 9
    assert math.isnan(sampled[1])  # the nodata pixel
    assert math.isnan(sampled[2])  # outside the raster


def test_clip_crops_to_the_mask(tmp_path):
    elevation = raster(tmp_path / "dtm.tif", GRID)
    mask = frame(tmp_path / "mask.parquet", {"id": ["m"]}, [Polygon([(20, 20), (60, 20), (60, 60), (20, 60)])])
    output = tmp_path / "clipped.tif"
    RUNNER.run("rasterio:clip", {"input": elevation, "mask": mask, "output": output})
    with rasterio.open(output) as clipped:
        assert clipped.shape == (4, 4)
        assert clipped.bounds.left == 20 and clipped.bounds.top == 60
        assert clipped.read(1)[0, 0] == 2
