// SPDX-License-Identifier: GPL-3.0-only

// MapLibre overlay specification tests: answer classing and preview styling.

import { describe, expect, it } from "vitest";

import { answerMapSources, previewOverlaySpecs } from "./mapOverlaySpecs";
import { answerMap, nearestAnswerMap } from "./test/fixtures";
import type { CatalogLayer, GeoJsonFeatureCollection } from "./types";

describe("MapLibre overlay specifications", () => {
  it("builds the classified Answer Map source and layers", () => {
    const map = answerMap();
    const specs = answerMapSources(map);

    expect(specs).toEqual({
      source: {
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
              },
            })),
          },
        },
      },
      layers: [
        {
          id: "answer-context",
          type: "fill",
          source: "answer-map",
          filter: ["==", ["get", "_geoqa_class"], "context"],
          paint: {
            "fill-color": "#45636d",
            "fill-opacity": 0.28,
            "fill-outline-color": "#314d56",
          },
        },
        {
          id: "answer-selected",
          type: "fill",
          source: "answer-map",
          filter: ["==", ["get", "_geoqa_class"], "selected"],
          paint: {
            "fill-color": "#e9315b",
            "fill-opacity": 0.82,
            "fill-outline-color": "#8f1432",
          },
        },
      ],
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

  it("builds a distance-styled point layer for nearest sources", () => {
    const specs = answerMapSources(nearestAnswerMap());

    expect(specs.layers).toHaveLength(1);
    expect(specs.layers[0]).toMatchObject({
      id: "answer-nearest-source",
      type: "circle",
      source: "answer-map",
    });
    const data = specs.source.specification.data;
    expect(data).toMatchObject({
      type: "FeatureCollection",
      features: [
        {
          properties: {
            _geoqa_class: "nearest-source",
            _geoqa_identity: "source-tie",
            nearest_distance_m: 10,
          },
        },
        {
          properties: {
            _geoqa_class: "nearest-source",
            _geoqa_identity: "source-zero",
            nearest_distance_m: 0,
          },
        },
      ],
    });
  });
});

function catalogLayer(): CatalogLayer {
  return {
    dataset: "gebieden",
    feature_type: "buurten",
    name: "Neighborhoods",
    name_language: "en",
    description: "Amsterdam neighborhoods",
    description_language: "en",
    semantic_label: "neighborhood",
    geometry_types: ["Polygon"],
    feature_count: 1,
    dataset_version: "snapshot-v1",
    crs: "EPSG:28992",
    original_crs: "EPSG:28992",
    temporal_extent: { start: "2026-01-01", end: null },
    spatial_extent: null,
  };
}
