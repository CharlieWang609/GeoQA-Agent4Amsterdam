// SPDX-License-Identifier: GPL-3.0-only

// MapLibre overlay specification tests: answer classing and preview styling.

import { describe, expect, it } from "vitest";

import { answerMapSources, answerSymbology, previewOverlaySpecs } from "./mapOverlaySpecs";
import { answerMap, nearestAnswerMap } from "./test/fixtures";
import type { CatalogLayer, GeoJsonFeatureCollection } from "./types";

describe("MapLibre overlay specifications", () => {
  it("builds the classified Answer Map source and layers", () => {
    const map = answerMap();
    const specs = answerMapSources(map);

    expect(specs.source).toEqual({
      id: "answer-map",
      specification: {
        type: "geojson",
        data: {
          type: "FeatureCollection",
          features: map.features.map((feature, index) => ({
            ...feature,
            properties: {
              ...feature.properties,
              _geoqa_class: index === 0 ? "context" : "selected",
              _geoqa_identity: index === 0 ? "A / 1" : "B / 7",
              _geoqa_label: index === 0 ? "A / 1" : "B / 7",
            },
          })),
        },
        generateId: true,
      },
    });
    expect(specs.layers.map((layer) => layer.id)).toEqual([
      "answer-context",
      "answer-selected",
      "answer-hover",
      "answer-line",
      "answer-point",
    ]);
    expect(specs.layers.find((layer) => layer.id === "answer-selected")).toMatchObject({
      type: "fill",
      source: "answer-map",
      filter: ["all", ["==", ["geometry-type"], "Polygon"], ["==", ["get", "_geoqa_class"], "selected"]],
      paint: { "fill-color": "#e9315b" },
    });
    // A selection answer keeps class-based point colouring.
    expect(specs.layers.find((layer) => layer.id === "answer-point")).toMatchObject({
      type: "circle",
      paint: { "circle-color": ["case", ["==", ["get", "_geoqa_class"], "selected"], "#e9315b", "#45636d"] },
    });
  });

  it("builds polygon, line, and point preview layers for a Catalog layer", () => {
    const geojson: GeoJsonFeatureCollection = {
      type: "FeatureCollection",
      features: [],
    };
    const specs = previewOverlaySpecs(catalogLayer(), geojson, "#007c91");

    expect(specs).toEqual({
      source: {
        id: "catalog-gebieden-buurten",
        specification: { type: "geojson", data: geojson },
      },
      layers: [
        {
          id: "catalog-gebieden-buurten-fill",
          type: "fill",
          source: "catalog-gebieden-buurten",
          filter: ["==", ["geometry-type"], "Polygon"],
          paint: { "fill-color": "#007c91", "fill-opacity": 0.2 },
        },
        {
          id: "catalog-gebieden-buurten-line",
          type: "line",
          source: "catalog-gebieden-buurten",
          paint: { "line-color": "#007c91", "line-width": 2 },
        },
        {
          id: "catalog-gebieden-buurten-circle",
          type: "circle",
          source: "catalog-gebieden-buurten",
          filter: ["==", ["geometry-type"], "Point"],
          paint: {
            "circle-color": "#007c91",
            "circle-radius": 5,
            "circle-stroke-color": "#ffffff",
            "circle-stroke-width": 1.5,
          },
        },
      ],
    });
  });

  it("grades points by the answer value when every row is in the answer", () => {
    const specs = answerMapSources(nearestAnswerMap());

    expect(specs.layers.find((layer) => layer.id === "answer-point")).toMatchObject({
      id: "answer-point",
      type: "circle",
      source: "answer-map",
      paint: {
        "circle-color": [
          "interpolate", ["linear"], ["get", "value"],
          0, "#ffdbde", 2.5, "#ffa6ae", 5, "#f56b7d", 7.5, "#da2752", 10, "#93002f",
        ],
      },
    });
    const data = specs.source.specification.data;
    expect(data).toMatchObject({
      type: "FeatureCollection",
      features: [
        { properties: { _geoqa_class: "selected", _geoqa_identity: "source-tie", value: 10 } },
        { properties: { _geoqa_class: "selected", _geoqa_identity: "source-zero", value: 0 } },
      ],
    });
  });

  it("grades polygons on a one-hue ramp when every feature carries a value", () => {
    const map = answerMap();
    map.features[0].properties.is_selected = true;

    expect(answerSymbology(map)).toEqual({
      kind: "ramp",
      low: 0,
      high: 2,
      colors: ["#ffdbde", "#ffa6ae", "#f56b7d", "#da2752", "#93002f"],
    });
    expect(answerMapSources(map).layers.find((layer) => layer.id === "answer-selected")).toMatchObject({
      paint: {
        "fill-color": [
          "interpolate", ["linear"], ["get", "value"],
          0, "#ffdbde", 0.5, "#ffa6ae", 1, "#f56b7d", 1.5, "#da2752", 2, "#93002f",
        ],
      },
    });
  });

  it("colours a nominal value per class in a fixed order", () => {
    const map = answerMap();
    map.features[0].properties.is_selected = true;
    map.value_scale = "nominal";

    expect(answerSymbology(map)).toEqual({
      kind: "classes",
      classes: [{ value: 0, color: "#2a78d6" }, { value: 2, color: "#eb6834" }],
    });
    expect(answerMapSources(map).layers.find((layer) => layer.id === "answer-selected")).toMatchObject({
      paint: { "fill-color": ["match", ["get", "value"], 0, "#2a78d6", 2, "#eb6834", "#9aa5aa"] },
    });
  });
});

function catalogLayer(): CatalogLayer {
  return {
    dataset: "gebieden",
    dataset_title: null,
    dataset_title_language: "nl" as const,
    dataset_description: null,
    dataset_description_language: "nl" as const,
    feature_type: "buurten",
    name: "Neighborhoods",
    name_language: "en",
    description: "Amsterdam neighborhoods",
    description_language: "en",
    semantic_label: "neighborhood",
    data_kind: "vector",
    bands: [],
    geometry_types: ["Polygon"],
    feature_count: 1,
    dataset_version: "snapshot-v1",
    crs: "EPSG:28992",
    original_crs: "EPSG:28992",
    temporal_extent: { start: "2026-01-01", end: null },
    spatial_extent: null,
  };
}
