// SPDX-License-Identifier: GPL-3.0-only

import * as maplibregl from "maplibre-gl";
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

// Serve the MapLibre web worker from our own Vite bundle instead of a CDN
// blob, so the app stays self-hosted and the worker is cache-busted with
// the rest of the build.
maplibregl.setWorkerUrl(maplibreWorkerUrl);
