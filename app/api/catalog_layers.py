# SPDX-License-Identifier: GPL-3.0-only

"""Browser-safe read models for the published Catalog."""

from __future__ import annotations

from functools import lru_cache
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import Transformer
from shapely import from_wkb
from shapely.errors import ShapelyError
from shapely.geometry import mapping
from shapely.ops import transform

from data_pipeline.catalog import CATALOG_ELIGIBILITY_POLICY, CatalogReader
from data_pipeline.errors import CatalogNotPublishedError
from data_pipeline.governance import PUBLIC_ACCESS
from data_pipeline.models import (
    AnnotationStatus,
    CatalogLayer,
    SemanticAnnotation,
)
from data_pipeline.serialization import sha256
from data_pipeline.storage import ObjectStore


DISPLAY_CRS = "EPSG:4326"
MAX_PREVIEW_FEATURES = 10_000
# Previews are addressed by the layer content hash (the ETag), so browsers
# and shared caches may retain the anonymous response indefinitely.
PREVIEW_CACHE_CONTROL = "public, max-age=31536000, immutable"


class CatalogLayerNotFoundError(LookupError):
    """No eligible layer matches the requested current Catalog key."""


class CatalogLayerPreviewUnavailableError(ValueError):
    """The immutable snapshot cannot produce a safe layer preview."""


def build_catalog_layer_listing(storage: ObjectStore) -> dict[str, object]:
    """Return the eligible current Catalog without internal storage metadata."""
    try:
        catalog = CatalogReader(storage).current()
    except CatalogNotPublishedError:
        return {"catalog_version": None, "layers": []}

    return {
        "catalog_version": catalog.version,
        "layers": [
            _layer_listing(layer)
            for layer in catalog.layers
            if is_catalog_eligible(layer)
        ],
    }


def is_catalog_eligible(layer: CatalogLayer) -> bool:
    """Check the persisted ADR-0005 eligibility decision fail-closed."""
    eligibility = layer.eligibility
    return (
        eligibility.policy_basis == CATALOG_ELIGIBILITY_POLICY
        and eligibility.dataset_access.casefold() in PUBLIC_ACCESS
        and eligibility.feature_type_access.casefold() in PUBLIC_ACCESS
    )


def find_catalog_layer(
    storage: ObjectStore,
    *,
    dataset: str,
    feature_type: str,
) -> CatalogLayer:
    """Resolve one eligible layer from the current published Catalog."""
    try:
        catalog = CatalogReader(storage).current()
    except CatalogNotPublishedError as error:
        raise CatalogLayerNotFoundError from error
    try:
        return next(
            layer
            for layer in catalog.layers
            if layer.dataset_id == dataset
            and layer.layer_id == feature_type
            and is_catalog_eligible(layer)
        )
    except StopIteration as error:
        raise CatalogLayerNotFoundError from error


def build_catalog_layer_preview(
    storage: ObjectStore,
    layer: CatalogLayer,
) -> dict[str, object]:
    """Read one pinned GeoParquet layer as browser-display GeoJSON."""
    digest = layer.content_hash.removeprefix("sha256:")
    key = f"datasets/{layer.dataset_id}/{layer.layer_id}/{digest}.parquet"
    if layer.storage_path != storage.uri(key):
        raise CatalogLayerPreviewUnavailableError
    stored = storage.read(key)
    if stored is None or f"sha256:{sha256(stored.data)}" != layer.content_hash:
        raise CatalogLayerPreviewUnavailableError

    # Deliberately narrow property allowlist: identity fields plus a display
    # name — never the full attribute table.
    attribute_names = {attribute.name for attribute in layer.vector.attributes}
    display_name_field = next(
        (
            candidate
            for candidate in (
                "naam",
                "naam_aanbieder",
                "naam_sportfaciliteit",
                "name",
            )
            if candidate in attribute_names
            and candidate not in layer.source_identity_fields
        ),
        None,
    )
    property_fields = list(layer.source_identity_fields)
    if display_name_field is not None:
        property_fields.append(display_name_field)

    try:
        table = pq.ParquetFile(pa.BufferReader(stored.data)).read(
            columns=[*property_fields, "geometry"],
            use_threads=False,
        )
        project = _transformer(layer.crs).transform
        features: list[dict[str, object]] = []
        for row in table.to_pylist():
            geometry_data = row["geometry"]
            geometry = (
                None
                if geometry_data is None
                else mapping(
                    transform(project, from_wkb(cast(bytes, geometry_data)))
                )
            )
            features.append(
                {
                    "type": "Feature",
                    "geometry": geometry,
                    "properties": {
                        **{
                            field: row[field]
                            for field in layer.source_identity_fields
                        },
                        **(
                            {}
                            if display_name_field is None
                            else {"name": row[display_name_field]}
                        ),
                    },
                }
            )
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        pa.ArrowException,
        ShapelyError,
    ) as error:
        raise CatalogLayerPreviewUnavailableError from error

    return {"type": "FeatureCollection", "features": features}


def _layer_listing(layer: CatalogLayer) -> dict[str, object]:
    enriched = layer.enriched
    name_en = None if enriched is None else _resolved(enriched.name_en)
    description_en = (
        None if enriched is None else _resolved(enriched.description_en)
    )
    semantic_label = (
        None if enriched is None else _resolved(enriched.semantic_label)
    )
    return {
        "dataset": layer.dataset_id,
        "feature_type": layer.layer_id,
        "name": name_en or layer.raw.name,
        "name_language": "en" if name_en is not None else "nl",
        "description": description_en or layer.raw.description,
        "description_language": (
            "en" if description_en is not None else "nl"
        ),
        "semantic_label": semantic_label,
        "geometry_types": list(layer.vector.geometry_types),
        "feature_count": layer.vector.feature_count,
        "dataset_version": layer.dataset_version,
        "crs": layer.crs,
        "original_crs": layer.original_crs,
        "temporal_extent": {
            "start": layer.temporal_extent.start.isoformat(),
            "end": (
                None
                if layer.temporal_extent.end is None
                else layer.temporal_extent.end.isoformat()
            ),
        },
        "spatial_extent": layer.spatial_extent,
    }


def _resolved(annotation: SemanticAnnotation) -> str | None:
    value = annotation.value
    if (
        annotation.status is not AnnotationStatus.RESOLVED
        or value is None
        or not value.strip()
    ):
        return None
    return value


@lru_cache(maxsize=8)
def _transformer(source_crs: str) -> Transformer:
    return Transformer.from_crs(source_crs, DISPLAY_CRS, always_xy=True)
