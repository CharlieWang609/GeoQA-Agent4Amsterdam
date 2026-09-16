// SPDX-License-Identifier: GPL-3.0-only

// Map pane tests: catalog legend, preview toggles, answer overlay states.

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, getCatalogLayerPreview, getCatalogLayers, getRasterView } from "./api";
import { MapPane } from "./MapPane";
import { classifyAnswerFeature } from "./mapOverlaySpecs";
import { answerMap, nearestAnswerMap } from "./test/fixtures";
import type { CatalogLayer, RasterView } from "./types";

const mapDouble = vi.hoisted(() => ({
  addControl: vi.fn(),
  addLayer: vi.fn(),
  addSource: vi.fn(),
  fitBounds: vi.fn(),
  getLayer: vi.fn(),
  getSource: vi.fn(),
  getCanvas: vi.fn(() => ({ style: {} })),
  getStyle: vi.fn(() => ({ layers: [] })),
  setFeatureState: vi.fn(),
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
    getRasterView: vi.fn(),
  };
});

describe("Map workspace", () => {
  beforeEach(() => {
    vi.mocked(getCatalogLayers).mockResolvedValue({
      catalog_version: "catalog-v1",
      layers: [catalogLayer("Neighborhoods", 518), catalogLayer("Large layer", 20_000)],
    });
    vi.mocked(getCatalogLayerPreview).mockReset();
    vi.mocked(getRasterView).mockReset();
    mapDouble.addSource.mockReset();
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
    // Dataset groups start collapsed; expand both before toggling layers.
    await user.click(await screen.findByText("gebieden"));
    await user.click(screen.getByText("large"));
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

  it("groups the governed Catalog layers by dataset in the map legend", async () => {
    const sportTitle = "Sport: Faciliteiten en aanbieders";
    const wasteTitle = "Afvalcontainers, putten en weeggegevens";
    vi.mocked(getCatalogLayers).mockResolvedValue({
      catalog_version: "catalog-showcase-v1",
      layers: [
        catalogLayer("Neighborhoods", 518, "gebieden", "buurten", "neighborhood"),
        catalogLayer("Containers", 29_345, "huishoudelijkafval", "container", "waste container", wasteTitle),
        catalogLayer("Sports locations", 841, "sport", "openbaresportplek", "sports location", sportTitle),
        catalogLayer("Provider", 1_799, "sport", "aanbieder", "sports provider", sportTitle),
        catalogLayer("Gymnasiums", 90, "sport", "gymzaal", "gymnasium", sportTitle),
        catalogLayer("Sports halls", 30, "sport", "hal", "sports hall", sportTitle),
        catalogLayer("Swimming pools", 18, "sport", "zwembad", "swimming pool", sportTitle),
      ],
    });

    render(<MapPane answerMap={null} answerMapError="" answerMapLoading={false} />);

    const legend = await screen.findByText("Available Data");
    const legendDetails = legend.closest("details")!;
    // One expandable group per dataset, titled by the official dataset
    // title with the dataset id as fallback.
    expect(within(legendDetails).getByText(sportTitle)).toBeInTheDocument();
    expect(within(legendDetails).getByText(wasteTitle)).toBeInTheDocument();
    expect(within(legendDetails).getByText("gebieden")).toBeInTheDocument();
    expect(within(legendDetails).getByText("5 layers")).toBeInTheDocument();
    expect(within(legendDetails).getAllByText("1 layer")).toHaveLength(2);
    for (const name of [
      "neighborhood",
      "waste container",
      "sports location",
      "sports provider",
      "gymnasium",
      "sports hall",
      "swimming pool",
    ]) {
      expect(within(legendDetails).getByText(name)).toBeInTheDocument();
    }
    expect(within(legendDetails).queryByText("Provider")).not.toBeInTheDocument();
    expect(screen.getAllByRole("checkbox")).toHaveLength(7);
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
    const user = userEvent.setup();

    const legend = await screen.findByText("Available Data");
    const legendDetails = legend.closest("details")!;
    await user.click(within(legendDetails).getByText("sport"));
    await user.click(within(legendDetails).getByText("fallback"));
    expect(within(legendDetails).getByText("sports provider")).toBeVisible();
    expect(within(legendDetails).queryByText("Provider")).not.toBeInTheDocument();
    expect(within(legendDetails).getByText("Fallback English name")).toBeVisible();
  });

  it("shows a value for every feature as a one-hue ramp in the answer legend", async () => {
    render(<MapPane answerMap={nearestAnswerMap()} answerMapError="" answerMapLoading={false} />);

    const panels = (await screen.findByText("Available Data")).closest(".map-panels") as HTMLElement;
    const legend = within(panels).getByLabelText("Answer Map legend");
    expect(within(legend).getByText("Distance m: 0 to 10 (2 features)")).toBeVisible();
    expect(within(legend).queryByText(/Matches the question/)).not.toBeInTheDocument();
  });

  it("lists the answer legend under Available Data with the answer title and plain counts", async () => {
    render(<MapPane answerMap={answerMap()} answerMapError="" answerMapLoading={false} />);

    const panels = (await screen.findByText("Available Data")).closest(".map-panels") as HTMLElement;
    const legend = within(panels).getByLabelText("Answer Map legend");
    expect(within(legend).getByText("Amsterdam neighborhoods with zero registered public sports locations")).toBeVisible();
    expect(within(legend).getByText("Matches the question (1)")).toBeVisible();
    expect(within(legend).getByText("Other features (1)")).toBeVisible();
    expect(screen.queryByText(/In answer/)).not.toBeInTheDocument();
    // The hover outline layer is part of the answer overlay.
    await waitFor(() =>
      expect(mapDouble.addLayer).toHaveBeenCalledWith(expect.objectContaining({ id: "answer-hover" }), undefined),
    );
  });

  it("classifies Answer Map features from their contractual selection flag", () => {
    const [context, selected] = answerMap().features;

    expect(classifyAnswerFeature(context)).toBe("context");
    expect(classifyAnswerFeature(selected)).toBe("selected");
  });

  it("shows raster Catalog layers and session observation data as image overlays with legends", async () => {
    vi.mocked(getCatalogLayers).mockResolvedValue({
      catalog_version: "catalog-v1",
      layers: [{
        ...catalogLayer("Elevation", 0, "ahn", "dtm", "elevation"),
        data_kind: "raster",
        feature_count: null,
        bands: ["elevation"],
        geometry_types: [],
      }],
    });
    vi.mocked(getRasterView)
      .mockResolvedValueOnce(rasterView("ramp"))
      .mockResolvedValueOnce(rasterView("classes"));
    const user = userEvent.setup();

    render(
      <MapPane
        answerMap={null}
        answerMapError=""
        answerMapLoading={false}
        observations={[{
          ref: "labels",
          origin: "step",
          step_id: "composite",
          algorithm_id: "gee:composite",
          kind: "image",
          bands: [],
          label: "composite (method=mode)",
          view_url: "/api/question-sessions/s/observations/labels",
        }]}
      />,
    );

    await user.click(await screen.findByText("ahn"));
    await user.click(screen.getByRole("checkbox", { name: /elevation/i }));
    expect(getRasterView).toHaveBeenCalledWith("/api/catalog-layers/ahn/dtm/raster-view");
    expect(await screen.findByText("elevation: 0.5 to 12.3")).toBeVisible();
    await waitFor(() =>
      expect(mapDouble.addSource).toHaveBeenCalledWith(
        expect.stringMatching(/^raster-/),
        expect.objectContaining({ type: "image", url: "data:image/png;base64,AAAA" }),
      ),
    );

    expect(screen.getByText("Observation Data")).toBeVisible();
    await user.click(screen.getByRole("checkbox", { name: /composite \(method=mode\)/i }));
    expect(getRasterView).toHaveBeenCalledWith("/api/question-sessions/s/observations/labels");
    expect(await screen.findByText("Trees")).toBeVisible();
    expect(screen.getByText("2 scenes, 2025-06-12 to 2025-08-30")).toBeVisible();
    // The double never reports added sources, so only the ids are checked.
    expect(mapDouble.addSource.mock.calls.map((call) => call[0])).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/^raster-ahn-dtm/),
        expect.stringMatching(/^raster-observation-/),
      ]),
    );
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
  datasetTitle: string | null = null,
): CatalogLayer {
  return {
    dataset,
    dataset_title: datasetTitle,
    dataset_title_language: "nl" as const,
    dataset_description: null,
    dataset_description_language: "nl" as const,
    feature_type: featureType,
    name,
    name_language: "en",
    description: `${name} description`,
    description_language: "en",
    semantic_label: semanticLabel,
    data_kind: "vector",
    bands: [],
    geometry_types: ["Polygon"],
    feature_count: featureCount,
    dataset_version: "snapshot-v1",
    crs: "EPSG:28992",
    original_crs: "EPSG:28992",
    temporal_extent: { start: "2026-01-01", end: null },
    spatial_extent: null,
  };
}

function rasterView(style: "ramp" | "classes"): RasterView {
  return {
    kind: style === "ramp" ? "raster" : "image",
    bands: style === "ramp" ? ["elevation"] : ["label"],
    style:
      style === "ramp"
        ? { type: "ramp", band: "elevation", min: 0.5, max: 12.3, colors: ["#440154", "#fde725"] }
        : { type: "classes", band: "label", classes: [{ value: 1, color: "#397d49", label: "Trees" }] },
    bounds: [[4.7, 52.3], [5.1, 52.45]],
    width: 800,
    height: 538,
    image: "data:image/png;base64,AAAA",
    scenes:
      style === "ramp"
        ? null
        : [
            { id: "20250612T103701_20250612T103656_T31UFT", date: "2025-06-12", cloud_percent: 0.6 },
            { id: "20250830T103701_20250830T103656_T31UFT", date: "2025-08-30", cloud_percent: null },
          ],
    label: style === "ramp" ? "AHN height model" : "labels",
  };
}
