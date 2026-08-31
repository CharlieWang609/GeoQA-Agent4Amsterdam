// SPDX-License-Identifier: GPL-3.0-only

import { useEffect, useMemo, useRef, useState } from "react";
import * as maplibregl from "maplibre-gl";
import type {
  GeoJSONSource,
  MapGeoJSONFeature,
  MapLayerMouseEvent,
  MapLibreMap,
  StyleSpecification,
} from "maplibre-gl";

import {
  ApiError,
  getCatalogLayerPreview,
  getCatalogLayers,
} from "./api";
import type {
  AnswerMapFeatureCollection,
  CatalogLayer,
  GeoJsonFeatureCollection,
} from "./types";
import {
  answerMapSources,
  layerKey,
  previewOverlaySpecs,
  type OverlaySpecifications,
} from "./mapOverlaySpecs";
import "./mapSetup";

const AMSTERDAM_CENTER: [number, number] = [4.9, 52.37];
const ANSWER_SOURCE = "answer-map";
const ANSWER_CONTEXT_LAYER = "answer-context";
const ANSWER_SELECTED_LAYER = "answer-selected";
const ANSWER_NEAREST_LAYER = "answer-nearest-source";
const CATALOG_PREVIEW_DISPLAY_CRS = "EPSG:4326" as const;
const CATALOG_COLORS = ["#007c91", "#7f5af0", "#ca6702", "#2a9d8f", "#9b5de5"];

const BASEMAP_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    "pdok-brt-water": {
      type: "raster",
      tiles: [
        "https://service.pdok.nl/kadaster/brt-achtergrondkaart/wmts/v2_0/water/EPSG:3857/{z}/{x}/{y}.png",
      ],
      tileSize: 256,
      attribution:
        '<a href="https://www.pdok.nl/introductie/-/article/basisregistratie-topografie-achtergrondkaarten-brt-a-" target="_blank" rel="noopener">BRT Achtergrondkaart © Kadaster / PDOK</a> · <a href="https://creativecommons.org/licenses/by/4.0/" target="_blank" rel="noopener">CC BY 4.0</a>',
    },
  },
  layers: [
    {
      id: "pdok-brt-water",
      type: "raster",
      source: "pdok-brt-water",
    },
  ],
};

type PreviewState = {
  active: boolean;
  data: {
    geojson: GeoJsonFeatureCollection;
    displayCrs: typeof CATALOG_PREVIEW_DISPLAY_CRS;
  } | null;
  loading: boolean;
  refusal: string;
  error: string;
};

export function MapPane({
  answerMap,
  answerMapLoading,
  answerMapError,
}: {
  answerMap: AnswerMapFeatureCollection | null;
  answerMapLoading: boolean;
  answerMapError: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const [layers, setLayers] = useState<CatalogLayer[]>([]);
  const [previews, setPreviews] = useState<Record<string, PreviewState>>({});
  const [legendError, setLegendError] = useState("");

  useEffect(() => {
    if (!containerRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: BASEMAP_STYLE,
      center: AMSTERDAM_CENTER,
      zoom: 10.5,
      minZoom: 10,
    });
    map.addControl(new maplibregl.NavigationControl(), "top-right");
    map.on("load", () => setMapReady(true));
    mapRef.current = map;
    return () => {
      mapRef.current = null;
      map.remove();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    getCatalogLayers()
      .then((listing) => {
        if (!cancelled) setLayers(listing.layers);
      })
      .catch((caught: unknown) => {
        if (!cancelled) setLegendError(errorMessage(caught));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Answer-map overlay: reuse the existing GeoJSON source when present so
  // re-renders swap data in place, and fit the view to the answer bounds.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    if (!answerMap) {
      removeAnswerLayers(map);
      return;
    }
    const overlay = answerMapSources(answerMap);
    const activeLayerIds = answerLayerIds(answerMap);
    const existing = map.getSource(overlay.source.id) as GeoJSONSource | undefined;
    if (existing && activeLayerIds.every((id) => map.getLayer(id))) {
      existing.setData(overlay.source.specification.data);
    } else {
      removeAnswerLayers(map);
      addOverlay(map, overlay);
    }

    const bounds = coordinateBounds(answerMap);
    if (bounds) map.fitBounds(bounds, { padding: 56, maxZoom: 14, duration: 700 });

    const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
    const showPopup = (event: MapLayerMouseEvent) => {
      const feature = event.features?.[0];
      if (!feature) return;
      popup
        .setLngLat(event.lngLat)
        .setText(answerTooltip(feature))
        .addTo(map);
    };
    const hidePopup = () => popup.remove();
    for (const layerId of activeLayerIds) {
      map.on("mousemove", layerId, showPopup);
      map.on("mouseleave", layerId, hidePopup);
    }
    return () => {
      for (const layerId of activeLayerIds) {
        map.off("mousemove", layerId, showPopup);
        map.off("mouseleave", layerId, hidePopup);
      }
      popup.remove();
    };
  }, [answerMap, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    layers.forEach((layer, index) => {
      const key = layerKey(layer);
      const preview = previews[key];
      const overlay = previewOverlaySpecs(
        layer,
        preview?.data?.geojson ?? { type: "FeatureCollection", features: [] },
        CATALOG_COLORS[index % CATALOG_COLORS.length],
      );
      if (!preview?.active || !preview.data) {
        removeOverlay(map, overlay);
        return;
      }
      const readyOverlay = previewOverlaySpecs(
        layer,
        preview.data.geojson,
        CATALOG_COLORS[index % CATALOG_COLORS.length],
      );
      const existing = map.getSource(readyOverlay.source.id) as GeoJSONSource | undefined;
      if (existing) {
        existing.setData(readyOverlay.source.specification.data);
        return;
      }
      // Catalog previews are inserted beneath the answer layers so the
      // answer styling always stays on top.
      const beforeAnswer = [ANSWER_CONTEXT_LAYER, ANSWER_NEAREST_LAYER].find(
        (id) => map.getLayer(id),
      );
      addOverlay(map, readyOverlay, beforeAnswer);
    });
  }, [layers, mapReady, previews]);

  async function toggleLayer(layer: CatalogLayer) {
    const key = layerKey(layer);
    const current = previews[key];
    if (current?.data) {
      setPreviews((value) => ({
        ...value,
        [key]: { ...current, active: !current.active },
      }));
      return;
    }
    setPreviews((value) => ({
      ...value,
      [key]: { active: false, data: null, loading: true, refusal: "", error: "" },
    }));
    try {
      const geojson = await getCatalogLayerPreview(layer.dataset, layer.feature_type);
      const data = catalogPreviewData(geojson);
      setPreviews((value) => ({
        ...value,
        [key]: {
          active: true,
          data,
          loading: false,
          refusal: "",
          error: "",
        },
      }));
    } catch (caught) {
      const refusal = caught instanceof ApiError && caught.status === 413;
      setPreviews((value) => ({
        ...value,
        [key]: {
          active: false,
          data: null,
          loading: false,
          refusal: refusal ? errorMessage(caught) : "",
          error: refusal ? "" : errorMessage(caught),
        },
      }));
    }
  }

  const answerCounts = useMemo(() => {
    if (!answerMap) return null;
    if ("answer_kind" in answerMap) {
      return { kind: "nearest" as const, sources: answerMap.features.length };
    }
    return answerMap.features.reduce(
      (counts, feature) => {
        counts[feature.properties.is_selected ? "selected" : "context"] += 1;
        return counts;
      },
      { kind: "count" as const, selected: 0, context: 0 },
    );
  }, [answerMap]);

  return (
    <section className="map-workspace" aria-label="Interactive map">
      <div ref={containerRef} className="map-canvas" aria-label="Amsterdam basemap" />
      <details className="map-legend" open>
        <summary>Available Data</summary>
        {legendError && <p role="alert">Catalog unavailable: {legendError}</p>}
        {!legendError && !layers.length && <p>Loading Catalog layers…</p>}
        <ul>
          {layers.map((layer, index) => {
            const preview = previews[layerKey(layer)];
            return (
              <li key={layerKey(layer)}>
                <label>
                  <input
                    type="checkbox"
                    checked={Boolean(preview?.active)}
                    disabled={Boolean(preview?.loading || preview?.refusal)}
                    onChange={() => void toggleLayer(layer)}
                  />
                  <span
                    className="layer-swatch"
                    style={{ background: CATALOG_COLORS[index % CATALOG_COLORS.length] }}
                    aria-hidden="true"
                  />
                  <span className="geometry-icon" aria-hidden="true">
                    {geometryIcon(layer.geometry_types)}
                  </span>
                  <span>
                    <strong>{layer.semantic_label ?? layer.name}</strong>
                    {layer.name_language === "nl" && <small> (Dutch source name)</small>}
                    <small>{layer.feature_count.toLocaleString("en")} features</small>
                  </span>
                </label>
                {preview?.loading && <small role="status">Loading preview…</small>}
                {preview?.refusal && <small>{preview.refusal}</small>}
                {preview?.error && <small role="alert">{preview.error}</small>}
              </li>
            );
          })}
        </ul>
      </details>
      {(answerMapLoading || answerMapError || answerCounts) && (
        <div className="answer-map-legend" aria-label="Answer Map legend">
          {answerMapLoading && <p role="status">Loading Answer Map…</p>}
          {answerMapError && <p role="alert">Answer Map unavailable: {answerMapError}</p>}
          {answerCounts && (
            <>
              <strong>Answer Map</strong>
              {answerCounts.kind === "nearest" ? (
                <span><i className="zero-swatch" />Sources by nearest distance ({answerCounts.sources})</span>
              ) : (
                <>
                  <span><i className="zero-swatch" />In answer ({answerCounts.selected})</span>
                  <span><i className="context-swatch" />Context ({answerCounts.context})</span>
                </>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}

export { classifyAnswerFeature } from "./mapOverlaySpecs";

// Guard against a preview that is not actually in display coordinates:
// any coordinate outside the WGS84 domain rejects the whole preview.
function catalogPreviewData(geojson: GeoJsonFeatureCollection): PreviewState["data"] {
  let invalidCoordinate = false;
  geojson.features.forEach((feature) => {
    visitCoordinates(feature.geometry?.coordinates, (longitude, latitude) => {
      if (longitude < -180 || longitude > 180 || latitude < -90 || latitude > 90) {
        invalidCoordinate = true;
      }
    });
  });
  if (invalidCoordinate) {
    throw new Error("Catalog preview must use EPSG:4326 display coordinates.");
  }
  return { geojson, displayCrs: CATALOG_PREVIEW_DISPLAY_CRS };
}

function answerTooltip(feature: MapGeoJSONFeature) {
  const properties = feature.properties as Record<string, unknown>;
  const identityText = String(properties._geoqa_identity ?? "Feature");
  return properties.nearest_distance_m === undefined
    ? `${identityText}: ${String(properties.count ?? "unknown")}`
    : `${identityText}: ${String(properties.nearest_distance_m)} m`;
}

function coordinateBounds(map: AnswerMapFeatureCollection) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  visitCoordinates(map.features.map((feature) => feature.geometry.coordinates), (x, y) => {
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x);
    maxY = Math.max(maxY, y);
  });
  return Number.isFinite(minX)
    ? ([[minX, minY], [maxX, maxY]] as [[number, number], [number, number]])
    : null;
}

// Walk arbitrarily nested GeoJSON coordinate arrays, calling visit for each
// finite [x, y] pair.
function visitCoordinates(value: unknown, visit: (x: number, y: number) => void) {
  if (!Array.isArray(value)) return;
  if (
    value.length >= 2 &&
    typeof value[0] === "number" &&
    typeof value[1] === "number" &&
    Number.isFinite(value[0]) &&
    Number.isFinite(value[1])
  ) {
    visit(value[0], value[1]);
    return;
  }
  value.forEach((child) => visitCoordinates(child, visit));
}

function removeAnswerLayers(map: MapLibreMap) {
  for (const id of [ANSWER_SELECTED_LAYER, ANSWER_CONTEXT_LAYER, ANSWER_NEAREST_LAYER]) {
    if (map.getLayer(id)) map.removeLayer(id);
  }
  if (map.getSource(ANSWER_SOURCE)) map.removeSource(ANSWER_SOURCE);
}

function answerLayerIds(answerMap: AnswerMapFeatureCollection): string[] {
  return "answer_kind" in answerMap
    ? [ANSWER_NEAREST_LAYER]
    : [ANSWER_CONTEXT_LAYER, ANSWER_SELECTED_LAYER];
}

function addOverlay(
  map: MapLibreMap,
  overlay: OverlaySpecifications,
  beforeId?: string,
) {
  map.addSource(overlay.source.id, overlay.source.specification);
  overlay.layers.forEach((layer) => map.addLayer(layer, beforeId));
}

function removeOverlay(map: MapLibreMap, overlay: OverlaySpecifications) {
  for (const layer of [...overlay.layers].reverse()) {
    if (map.getLayer(layer.id)) map.removeLayer(layer.id);
  }
  if (map.getSource(overlay.source.id)) map.removeSource(overlay.source.id);
}

function geometryIcon(types: string[]) {
  if (types.some((type) => type.includes("Point"))) return "●";
  if (types.some((type) => type.includes("Line"))) return "⌁";
  return "⬡";
}

function errorMessage(caught: unknown) {
  return caught instanceof Error ? caught.message : "The map request failed.";
}
