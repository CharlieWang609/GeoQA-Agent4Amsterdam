// SPDX-License-Identifier: GPL-3.0-only

// Map pane tests: catalog legend, preview toggles, answer overlay states.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, getCatalogLayerPreview, getCatalogLayers } from "./api";
import { MapPane, classifyAnswerFeature } from "./MapPane";
import { answerMap } from "./test/fixtures";
import type { CatalogLayer } from "./types";

const mapDouble = vi.hoisted(() => ({
  addControl: vi.fn(),
  addLayer: vi.fn(),
  addSource: vi.fn(),
  fitBounds: vi.fn(),
  getLayer: vi.fn(),
  getSource: vi.fn(),
  off: vi.fn(),
  on: vi.fn((event: string, layerOrHandler: unknown, handler?: unknown) => {
    if (event === "load" && typeof layerOrHandler === "function") {
      layerOrHandler();
    }
    void handler;
  }),
  remove: vi.fn(),
  removeLayer: vi.fn(),
  removeSource: vi.fn(),
}));

const mapConstructor = vi.hoisted(() => vi.fn((_options: unknown) => mapDouble));
const setWorkerUrl = vi.hoisted(() => vi.fn());

vi.mock("maplibre-gl", () => ({
  Map: mapConstructor,
  NavigationControl: vi.fn(),
  Popup: vi.fn(() => ({
    addTo: vi.fn().mockReturnThis(),
    remove: vi.fn(),
    setLngLat: vi.fn().mockReturnThis(),
    setText: vi.fn().mockReturnThis(),
  })),
  setWorkerUrl,
}));

vi.mock("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url", () => ({
  default: "/assets/maplibre-gl-worker-bundled.js",
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getCatalogLayerPreview: vi.fn(),
    getCatalogLayers: vi.fn(),
  };
});

describe("Map workspace", () => {
  beforeEach(() => {
    vi.mocked(getCatalogLayers).mockResolvedValue({
      catalog_version: "catalog-v1",
      layers: [catalogLayer("Neighborhoods", 518), catalogLayer("Large layer", 20_000)],
    });
    vi.mocked(getCatalogLayerPreview).mockReset();
    mapDouble.getLayer.mockReturnValue(undefined);
    mapDouble.getSource.mockReturnValue(undefined);
  });

  it("initializes MapLibre and wires Catalog preview toggles including 413 refusal", async () => {
    const preview = { type: "FeatureCollection" as const, features: [] };
    vi.mocked(getCatalogLayerPreview)
      .mockResolvedValueOnce(preview)
      .mockRejectedValueOnce(new ApiError(413, "layer too large to preview"));
    const user = userEvent.setup();

    render(<MapPane answerMap={answerMap()} answerMapError="" answerMapLoading={false} />);

    expect(await screen.findByText("Available Data")).toBeVisible();
    expect(mapConstructor).toHaveBeenCalledTimes(1);
    expect(setWorkerUrl).toHaveBeenCalledTimes(1);
    expect(setWorkerUrl).toHaveBeenCalledWith(
      "/assets/maplibre-gl-worker-bundled.js",
    );
    expect(setWorkerUrl.mock.invocationCallOrder[0]).toBeLessThan(
      mapConstructor.mock.invocationCallOrder[0],
    );
    const mapOptions = mapConstructor.mock.calls[0][0] as { style: unknown };
    const mapStyle = JSON.stringify(mapOptions.style);
    expect(mapStyle).toContain("/water/EPSG:3857/{z}/{x}/{y}.png");
    expect(mapStyle).toContain("BRT Achtergrondkaart © Kadaster / PDOK");
    expect(mapStyle).toContain("CC BY 4.0");
    await user.click(await screen.findByRole("checkbox", { name: /neighborhoods/i }));
    expect(getCatalogLayerPreview).toHaveBeenCalledWith("gebieden", "buurten");
    await waitFor(() => expect(mapDouble.addSource).toHaveBeenCalled());
    expect(screen.queryByText("Preview display CRS: EPSG:4326")).not.toBeInTheDocument();

    const largeToggle = screen.getByRole("checkbox", { name: /large layer/i });
    await user.click(largeToggle);
    expect(await screen.findByText(/layer too large to preview/i)).toBeVisible();
    expect(largeToggle).toBeDisabled();
  });

  it("renders all five governed Catalog layers in the anonymous map legend", async () => {
    vi.mocked(getCatalogLayers).mockResolvedValue({
      catalog_version: "catalog-showcase-v1",
      layers: [
        catalogLayer("Neighborhoods", 518, "gebieden", "buurten", "neighborhood"),
        catalogLayer("Sports locations", 841, "sport", "openbaresportplek", "sports location"),
        catalogLayer("Provider", 1_799, "sport", "aanbieder", "sports provider"),
        catalogLayer("Gymnasiums", 90, "sport", "gymzaal", "gymnasium"),
        catalogLayer("Swimming pools", 18, "sport", "zwembad", "swimming pool"),
      ],
    });

    render(<MapPane answerMap={null} answerMapError="" answerMapLoading={false} />);

    const legend = await screen.findByText("Available Data");
    const legendDetails = legend.closest("details")!;
    for (const name of [
      "neighborhood",
      "sports location",
      "sports provider",
      "gymnasium",
      "swimming pool",
    ]) {
      expect(within(legendDetails).getByText(name)).toBeVisible();
    }
    expect(within(legendDetails).queryByText("Provider")).not.toBeInTheDocument();
    expect(screen.getAllByRole("checkbox")).toHaveLength(5);
  });

  it("prefers the governed semantic label and falls back to the resolved English name", async () => {
    vi.mocked(getCatalogLayers).mockResolvedValue({
      catalog_version: "catalog-showcase-v1",
      layers: [
        catalogLayer("Provider", 1_799, "sport", "aanbieder", "sports provider"),
        catalogLayer("Fallback English name", 1, "fallback", "layer", null),
      ],
    });

    render(<MapPane answerMap={null} answerMapError="" answerMapLoading={false} />);

    const legend = await screen.findByText("Available Data");
    const legendDetails = legend.closest("details")!;
    expect(within(legendDetails).getByText("sports provider")).toBeVisible();
    expect(within(legendDetails).queryByText("Provider")).not.toBeInTheDocument();
    expect(within(legendDetails).getByText("Fallback English name")).toBeVisible();
  });

  it("classifies Answer Map features from their contractual selection flag", () => {
    const [context, selected] = answerMap().features;

    expect(classifyAnswerFeature(context)).toBe("context");
    expect(classifyAnswerFeature(selected)).toBe("selected");
  });

  it("rejects a Catalog preview outside the contractual EPSG:4326 display range", async () => {
    vi.mocked(getCatalogLayerPreview).mockResolvedValue({
      type: "FeatureCollection",
      features: [{
        type: "Feature",
        geometry: { type: "Point", coordinates: [121_000, 487_000] },
        properties: {},
      }],
    });
    const user = userEvent.setup();

    render(<MapPane answerMap={null} answerMapError="" answerMapLoading={false} />);
    await user.click(await screen.findByRole("checkbox", { name: /neighborhoods/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/must use EPSG:4326/i);
    expect(mapDouble.addSource).not.toHaveBeenCalled();
  });
});

function catalogLayer(
  name: string,
  featureCount: number,
  dataset = name === "Neighborhoods" ? "gebieden" : "large",
  featureType = name === "Neighborhoods" ? "buurten" : "features",
  semanticLabel: string | null = null,
): CatalogLayer {
  return {
    dataset,
    feature_type: featureType,
    name,
    name_language: "en",
    description: `${name} description`,
    description_language: "en",
    semantic_label: semanticLabel,
    geometry_types: ["Polygon"],
    feature_count: featureCount,
    dataset_version: "snapshot-v1",
    crs: "EPSG:28992",
    original_crs: "EPSG:28992",
    temporal_extent: { start: "2026-01-01", end: null },
    spatial_extent: null,
  };
}
