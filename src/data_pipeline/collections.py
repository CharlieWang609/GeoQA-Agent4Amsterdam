# SPDX-License-Identifier: GPL-3.0-only

"""Governed Earth Engine image collections as Catalog Layers.

Nothing is downloaded at ingestion: the Layer's bytes are a small descriptor
naming the asset, its bands and the catalog extent, and every workflow step
over it appends to that descriptor until a step reduces onto vector zones or
exports a GeoTIFF. Credentials are needed only at execution time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from data_pipeline.catalog import CATALOG_ELIGIBILITY_POLICY
from data_pipeline.geoparquet import CANONICAL_CRS
from data_pipeline.models import (
    AcquisitionProvenance,
    AttributeMetadata,
    CatalogLayer,
    DataKind,
    EligibilityDecision,
    QualityIndicators,
    RasterMetadata,
    RawAccessMetadata,
    RawLayerMetadata,
    TemporalExtent,
)
from data_pipeline.serialization import canonical_json, sha256
from data_pipeline.storage import ObjectStore

Bounds = tuple[float, float, float, float]


@dataclass(frozen=True)
class BandDefinition:
    name: str
    description: str
    value_type: str
    unit: str | None
    # Documented value range or class codes, the annotation evidence.
    sample_values: tuple[object, ...]


@dataclass(frozen=True)
class CollectionDefinition:
    dataset_id: str
    layer_id: str
    asset: str
    title: str
    description: str
    license: str
    bands: tuple[BandDefinition, ...]
    pixel_size: float
    start_date: str
    # Metadata property holding the scene cloud percentage, if any.
    cloud_property: str | None
    # Named per-pixel cloud-mask rule the runner knows (None: no mask).
    cloud_mask: str | None


SENTINEL2 = CollectionDefinition(
    dataset_id="gee",
    layer_id="sentinel2",
    asset="COPERNICUS/S2_SR_HARMONIZED",
    title="Sentinel-2 MSI surface reflectance (harmonized)",
    description=(
        "Copernicus Sentinel-2 Level-2A bottom-of-atmosphere reflectance, "
        "10 m, revisit every 5 days, 2017 onwards, served by Google Earth "
        "Engine; the scene classification band flags clouds."
    ),
    license="Copernicus Sentinel data terms (free and open)",
    bands=(
        BandDefinition("B2", "Blue reflectance, scaled by 10000", "integer", None, (0, 10000)),
        BandDefinition("B3", "Green reflectance, scaled by 10000", "integer", None, (0, 10000)),
        BandDefinition("B4", "Red reflectance, scaled by 10000", "integer", None, (0, 10000)),
        BandDefinition("B8", "Near-infrared reflectance, scaled by 10000", "integer", None, (0, 10000)),
        BandDefinition("B11", "Short-wave infrared reflectance (20 m), scaled by 10000", "integer", None, (0, 10000)),
        BandDefinition(
            "SCL",
            "Scene classification code: 1 saturated, 2 dark, 3 cloud shadow, 4 vegetation, "
            "5 bare soil, 6 water, 7 unclassified, 8 cloud medium, 9 cloud high, 10 cirrus, 11 snow",
            "integer", None, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11),
        ),
    ),
    pixel_size=10.0,
    start_date="2017-03-28",
    cloud_property="CLOUDY_PIXEL_PERCENTAGE",
    cloud_mask="s2_scl",
)

DYNAMIC_WORLD = CollectionDefinition(
    dataset_id="gee",
    layer_id="dynamicworld",
    asset="GOOGLE/DYNAMICWORLD/V1",
    title="Dynamic World near-real-time land cover",
    description=(
        "Per-Sentinel-2-scene land cover probabilities and label at 10 m from "
        "Google and the World Resources Institute, 2015 onwards, served by "
        "Google Earth Engine."
    ),
    license="CC BY 4.0",
    bands=(
        BandDefinition("water", "Probability of water", "number", None, (0.0, 1.0)),
        BandDefinition("trees", "Probability of tree cover", "number", None, (0.0, 1.0)),
        BandDefinition("grass", "Probability of grass", "number", None, (0.0, 1.0)),
        BandDefinition("crops", "Probability of crops", "number", None, (0.0, 1.0)),
        BandDefinition("built", "Probability of built-up area", "number", None, (0.0, 1.0)),
        BandDefinition("bare", "Probability of bare ground", "number", None, (0.0, 1.0)),
        BandDefinition(
            "label",
            "Most likely class code: 0 water, 1 trees, 2 grass, 3 flooded vegetation, "
            "4 crops, 5 shrub and scrub, 6 built, 7 bare, 8 snow and ice",
            "integer", None, (0, 1, 2, 3, 4, 5, 6, 7, 8),
        ),
    ),
    pixel_size=10.0,
    start_date="2015-06-27",
    cloud_property=None,
    cloud_mask=None,
)

GOVERNED_COLLECTIONS = (SENTINEL2, DYNAMIC_WORLD)


def descriptor(definition: CollectionDefinition, extent: Bounds) -> dict[str, object]:
    """The remote object a workflow starts from: the asset, its bands, the
    catalog extent in the canonical CRS, and no operations yet."""

    return {
        "kind": DataKind.IMAGE_COLLECTION.value,
        "asset": definition.asset,
        "bands": [band.name for band in definition.bands],
        "extent": list(extent),
        "crs": CANONICAL_CRS,
        "pixel_size": definition.pixel_size,
        "cloud_property": definition.cloud_property,
        "cloud_mask": definition.cloud_mask,
        "operations": [],
    }


def prepare_collection(
    storage: ObjectStore,
    definition: CollectionDefinition,
    *,
    extent: Bounds,
    retrieved_at: datetime,
) -> CatalogLayer:
    """Register one image collection: store its descriptor and describe it."""

    data = canonical_json(descriptor(definition, extent))
    content_hash = sha256(data)
    key = f"datasets/{definition.dataset_id}/{definition.layer_id}/{content_hash}.json"
    storage.put_immutable(key, data)
    return CatalogLayer(
        dataset_id=definition.dataset_id,
        layer_id=definition.layer_id,
        dataset_version=content_hash,
        content_hash=f"sha256:{content_hash}",
        storage_path=storage.uri(key),
        format="gee-image-collection",
        crs=CANONICAL_CRS,
        original_crs="EPSG:4326",
        spatial_extent=extent,
        temporal_extent=TemporalExtent(
            start=datetime.fromisoformat(definition.start_date).replace(tzinfo=UTC), end=None
        ),
        source_identity_fields=(),
        raw=RawLayerMetadata(
            name=definition.asset,
            description=definition.description,
            schema={
                "schema": {
                    "properties": {
                        band.name: {
                            "type": band.value_type,
                            "description": band.description,
                            **({} if band.unit is None else {"unit": band.unit}),
                        }
                        for band in definition.bands
                    }
                }
            },
            access=RawAccessMetadata(
                dataset="openbaar", feature_type="openbaar", reuse_license=definition.license
            ),
            provenance=AcquisitionProvenance(
                endpoint="https://earthengine.googleapis.com",
                dataset_version=definition.asset,
                wfs_version=None,
                feature_type=definition.asset,
                query={"extent": json.dumps(list(extent))},
                retrieved_at=retrieved_at,
                source_content_hash=f"sha256:{content_hash}",
                dataset_schema_content_hash=None,
                feature_schema_content_hash=None,
                api_key_required=True,
                page_count=0,
                protocol="gee",
            ),
            dataset_title=definition.title,
            dataset_description=definition.description,
        ),
        enriched=None,
        eligibility=EligibilityDecision(
            dataset_access="openbaar",
            feature_type_access="openbaar",
            policy_basis=CATALOG_ELIGIBILITY_POLICY,
        ),
        vector=None,
        quality=QualityIndicators(invalid_geometry_count=0, diagnostics=()),
        raster=RasterMetadata(
            kind=DataKind.IMAGE_COLLECTION,
            bands=tuple(
                AttributeMetadata(
                    name=band.name,
                    source_type=band.value_type,
                    storage_type=band.value_type,
                    unit=band.unit,
                    sample_values=band.sample_values,
                )
                for band in definition.bands
            ),
            nodata=None,
            pixel_size=definition.pixel_size,
        ),
    )
