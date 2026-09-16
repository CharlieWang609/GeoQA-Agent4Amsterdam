# SPDX-License-Identifier: GPL-3.0-only

"""Administrator ingestion for the governed raster Layers.

Every raster is normalised onto one grid over the catalog's extent: the
canonical CRS, a pinned pixel size, bounds snapped outward to whole pixels.
Sources are a PDOK WCS coverage (fetched server-side rescaled) or a window
of a public cloud-optimised GeoTIFF (read remotely, reprojected locally).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx
import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_bounds

from data_pipeline.catalog import CATALOG_ELIGIBILITY_POLICY
from data_pipeline.geoparquet import CANONICAL_CRS
from data_pipeline.models import (
    AcquisitionProvenance,
    AttributeMetadata,
    CatalogLayer,
    DataKind,
    EligibilityDecision,
    QualityDiagnostic,
    QualityIndicators,
    RasterMetadata,
    RawAccessMetadata,
    RawLayerMetadata,
    TemporalExtent,
)
from data_pipeline.serialization import sha256
from data_pipeline.storage import ObjectStore

Bounds = tuple[float, float, float, float]


@dataclass(frozen=True)
class RasterDefinition:
    """Pinned source and band contract for one raster Layer."""

    dataset_id: str
    layer_id: str
    source: Literal["pdok-wcs", "cog"]
    endpoint: str
    # WCS coverage id, or the source's own name for a COG.
    coverage: str
    band: str
    band_description: str
    unit: str | None
    value_type: Literal["number", "integer"]
    dataset_title: str
    dataset_description: str
    license: str
    reference_year: int
    pixel_size: float = 10.0
    resampling: Literal["bilinear", "nearest"] = "bilinear"
    nodata: float = -9999.0


AHN_WCS = "https://service.pdok.nl/rws/ahn/wcs/v1_0"
WORLDCOVER_COG = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_N51E003_Map.tif"
)

GOVERNED_RASTER_LAYERS = (
    RasterDefinition(
        dataset_id="ahn",
        layer_id="dtm",
        source="pdok-wcs",
        endpoint=AHN_WCS,
        coverage="dtm_05m",
        band="elevation",
        band_description=(
            "Ground surface height in metres above NAP (Dutch ordnance datum), "
            "from the AHN LiDAR digital terrain model resampled to 10 m; water "
            "and buildings are nodata."
        ),
        unit="m",
        value_type="number",
        dataset_title="Actueel Hoogtebestand Nederland (AHN)",
        dataset_description=(
            "National LiDAR elevation dataset of the Netherlands: digital "
            "terrain and surface models at 0.5 m, published by Rijkswaterstaat "
            "through PDOK."
        ),
        license="CC0 1.0",
        reference_year=2020,
    ),
    RasterDefinition(
        dataset_id="ahn",
        layer_id="dsm",
        source="pdok-wcs",
        endpoint=AHN_WCS,
        coverage="dsm_05m",
        band="elevation",
        band_description=(
            "Surface height in metres above NAP including buildings and "
            "vegetation, from the AHN LiDAR digital surface model resampled "
            "to 10 m; water is nodata."
        ),
        unit="m",
        value_type="number",
        dataset_title="Actueel Hoogtebestand Nederland (AHN)",
        dataset_description=(
            "National LiDAR elevation dataset of the Netherlands: digital "
            "terrain and surface models at 0.5 m, published by Rijkswaterstaat "
            "through PDOK."
        ),
        license="CC0 1.0",
        reference_year=2020,
    ),
    RasterDefinition(
        dataset_id="worldcover",
        layer_id="landcover",
        source="cog",
        endpoint=WORLDCOVER_COG,
        coverage="ESA WorldCover 10 m 2021 v200",
        band="landcover",
        band_description=(
            "Land cover class code: 10 tree cover, 20 shrubland, 30 grassland, "
            "40 cropland, 50 built-up, 60 bare or sparse vegetation, 70 snow "
            "and ice, 80 permanent water bodies, 90 herbaceous wetland, 95 "
            "mangroves, 100 moss and lichen."
        ),
        unit=None,
        value_type="integer",
        dataset_title="ESA WorldCover 2021",
        dataset_description=(
            "Global 10 m land cover map for 2021 from Sentinel-1 and Sentinel-2 "
            "imagery, produced by the ESA WorldCover consortium."
        ),
        license="CC BY 4.0",
        reference_year=2021,
        resampling="nearest",
        nodata=0.0,
    ),
)


@dataclass(frozen=True)
class PreparedRaster:
    layer: CatalogLayer
    geotiff_data: bytes


class RasterIngestion:
    """Acquire governed rasters and prepare their immutable Layers."""

    def __init__(self, storage: ObjectStore, client: httpx.Client) -> None:
        self._storage = storage
        self._client = client

    def prepare(
        self,
        definition: RasterDefinition,
        *,
        extent: Bounds,
        retrieved_at: datetime,
    ) -> PreparedRaster:
        bounds = snapped_bounds(extent, definition.pixel_size)
        if definition.source == "pdok-wcs":
            query = _wcs_query(definition, bounds)
            # WCS takes one subset parameter per axis; the provenance keeps
            # them apart under their own keys.
            params = (
                *((key, value) for key, value in query.items() if not key.startswith("subset_")),
                ("subset", query["subset_x"]),
                ("subset", query["subset_y"]),
            )
            response = self._client.get(definition.endpoint, params=params, timeout=600)
            response.raise_for_status()
            source_bytes = response.content
            original_crs = _crs_of(source_bytes)
        else:
            query = {"url": definition.endpoint, "bounds": ",".join(f"{v:.6f}" for v in bounds)}
            source_bytes, original_crs = _cog_window(definition.endpoint, bounds)
        data = normalise_raster(
            source_bytes,
            bounds=bounds,
            band=definition.band,
            pixel_size=definition.pixel_size,
            nodata=definition.nodata,
            value_type=definition.value_type,
            resampling=definition.resampling,
        )
        band, nodata_pixels = _band_metadata(data, definition)
        content_hash = sha256(data)
        key = f"datasets/{definition.dataset_id}/{definition.layer_id}/{content_hash}.tif"
        self._storage.put_immutable(key, data)
        layer = CatalogLayer(
            dataset_id=definition.dataset_id,
            layer_id=definition.layer_id,
            dataset_version=content_hash,
            content_hash=f"sha256:{content_hash}",
            storage_path=self._storage.uri(key),
            format="GeoTIFF",
            crs=CANONICAL_CRS,
            original_crs=original_crs,
            spatial_extent=bounds,
            temporal_extent=TemporalExtent(
                start=datetime(definition.reference_year, 1, 1, tzinfo=UTC), end=None
            ),
            source_identity_fields=(),
            raw=RawLayerMetadata(
                name=definition.coverage,
                description=definition.band_description,
                schema={
                    "schema": {
                        "properties": {
                            definition.band: {
                                "type": definition.value_type,
                                "description": definition.band_description,
                                **({} if definition.unit is None else {"unit": definition.unit}),
                            }
                        }
                    }
                },
                access=RawAccessMetadata(
                    dataset="openbaar", feature_type="openbaar", reuse_license=definition.license
                ),
                provenance=AcquisitionProvenance(
                    endpoint=definition.endpoint,
                    dataset_version=str(definition.reference_year),
                    wfs_version=None,
                    feature_type=definition.coverage,
                    query=query,
                    retrieved_at=retrieved_at,
                    source_content_hash=f"sha256:{sha256(source_bytes)}",
                    dataset_schema_content_hash=None,
                    feature_schema_content_hash=None,
                    api_key_required=False,
                    page_count=1,
                    protocol="wcs" if definition.source == "pdok-wcs" else "cog",
                ),
                dataset_title=definition.dataset_title,
                dataset_description=definition.dataset_description,
            ),
            enriched=None,
            eligibility=EligibilityDecision(
                dataset_access="openbaar",
                feature_type_access="openbaar",
                policy_basis=CATALOG_ELIGIBILITY_POLICY,
            ),
            vector=None,
            quality=QualityIndicators(
                invalid_geometry_count=0,
                diagnostics=(
                    QualityDiagnostic(category="nodata_pixels", count=nodata_pixels, record_refs=()),
                ),
            ),
            raster=RasterMetadata(
                kind=DataKind.RASTER,
                bands=(band,),
                nodata=definition.nodata,
                pixel_size=definition.pixel_size,
            ),
        )
        return PreparedRaster(layer=layer, geotiff_data=data)


def snapped_bounds(extent: Bounds, pixel_size: float) -> Bounds:
    """The extent widened outward to whole pixels of the common grid."""

    min_x, min_y, max_x, max_y = extent
    return (
        math.floor(min_x / pixel_size) * pixel_size,
        math.floor(min_y / pixel_size) * pixel_size,
        math.ceil(max_x / pixel_size) * pixel_size,
        math.ceil(max_y / pixel_size) * pixel_size,
    )


def normalise_raster(
    source_bytes: bytes,
    *,
    bounds: Bounds,
    band: str,
    pixel_size: float,
    nodata: float,
    value_type: str,
    resampling: str,
) -> bytes:
    """Reproject a source raster onto the common grid as a compressed,
    tiled GeoTIFF with one named band and the given nodata."""

    width = round((bounds[2] - bounds[0]) / pixel_size)
    height = round((bounds[3] - bounds[1]) / pixel_size)
    transform = from_origin(bounds[0], bounds[3], pixel_size, pixel_size)
    dtype = "float32" if value_type == "number" else "uint8"
    destination = np.full((height, width), nodata, dtype=dtype)
    with MemoryFile(source_bytes) as memory, memory.open() as source:
        reproject(
            source=rasterio.band(source, 1),
            destination=destination,
            src_nodata=source.nodata,
            dst_transform=transform,
            dst_crs=CANONICAL_CRS,
            dst_nodata=nodata,
            resampling=Resampling[resampling],
        )
    profile = {
        "driver": "GTiff",
        "dtype": dtype,
        "count": 1,
        "width": width,
        "height": height,
        "crs": CANONICAL_CRS,
        "transform": transform,
        "nodata": nodata,
        "compress": "deflate",
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
    }
    with MemoryFile() as memory:
        with memory.open(**profile) as target:
            target.write(destination, 1)
            target.set_band_description(1, band)
        return memory.read()


def _wcs_query(definition: RasterDefinition, bounds: Bounds) -> dict[str, str]:
    return {
        "service": "WCS",
        "version": "2.0.1",
        "request": "GetCoverage",
        "CoverageId": definition.coverage,
        "subset_x": f"x({bounds[0]:.0f},{bounds[2]:.0f})",
        "subset_y": f"y({bounds[1]:.0f},{bounds[3]:.0f})",
        "format": "image/tiff",
        "SCALEFACTOR": f"{0.5 / definition.pixel_size:g}",
    }


def _cog_window(url: str, bounds: Bounds) -> tuple[bytes, str]:
    """Read the window of a remote COG covering ``bounds`` (given in the
    canonical CRS) and return it as an in-memory GeoTIFF in the source CRS."""

    with rasterio.open(f"/vsicurl/{url}") as source:
        window_bounds = transform_bounds(CANONICAL_CRS, source.crs, *bounds)
        window = source.window(*window_bounds).round_offsets().round_lengths()
        data = source.read(1, window=window)
        profile = source.profile.copy()
        profile.update(
            width=data.shape[1],
            height=data.shape[0],
            transform=source.window_transform(window),
            driver="GTiff",
        )
        with MemoryFile() as memory:
            with memory.open(**profile) as target:
                target.write(data, 1)
            return memory.read(), str(source.crs)


def _crs_of(geotiff: bytes) -> str:
    with MemoryFile(geotiff) as memory, memory.open() as source:
        return str(source.crs)


def _band_metadata(geotiff: bytes, definition: RasterDefinition) -> tuple[AttributeMetadata, int]:
    with MemoryFile(geotiff) as memory, memory.open() as source:
        values = source.read(1)
        dtype = source.dtypes[0]
    valid = values[values != definition.nodata]
    if definition.value_type == "number":
        samples: tuple[object, ...] = (
            round(float(valid.min()), 2),
            round(float(valid.mean()), 2),
            round(float(valid.max()), 2),
        )
    else:
        samples = tuple(int(item) for item in np.unique(valid)[:12])
    band = AttributeMetadata(
        name=definition.band,
        source_type=definition.value_type,
        storage_type=dtype,
        unit=definition.unit,
        sample_values=samples,
    )
    return band, int(values.size - valid.size)
