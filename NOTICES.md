# Notices

GeoQA Agent is distributed under `GPL-3.0-only`. The tree contains no
vendored third-party source files or data.

## Data sources

Each source is used under its own terms; the catalog records the access class
and reuse licence of every ingested layer.

- City of Amsterdam open data, via the WFS services of
  `api.data.amsterdam.nl` (public datasets only).
- AHN, the Dutch national height model (digital surface and terrain models),
  open data.
- ESA WorldCover 10 m 2021 v200, CC BY 4.0.
- Copernicus Sentinel-2 surface reflectance, accessed through Google Earth
  Engine; contains modified Copernicus Sentinel data.
- Dynamic World V1 (Google and World Resources Institute), accessed through
  Google Earth Engine, CC BY 4.0.

## Third-party software

Installed as dependencies, not vendored: GeoPandas, Shapely and rasterio
(BSD-3-Clause), pyarrow and the Earth Engine Python client (Apache-2.0),
FastAPI and pydantic (MIT), React and Vite (MIT), MapLibre GL JS
(BSD-3-Clause). See each package for its full licence text.
