// SPDX-License-Identifier: GPL-3.0-only

import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

const defaultDevPrincipalId = "dev-sandbox-user";

export default defineConfig(({ command, mode }) => {
  const environment = loadEnv(mode, ".", "GEOQA_DEV_");
  // The dev proxy injects the Easy Auth principal header so the local
  // backend sees a signed-in user; GEOQA_DEV_ANONYMOUS=1 disables it to
  // exercise the signed-out experience.
  const injectDevPrincipal =
    command === "serve" && environment.GEOQA_DEV_ANONYMOUS !== "1";
  const proxyHeaders = injectDevPrincipal
    ? {
        "X-MS-CLIENT-PRINCIPAL-ID":
          environment.GEOQA_DEV_PRINCIPAL_ID ?? defaultDevPrincipalId,
      }
    : undefined;

  return {
    plugins: [react()],
    optimizeDeps: {
      exclude: ["maplibre-gl"],
    },
    server: {
      proxy: {
        "/api": {
          target: "http://127.0.0.1:8000",
          headers: proxyHeaders,
        },
        "/.auth": "http://127.0.0.1:8000",
      },
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/test/setup.ts",
    },
  };
});
