// SPDX-License-Identifier: GPL-3.0-only

import { useEffect, useMemo, useRef, useState } from "react";
import * as maplibregl from "maplibre-gl";
import type {
  GeoJSONSource,
  MapGeoJSONFeature,
  MapLayerMouseEvent,
  MapLibreMap,
  SourceSpecification,
  StyleSpecification,
} from "maplibre-gl";

import {
  ApiError,
  catalogRasterViewUrl,
  getCatalogLayerPreview,
  getCatalogLayers,
  getRasterView,
} from "./api";
import type {
  AnswerMapFeatureCollection,
  CatalogLayer,
  GeoJsonFeatureCollection,
  ObservationItem,
  RasterView,
} from "./types";
import {
  answerMapSources,
  answerSymbology,
  layerKey,
  previewOverlaySpecs,
  rasterOverlaySpecs,
  type OverlaySpecifications,
} from "./mapOverlaySpecs";
import { errorMessage, readableLabel } from "./jsonRecords";
import "./mapSetup";

const AMSTERDAM_CENTER: [number, number] = [4.9, 52.37];
const ANSWER_SOURCE = "answer-map";
const ANSWER_LAYERS = ["answer-context", "answer-selected", "answer-hover", "answer-line", "answer-point"];
const NO_OBSERVATIONS: ObservationItem[] = [];
const CATALOG_COLORS = [
  "#007c91",
  "#7f5af0",
  "#ca6702",
  "#2a9d8f",
  "#9b5de5",
  "#bc4749",
  "#3a5a40",
];

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
  data: GeoJsonFeatureCollection | null;
  loading: boolean;
  refusal: string;
  error: string;
};

// A raster view once requested: the catalog raster/collection layers and
// the session's observation items share this state, keyed by overlay.
type RasterState = {
  active: boolean;
  view: RasterView | null;
  loading: boolean;
  error: string;
};

export function MapPane({
  answerMap,
  answerMapLoading,
  answerMapError,
  observations = NO_OBSERVATIONS,
}: {
  answerMap: AnswerMapFeatureCollection | null;
  answerMapLoading: boolean;
  answerMapError: string;
  observations?: ObservationItem[];
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const [layers, setLayers] = useState<CatalogLayer[]>([]);
  const [previews, setPreviews] = useState<Record<string, PreviewState>>({});
  const [rasters, setRasters] = useState<Record<string, RasterState>>({});
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
        if (!cancelled) setLegendError(errorMessage(caught, "The map request failed."));
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

    // Hovering emphasises the feature under the pointer through feature
    // state and names it in a tooltip.
    const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
    let hovered: string | number | undefined;
    const setHover = (id: string | number | undefined, hover: boolean) => {
      if (id !== undefined && map.getSource(ANSWER_SOURCE)) {
        map.setFeatureState({ source: ANSWER_SOURCE, id }, { hover });
      }
    };
    const showPopup = (event: MapLayerMouseEvent) => {
      const feature = event.features?.[0];
      if (!feature) return;
      if (feature.id !== hovered) {
        setHover(hovered, false);
        setHover(feature.id, true);
        hovered = feature.id;
      }
      map.getCanvas().style.cursor = "pointer";
      popup
        .setLngLat(event.lngLat)
        .setText(answerTooltip(feature))
        .addTo(map);
    };
    const hidePopup = () => {
      setHover(hovered, false);
      hovered = undefined;
      map.getCanvas().style.cursor = "";
      popup.remove();
    };
    for (const layerId of activeLayerIds) {
      map.on("mousemove", layerId, showPopup);
      map.on("mouseleave", layerId, hidePopup);
    }
    return () => {
      for (const layerId of activeLayerIds) {
        map.off("mousemove", layerId, showPopup);
        map.off("mouseleave", layerId, hidePopup);
      }
      hidePopup();
    };
  }, [answerMap, mapReady]);

  // Catalog previews name the feature under the pointer; the handlers are
  // stable so they can be detached with the overlay.
  const previewHover = useRef<{ show: (event: MapLayerMouseEvent) => void; hide: () => void } | null>(null);
  if (previewHover.current === null) {
    const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
    previewHover.current = {
      show: (event) => {
        const map = mapRef.current;
        const feature = event.features?.[0];
        if (!map || !feature) return;
        map.getCanvas().style.cursor = "pointer";
        popup.setLngLat(event.lngLat).setText(previewTooltip(feature)).addTo(map);
      },
      hide: () => {
        const map = mapRef.current;
        if (map) map.getCanvas().style.cursor = "";
        popup.remove();
      },
    };
  }

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    layers.forEach((layer, index) => {
      const key = layerKey(layer);
      const preview = previews[key];
      const overlay = previewOverlaySpecs(
        layer,
        preview?.data ?? { type: "FeatureCollection", features: [] },
        CATALOG_COLORS[index % CATALOG_COLORS.length],
      );
      const hover = previewHover.current!;
      if (!preview?.active || !preview.data) {
        overlay.layers.forEach((item) => {
          map.off("mousemove", item.id, hover.show);
          map.off("mouseleave", item.id, hover.hide);
        });
        removeOverlay(map, overlay);
        return;
      }
      const existing = map.getSource(overlay.source.id) as GeoJSONSource | undefined;
      if (existing) {
        existing.setData(overlay.source.specification.data);
        return;
      }
      // Catalog previews are inserted beneath the answer layers so the
      // answer styling always stays on top.
      const beforeAnswer = ANSWER_LAYERS.find((id) => map.getLayer(id));
      addOverlay(map, overlay, beforeAnswer);
      overlay.layers.forEach((item) => {
        map.on("mousemove", item.id, hover.show);
        map.on("mouseleave", item.id, hover.hide);
      });
    });
  }, [layers, mapReady, previews]);

  // Raster views sit beneath every vector overlay so outlines and the
  // answer styling stay readable over imagery.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady) return;
    Object.entries(rasters).forEach(([key, state]) => {
      if (!state.view) return;
      const overlay = rasterOverlaySpecs(key, state.view);
      if (!state.active) {
        removeOverlay(map, overlay);
        return;
      }
      if (map.getSource(overlay.source.id)) return;
      addOverlay(map, overlay, firstVectorLayerId(map));
    });
  }, [rasters, mapReady]);

  // Observation overlays of a previous session are switched off when the
  // listing changes; their keys carry the session, so nothing is reused.
  useEffect(() => {
    const keep = new Set(observations.map(observationKey));
    setRasters((value) => {
      const stale = Object.keys(value).filter(
        (key) => key.startsWith("observation:") && !keep.has(key) && value[key].active,
      );
      if (!stale.length) return value;
      return { ...value, ...Object.fromEntries(stale.map((key) => [key, { ...value[key], active: false }])) };
    });
  }, [observations]);

  async function toggleRaster(key: string, viewUrl: string) {
    const current = rasters[key];
    if (current?.view) {
      setRasters((value) => ({ ...value, [key]: { ...current, active: !current.active } }));
      return;
    }
    setRasters((value) => ({ ...value, [key]: { active: false, view: null, loading: true, error: "" } }));
    try {
      const view = await getRasterView(viewUrl);
      setRasters((value) => ({ ...value, [key]: { active: true, view, loading: false, error: "" } }));
    } catch (caught) {
      setRasters((value) => ({
        ...value,
        [key]: { active: false, view: null, loading: false, error: errorMessage(caught, "The map request failed.") },
      }));
    }
  }

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
          refusal: refusal ? errorMessage(caught, "The map request failed.") : "",
          error: refusal ? "" : errorMessage(caught, "The map request failed."),
        },
      }));
    }
  }

  // Group the flat Catalog listing by source dataset, preserving each
  // layer's global index so preview colors stay stable across grouping.
  const datasetGroups = useMemo(() => {
    const groups = new Map<
      string,
      {
        title: string | null;
        titleLanguage: "en" | "nl";
        entries: { layer: CatalogLayer; index: number }[];
      }
    >();
    layers.forEach((layer, index) => {
      const group = groups.get(layer.dataset) ?? {
        title: layer.dataset_title,
        titleLanguage: layer.dataset_title_language,
        entries: [],
      };
      if (group.title === null && layer.dataset_title !== null) {
        group.title = layer.dataset_title;
        group.titleLanguage = layer.dataset_title_language;
      }
      group.entries.push({ layer, index });
      groups.set(layer.dataset, group);
    });
    return [...groups.entries()];
  }, [layers]);

  const answerLegend = useMemo(
    () =>
      answerMap
        ? {
            title: answerMap.title,
            valueLabel: readableLabel(answerMap.value_field),
            count: answerMap.features.length,
            symbology: answerSymbology(answerMap),
          }
        : null,
    [answerMap],
  );

  return (
    <section className="map-workspace" aria-label="Interactive map">
      <div ref={containerRef} className="map-canvas" aria-label="Amsterdam basemap" />
      <div className="map-panels">
      <details className="map-legend" open>
        <summary>Available Data</summary>
        {legendError && <p role="alert">Catalog unavailable: {legendError}</p>}
        {!legendError && !layers.length && <p>Loading Catalog layers…</p>}
        <ul className="catalog-groups">
          {datasetGroups.map(([dataset, group]) => (
            <li key={dataset}>
              <details className="catalog-group">
                <summary>
                  <strong>
                    {group.title ?? dataset}
                    {group.title !== null && group.titleLanguage === "nl" && (
                      <small> (Dutch source title)</small>
                    )}
                  </strong>
                  <small>
                    {group.entries.length}{" "}
                    {group.entries.length === 1 ? "layer" : "layers"}
                  </small>
                </summary>
                <ul>
                  {group.entries.map(({ layer, index }) => {
                    const vector = layer.data_kind === "vector";
                    const preview = previews[layerKey(layer)];
                    const raster = rasters[layerKey(layer)];
                    return (
                      <li key={layerKey(layer)}>
                        <label>
                          <input
                            type="checkbox"
                            checked={Boolean(vector ? preview?.active : raster?.active)}
                            disabled={Boolean(vector ? preview?.loading || preview?.refusal : raster?.loading)}
                            onChange={() =>
                              void (vector
                                ? toggleLayer(layer)
                                : toggleRaster(layerKey(layer), catalogRasterViewUrl(layer.dataset, layer.feature_type)))}
                          />
                          <span
                            className="layer-swatch"
                            style={{ background: CATALOG_COLORS[index % CATALOG_COLORS.length] }}
                            aria-hidden="true"
                          />
                          <span className="geometry-icon" aria-hidden="true">
                            {layer.data_kind === "vector" ? geometryIcon(layer.geometry_types) : "▦"}
                          </span>
                          <span>
                            <strong>{layer.semantic_label ?? layer.name}</strong>
                            {layer.name_language === "nl" && <small> (Dutch source name)</small>}
                            <small>{layer.feature_count === null ? `${layer.bands.length} band${layer.bands.length === 1 ? "" : "s"}` : `${layer.feature_count.toLocaleString("en")} features`}</small>
                          </span>
                        </label>
                        {preview?.loading && <small role="status">Loading preview…</small>}
                        {preview?.refusal && <small>{preview.refusal}</small>}
                        {preview?.error && <small role="alert">{preview.error}</small>}
                        <RasterDetails state={raster} />
                      </li>
                    );
                  })}
                </ul>
              </details>
            </li>
          ))}
        </ul>
      </details>
      {(answerMapLoading || answerMapError || answerLegend) && (
        <details className="map-legend" open aria-label="Answer Map legend">
          <summary>Answer Map</summary>
          <div className="answer-legend">
            {answerMapLoading && <p role="status">Loading Answer Map…</p>}
            {answerMapError && <p role="alert">Answer Map unavailable: {answerMapError}</p>}
            {answerLegend && (
              <>
                <strong>{answerLegend.title}</strong>
                {answerLegend.symbology.kind === "selection" && (
                  <>
                    <span><i className="zero-swatch" />Matches the question ({answerLegend.symbology.selected})</span>
                    <span><i className="context-swatch" />Other features ({answerLegend.symbology.context})</span>
                  </>
                )}
                {answerLegend.symbology.kind === "ramp" && (
                  <div className="raster-ramp">
                    <i
                      style={{
                        background:
                          answerLegend.symbology.colors.length > 1
                            ? `linear-gradient(90deg, ${answerLegend.symbology.colors.join(", ")})`
                            : answerLegend.symbology.colors[0],
                      }}
                      aria-hidden="true"
                    />
                    <small>
                      {answerLegend.valueLabel}: {formatValue(answerLegend.symbology.low)} to {formatValue(answerLegend.symbology.high)} ({answerLegend.count} features)
                    </small>
                  </div>
                )}
                {answerLegend.symbology.kind === "classes" && (
                  <ul className="raster-classes">
                    {answerLegend.symbology.classes.map((entry) => (
                      <li key={entry.value}>
                        <i style={{ background: entry.color }} aria-hidden="true" />
                        {answerLegend.valueLabel} {formatValue(entry.value)}
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </div>
        </details>
      )}
      {observations.length > 0 && (
        <details className="map-legend" open>
          <summary>Observation Data</summary>
          <ul>
            {observations.map((item) => {
              const key = observationKey(item);
              const state = rasters[key];
              return (
                <li key={key}>
                  <label>
                    <input
                      type="checkbox"
                      checked={Boolean(state?.active)}
                      disabled={Boolean(state?.loading)}
                      onChange={() => void toggleRaster(key, item.view_url)}
                    />
                    <span className="layer-swatch" style={{ background: "#45636d" }} aria-hidden="true" />
                    <span className="geometry-icon" aria-hidden="true">{item.kind === "raster" ? "▦" : "🛰"}</span>
                    <span>
                      <strong>{item.label}</strong>
                      <small>
                        {item.origin === "input" ? "Input layer" : `Step ${item.step_id}`} · {kindLabel(item.kind)}
                      </small>
                    </span>
                  </label>
                  <RasterDetails state={state} />
                </li>
              );
            })}
          </ul>
        </details>
      )}
      </div>
    </section>
  );
}

function RasterDetails({ state }: { state?: RasterState }) {
  if (!state) return null;
  return (
    <>
      {state.loading && <small role="status">Loading raster view…</small>}
      {state.error && <small role="alert">{state.error}</small>}
      {state.view && <RasterLegend view={state.view} />}
    </>
  );
}

// How to read the overlay: the visible bands, a class palette, or the
// value ramp; for satellite data also the scenes behind it.
function RasterLegend({ view }: { view: RasterView }) {
  const { style, scenes } = view;
  return (
    <div className="raster-legend">
      <small>{view.label}</small>
      {style.type === "rgb" && <small>True colour ({style.bands.join(", ")})</small>}
      {style.type === "ramp" && (
        <div className="raster-ramp">
          <i style={{ background: `linear-gradient(90deg, ${style.colors.join(", ")})` }} aria-hidden="true" />
          <small>{style.band}: {formatValue(style.min)} to {formatValue(style.max)}</small>
        </div>
      )}
      {style.type === "classes" && (
        <ul className="raster-classes">
          {style.classes.map((entry) => (
            <li key={entry.value}><i style={{ background: entry.color }} aria-hidden="true" />{entry.label}</li>
          ))}
        </ul>
      )}
      {scenes && (
        <details className="raster-scenes">
          <summary>
            {scenes.length} scenes{scenes.length ? `, ${scenes[0].date} to ${scenes[scenes.length - 1].date}` : ""}
          </summary>
          <ul>
            {scenes.map((scene) => (
              <li key={scene.id}>
                {scene.date} · {scene.id}
                {scene.cloud_percent === null ? "" : ` · ${scene.cloud_percent.toFixed(1)}% cloud`}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function observationKey(item: ObservationItem) {
  return `observation:${item.view_url}`;
}

function kindLabel(kind: ObservationItem["kind"]) {
  return kind === "raster" ? "raster" : kind === "image" ? "satellite image" : "image collection";
}

function formatValue(value: number) {
  return value.toLocaleString("en", { maximumFractionDigits: 2 });
}

// The lowest vector overlay currently on the map, so rasters go beneath.
function firstVectorLayerId(map: MapLibreMap) {
  return map
    .getStyle()
    .layers.find((layer) => layer.id.startsWith("catalog-") || ANSWER_LAYERS.includes(layer.id))?.id;
}

// Guard against a preview that is not actually in display coordinates:
// any coordinate outside the WGS84 domain rejects the whole preview.
function catalogPreviewData(geojson: GeoJsonFeatureCollection): GeoJsonFeatureCollection {
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
  return geojson;
}

function answerTooltip(feature: MapGeoJSONFeature) {
  const properties = feature.properties as Record<string, unknown>;
  const label = String(properties._geoqa_label ?? properties._geoqa_identity ?? "Feature");
  const value = typeof properties.value === "number" ? formatValue(properties.value) : "unknown";
  return `${label} · ${readableLabel(String(properties.value_field))}: ${value}`;
}

// A preview names its feature when the layer has a name; otherwise its
// identity values.
function previewTooltip(feature: MapGeoJSONFeature) {
  const properties = feature.properties as Record<string, unknown>;
  const name = properties.name;
  if (typeof name === "string" && name.trim()) return name;
  const identity = Object.entries(properties)
    .filter(([key]) => key !== "name")
    .map(([, value]) => String(value));
  return identity.join(" / ") || "Feature";
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
  for (const id of ANSWER_LAYERS) {
    if (map.getLayer(id)) map.removeLayer(id);
  }
  if (map.getSource(ANSWER_SOURCE)) map.removeSource(ANSWER_SOURCE);
}

function answerLayerIds(_answerMap: AnswerMapFeatureCollection): string[] {
  return ANSWER_LAYERS;
}

function addOverlay(
  map: MapLibreMap,
  overlay: OverlaySpecifications<SourceSpecification>,
  beforeId?: string,
) {
  map.addSource(overlay.source.id, overlay.source.specification);
  overlay.layers.forEach((layer) => map.addLayer(layer, beforeId));
}

function removeOverlay(map: MapLibreMap, overlay: OverlaySpecifications<SourceSpecification>) {
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

