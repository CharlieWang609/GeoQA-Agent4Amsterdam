// SPDX-License-Identifier: GPL-3.0-only

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "maplibre-gl/dist/maplibre-gl.css";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("GeoQA Agent root element is missing.");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
