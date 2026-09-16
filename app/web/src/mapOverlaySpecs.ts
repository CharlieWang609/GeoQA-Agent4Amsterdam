// SPDX-License-Identifier: GPL-3.0-only

import type { FeatureCollection, GeoJsonProperties, Geometry } from "geojson";
import type {
  GeoJSONSourceSpecification,
  ImageSourceSpecification,
  LayerSpecification,
  SourceSpecification,
} from "maplibre-gl";

import type {
  AnswerMapFeatureCollection,
  CatalogLayer,
  GeoJsonFeatureCollection,
  RasterView,
} from "./types";

export type OverlaySpecifications<Source extends SourceSpecification = GeoJSONSourceSpecification> = {
  source: { id: string; specification: Source };
  layers: LayerSpecification[];
};

// The answer's symbology follows the answer's shape. A subset answer is two
// classes (the features that match against the rest); a value for every
// feature is graded on a one-hue ramp from light to dark, or, for a nominal
// value, coloured per class in a fixed order.
export const SEQUENTIAL_RAMP = ["#ffdbde", "#ffa6ae", "#f56b7d", "#da2752", "#93002f"] as const;
export const CATEGORICAL_PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"] as const;
const SELECTED_COLOR = "#e9315b";
const CONTEXT_COLOR = "#45636d";
const UNCLASSED_COLOR = "#9aa5aa";

export type AnswerSymbology =
  | { kind: "selection"; selected: number; context: number }
  | { kind: "ramp"; low: number; high: number; colors: string[] }
  | { kind: "classes"; classes: Array<{ value: number; color: string }> };

export function answerSymbology(answerMap: AnswerMapFeatureCollection): AnswerSymbology {
  const features = answerMap.features;
  const selected = features.filter((feature) => feature.properties.is_selected).length;
  const context = features.length - selected;
  if (features.length === 0 || context > 0) return { kind: "selection", selected, context };
  const values = features.map((feature) => feature.properties.value);
  if (answerMap.value_scale === "nominal") {
    const distinct = [...new Set(values)].sort((a, b) => a - b);
    return {
      kind: "classes",
      classes: distinct.map((value, index) => ({ value, color: CATEGORICAL_PALETTE[index] ?? UNCLASSED_COLOR })),
    };
  }
  const low = Math.min(...values);
  const high = Math.max(...values);
  return { kind: "ramp", low, high, colors: high > low ? [...SEQUENTIAL_RAMP] : [SEQUENTIAL_RAMP[3]] };
}

// The MapLibre colour expression that paints a feature under a symbology.
function valueColor(symbology: AnswerSymbology): unknown {
  switch (symbology.kind) {
    case "selection":
      return ["case", ["==", ["get", "_geoqa_class"], "selected"], SELECTED_COLOR, CONTEXT_COLOR];
    case "ramp": {
      if (symbology.colors.length === 1) return symbology.colors[0];
      const span = symbology.high - symbology.low;
      const stops = symbology.colors.flatMap((color, index) => [
        symbology.low + (span * index) / (symbology.colors.length - 1),
        color,
      ]);
      return ["interpolate", ["linear"], ["get", "value"], ...stops];
    }
    case "classes":
      return [
        "match",
        ["get", "value"],
        ...symbology.classes.flatMap((entry) => [entry.value, entry.color]),
        UNCLASSED_COLOR,
      ];
  }
}

// Build the answer overlay: each feature is stamped with derived _geoqa_*
// properties so the layers can filter selected vs context rows and the
// tooltip can show the feature's name (its identity when the result has
// none). Geometry decides the mark (fill, line, circle); the symbology
// decides its colour. Features get generated ids so the hovered one can be
// emphasised through feature state.
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
        _geoqa_label: feature.properties.name || Object.values(feature.properties.identity).join(" / "),
      },
    })),
  };
  const hovered: unknown = ["boolean", ["feature-state", "hover"], false];
  const symbology = answerSymbology(answerMap);
  const color = valueColor(symbology);
  const fill = symbology.kind === "selection" ? SELECTED_COLOR : color;
  return {
    source: {
      id: "answer-map",
      specification: { type: "geojson", data, generateId: true },
    },
    layers: [
      {
        id: "answer-context",
        type: "fill",
        source: "answer-map",
        filter: ["all", ["==", ["geometry-type"], "Polygon"], ["==", ["get", "_geoqa_class"], "context"]],
        paint: {
          "fill-color": "#45636d",
          "fill-opacity": ["case", hovered, 0.5, 0.28] as never,
          "fill-outline-color": "#314d56",
        },
      },
      {
        id: "answer-selected",
        type: "fill",
        source: "answer-map",
        filter: ["all", ["==", ["geometry-type"], "Polygon"], ["==", ["get", "_geoqa_class"], "selected"]],
        paint: {
          "fill-color": fill as never,
          "fill-opacity": ["case", hovered, 1, 0.82] as never,
          "fill-outline-color": "#8f1432",
        },
      },
      {
        id: "answer-hover",
        type: "line",
        source: "answer-map",
        filter: ["==", ["geometry-type"], "Polygon"],
        paint: {
          "line-color": "#16303d",
          "line-width": ["case", hovered, 2.5, 0] as never,
        },
      },
      {
        id: "answer-line",
        type: "line",
        source: "answer-map",
        filter: ["==", ["geometry-type"], "LineString"],
        paint: {
          "line-color": color as never,
          "line-width": ["case", hovered, 5, 3] as never,
        },
      },
      {
        id: "answer-point",
        type: "circle",
        source: "answer-map",
        filter: ["==", ["geometry-type"], "Point"],
        paint: {
          "circle-color": color as never,
          "circle-radius": ["case", hovered, 10, 7] as never,
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 1.5,
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
): "selected" | "context" {
  return feature.properties.is_selected ? "selected" : "context";
}

export function layerKey(layer: CatalogLayer) {
  return `${layer.dataset}/${layer.feature_type}`;
}

// A raster view is one PNG in Web Mercator placed by its four corners.
export function rasterOverlaySpecs(
  key: string,
  view: RasterView,
): OverlaySpecifications<ImageSourceSpecification> {
  const id = `raster-${key.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  const [[west, south], [east, north]] = view.bounds;
  return {
    source: {
      id,
      specification: {
        type: "image",
        url: view.image,
        coordinates: [[west, north], [east, north], [east, south], [west, south]],
      },
    },
    layers: [
      {
        id: `${id}-image`,
        type: "raster",
        source: id,
        paint: { "raster-opacity": 0.85, "raster-resampling": "nearest" },
      },
    ],
  };
}
