# SPDX-License-Identifier: GPL-3.0-only

"""Earth Engine steps are recipes: they extend a descriptor without touching
the cloud, and only well-formed chains reach a reduction."""

from __future__ import annotations

import json

import pytest

from data_pipeline.collections import SENTINEL2, descriptor
from geoqa_agent.gee_runner import GeeRunner, _names, read_descriptor

RUNNER = GeeRunner()
EXTENT = (110180.0, 476700.0, 135940.0, 493900.0)


def write(path, document):
    path.write_text(json.dumps(document))
    return path


def test_filter_composite_bandmath_build_a_recipe(tmp_path):
    collection = write(tmp_path / "s2.json", descriptor(SENTINEL2, EXTENT))
    filtered = tmp_path / "filtered.json"
    RUNNER.run(
        "gee:filter",
        {"input": collection, "start_date": "2025-06-01", "end_date": "2025-09-01", "max_cloud_percent": 30, "output": filtered},
    )
    composite = tmp_path / "composite.json"
    RUNNER.run("gee:composite", {"input": filtered, "method": "median", "output": composite})
    ndvi = tmp_path / "ndvi.json"
    RUNNER.run(
        "gee:bandmath",
        {"input": composite, "expression": "(B8 - B4) / (B8 + B4)", "output_band": "ndvi", "output": ndvi},
    )
    document = read_descriptor(ndvi)
    assert document["kind"] == "image" and document["bands"] == ["ndvi"]
    assert [step["op"] for step in document["operations"]] == ["filter", "composite", "bandmath"]
    assert document["operations"][1]["mask_clouds"] is True
    assert document["asset"] == SENTINEL2.asset and document["extent"] == list(EXTENT)


def test_a_collection_cannot_be_reduced_or_recombined_before_a_composite(tmp_path):
    collection = write(tmp_path / "s2.json", descriptor(SENTINEL2, EXTENT))
    with pytest.raises(ValueError, match="composite first"):
        RUNNER.run("gee:bandmath", {"input": collection, "expression": "B8 - B4", "output": tmp_path / "x.json"})
    composite = tmp_path / "composite.json"
    RUNNER.run("gee:composite", {"input": collection, "output": composite})
    with pytest.raises(ValueError, match="image collection"):
        RUNNER.run("gee:composite", {"input": composite, "output": tmp_path / "y.json"})


def test_bandmath_over_two_images_embeds_the_second_recipe(tmp_path):
    collection = write(tmp_path / "s2.json", descriptor(SENTINEL2, EXTENT))
    images = {}
    for year in ("2024", "2025"):
        filtered = tmp_path / f"{year}.json"
        RUNNER.run("gee:filter", {"input": collection, "start_date": f"{year}-06-01", "end_date": f"{year}-09-01", "output": filtered})
        composite = tmp_path / f"{year}-median.json"
        RUNNER.run("gee:composite", {"input": filtered, "output": composite})
        ndvi = tmp_path / f"{year}-ndvi.json"
        RUNNER.run("gee:bandmath", {"input": composite, "expression": "(B8 - B4) / (B8 + B4)", "output_band": "ndvi", "output": ndvi})
        images[year] = ndvi
    change = tmp_path / "change.json"
    RUNNER.run(
        "gee:bandmath",
        {"input": images["2025"], "input_2": images["2024"], "expression": "ndvi - other_ndvi", "output_band": "ndvi_change", "output": change},
    )
    document = read_descriptor(change)
    assert document["bands"] == ["ndvi_change"]
    other = document["operations"][-1]["other"]
    assert other["bands"] == ["ndvi"] and other["operations"][0]["start_date"] == "2024-06-01"
    with pytest.raises(ValueError, match="other_B8"):
        RUNNER.run("gee:bandmath", {"input": images["2025"], "input_2": images["2024"], "expression": "ndvi - other_B8", "output": tmp_path / "x.json"})


def test_bandmath_rejects_unknown_bands(tmp_path):
    collection = write(tmp_path / "s2.json", descriptor(SENTINEL2, EXTENT))
    composite = tmp_path / "composite.json"
    RUNNER.run("gee:composite", {"input": collection, "output": composite})
    with pytest.raises(ValueError, match="B99"):
        RUNNER.run("gee:bandmath", {"input": composite, "expression": "B99 / B4", "output": tmp_path / "x.json"})


def test_expression_names_skip_numbers_and_keywords():
    assert _names("(B8 - B4) / (B8 + B4) * 1.5") == ["B8", "B4"]
    assert _names("label == 1 and trees > 0.5") == ["label", "trees"]
