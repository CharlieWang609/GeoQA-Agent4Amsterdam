// SPDX-License-Identifier: GPL-3.0-only

// Anonymous shell integration: account state, Catalog legend, and previews.

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import { App } from "./App";
import type { CatalogLayer } from "./types";

const mapDouble = vi.hoisted(() => ({
  addControl: vi.fn(),
  addLayer: vi.fn(),
  addSource: vi.fn(),
  fitBounds: vi.fn(),
  getLayer: vi.fn(),
  getSource: vi.fn(),
  off: vi.fn(),
  on: vi.fn((event: string, layerOrHandler: unknown) => {
    if (event === "load" && typeof layerOrHandler === "function") {
      layerOrHandler();
    }
  }),
  remove: vi.fn(),
  removeLayer: vi.fn(),
  removeSource: vi.fn(),
}));

vi.mock("maplibre-gl", () => ({
  Map: vi.fn(() => mapDouble),
  NavigationControl: vi.fn(),
  Popup: vi.fn(() => ({
    addTo: vi.fn().mockReturnThis(),
    remove: vi.fn(),
    setLngLat: vi.fn().mockReturnThis(),
    setText: vi.fn().mockReturnThis(),
  })),
  setWorkerUrl: vi.fn(),
}));

vi.mock("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url", () => ({
  default: "/assets/maplibre-gl-worker-bundled.js",
}));

beforeEach(() => {
  vi.clearAllMocks();
  window.history.replaceState({}, "", "/sandbox");
});

it("lets an anonymous visitor inspect every governed Catalog preview", async () => {
  const layers = [
    catalogLayer("Neighborhoods", "gebieden", "buurten"),
    catalogLayer("Sports locations", "sport", "openbaresportplek"),
    catalogLayer("Sports providers", "sport", "aanbieder"),
    catalogLayer("Gymnasiums", "sport", "gymzaal"),
    catalogLayer("Swimming pools", "sport", "zwembad"),
  ];
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
    async (input) => {
      const path = String(input);
      if (path === "/api/me" || path === "/api/question-sessions") {
        return jsonResponse(
          { detail: "GitHub authentication is required." },
          401,
        );
      }
      if (path === "/api/catalog-layers") {
        return jsonResponse({ catalog_version: "catalog-v1", layers });
      }
      if (
        path.startsWith("/api/catalog-layers/")
        && path.endsWith("/preview")
      ) {
        return jsonResponse({
          type: "FeatureCollection",
          features: [{
            type: "Feature",
            geometry: { type: "Point", coordinates: [4.895, 52.375] },
            properties: { id: 1, name: "Preview feature" },
          }],
        });
      }
      throw new Error(`Unexpected request: ${path}`);
    },
  );
  const user = userEvent.setup();

  render(<App />);

  expect(await screen.findByRole("link", {
    name: /sign in with github/i,
  })).toBeVisible();
  for (const layer of layers) {
    await user.click(await screen.findByRole("checkbox", {
      name: new RegExp(layer.name, "i"),
    }));
  }
  await waitFor(() => {
    expect(fetchMock.mock.calls.filter(
      ([input]) => String(input).endsWith("/preview"),
    )).toHaveLength(5);
  });
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByLabelText(/upload|external.*url/i)).not.toBeInTheDocument();
});

function catalogLayer(
  name: string,
  dataset: string,
  featureType: string,
): CatalogLayer {
  return {
    dataset,
    feature_type: featureType,
    name,
    name_language: "en",
    description: `${name} description`,
    description_language: "en",
    semantic_label: name.toLowerCase(),
    geometry_types: ["Point"],
    feature_count: 1,
    dataset_version: "snapshot-v1",
    crs: "EPSG:28992",
    original_crs: "EPSG:28992",
    temporal_extent: { start: "2026-01-01", end: null },
    spatial_extent: null,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
