// SPDX-License-Identifier: GPL-3.0-only

import type { FeatureCollection, GeoJsonProperties, Geometry } from "geojson";
import type { GeoJSONSourceSpecification, LayerSpecification } from "maplibre-gl";

import type {
  AnswerMapFeatureCollection,
  CatalogLayer,
  GeoJsonFeatureCollection,
} from "./types";

export type OverlaySpecifications = {
  source: { id: string; specification: GeoJSONSourceSpecification };
  layers: LayerSpecification[];
};

// Build the answer overlay: each feature is stamped with derived _geoqa_*
// properties so the two fill layers can filter selected vs context
// neighborhoods and the tooltip can show a compact identity.
export function answerMapSources(
  answerMap: AnswerMapFeatureCollection,
): OverlaySpecifications {
  const data: FeatureCollection<Geometry, GeoJsonProperties> = {
    type: "FeatureCollection",
    features: answerMap.features.map((feature) => ({
      ...feature,
      geometry: feature.geometry as Geometry,
      properties: {
        ...feature.properties,
        _geoqa_class: classifyAnswerFeature(feature),
        _geoqa_identity: Object.values(feature.properties.identity).join(" / "),
      },
    })),
  };
  const nearest = "answer_kind" in answerMap;
  return {
    source: {
      id: "answer-map",
      specification: { type: "geojson", data },
    },
    layers: nearest ? [
      {
        id: "answer-nearest-source",
        type: "circle",
        source: "answer-map",
        paint: {
          "circle-color": [
            "interpolate",
            ["linear"],
            ["get", "nearest_distance_m"],
            0,
            "#2a9d8f",
            5000,
            "#e9315b",
          ],
          "circle-radius": 7,
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 1.5,
        },
      },
    ] : [
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
  };
}

export function previewOverlaySpecs(
  layer: CatalogLayer,
  geojson: GeoJsonFeatureCollection,
  color: string,
): OverlaySpecifications {
  const base = `catalog-${layerKey(layer).replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  return {
    source: {
      id: base,
      specification: {
        type: "geojson",
        data: geojson as FeatureCollection<Geometry, GeoJsonProperties>,
      },
    },
    layers: [
      {
        id: `${base}-fill`,
        type: "fill",
        source: base,
        filter: ["==", ["geometry-type"], "Polygon"],
        paint: { "fill-color": color, "fill-opacity": 0.2 },
      },
      {
        id: `${base}-line`,
        type: "line",
        source: base,
        paint: { "line-color": color, "line-width": 2 },
      },
      {
        id: `${base}-circle`,
        type: "circle",
        source: base,
        filter: ["==", ["geometry-type"], "Point"],
        paint: {
          "circle-color": color,
          "circle-radius": 5,
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 1.5,
        },
      },
    ],
  };
}

export function classifyAnswerFeature(
  feature: AnswerMapFeatureCollection["features"][number],
): "selected" | "context" | "nearest-source" {
  return "nearest_distance_m" in feature.properties
    ? "nearest-source"
    : feature.properties.is_selected
      ? "selected"
      : "context";
}

export function layerKey(layer: CatalogLayer) {
  return `${layer.dataset}/${layer.feature_type}`;
}
