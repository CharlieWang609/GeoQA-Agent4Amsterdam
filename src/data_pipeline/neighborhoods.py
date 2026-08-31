# SPDX-License-Identifier: GPL-3.0-only

"""Administrator ingestion for the governed Amsterdam neighborhood source."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

import httpx
from pyproj import CRS

from data_pipeline.catalog import CATALOG_ELIGIBILITY_POLICY
from data_pipeline.errors import UnsupportedSourceError
from data_pipeline.geoparquet import CANONICAL_CRS, build_neighborhood_geoparquet
from data_pipeline.governance import require_public_access
from data_pipeline.models import (
    AcquisitionProvenance,
    CatalogLayer,
    EligibilityDecision,
    RawAccessMetadata,
    RawLayerMetadata,
)
from data_pipeline.serialization import sha256
from data_pipeline.storage import ObjectStore
from data_pipeline.wfs import fetch_all_features

DATASET_SCHEMA_URL = "https://schemas.data.amsterdam.nl/datasets/gebieden/dataset"
FEATURE_SCHEMA_URL = (
    "https://schemas.data.amsterdam.nl/datasets/gebieden/buurten/v1"
)
FEATURE_SCHEMA_VERSION = "1.1.5"
WFS_URL = "https://api.data.amsterdam.nl/v1/wfs/gebieden/v1"
WFS_QUERY = {
    "service": "WFS",
    "version": "2.0.0",
    "request": "GetFeature",
    "typeNames": "app:buurten",
    "outputFormat": "application/json",
    "count": "10000",
}
@dataclass(frozen=True)
class PreparedNeighborhoodLayer:
    """Catalog contract and bytes prepared before atomic publication."""

    layer: CatalogLayer
    geoparquet_data: bytes


class NeighborhoodIngestion:
    """Acquire and prepare the governed neighborhood Feature Type."""

    def __init__(
        self,
        storage: ObjectStore,
        client: httpx.Client,
    ) -> None:
        self._storage = storage
        self._client = client

    def prepare(self, *, retrieved_at: datetime) -> PreparedNeighborhoodLayer:
        """Prepare the immutable Layer without advancing the Catalog pointer."""
        dataset_response = self._client.get(DATASET_SCHEMA_URL)
        dataset_response.raise_for_status()
        feature_response = self._client.get(FEATURE_SCHEMA_URL)
        feature_response.raise_for_status()
        dataset = cast(dict[str, object], dataset_response.json())
        feature_type = cast(dict[str, object], feature_response.json())
        (
            raw_dataset_access,
            raw_feature_access,
            effective_feature_access,
        ) = self._validate_metadata(
            dataset,
            feature_type,
        )

        acquired = fetch_all_features(self._client, WFS_URL, WFS_QUERY)
        dataset_crs = str(dataset["crs"])
        if CRS.from_user_input(dataset_crs) != CRS.from_user_input(
            acquired.original_crs
        ):
            raise UnsupportedSourceError(
                "WFS response CRS conflicts with official Dataset metadata."
            )
        prepared = build_neighborhood_geoparquet(
            acquired.payload,
            original_crs=acquired.original_crs,
            source_schema=feature_type,
            retrieved_at=retrieved_at,
        )
        content_hash = sha256(prepared.data)
        dataset_key = f"datasets/gebieden/buurten/{content_hash}.parquet"
        self._storage.put_immutable(dataset_key, prepared.data)

        layer = CatalogLayer(
            dataset_id="gebieden",
            layer_id="buurten",
            dataset_version=content_hash,
            content_hash=f"sha256:{content_hash}",
            storage_path=self._storage.uri(dataset_key),
            format="GeoParquet",
            crs=CANONICAL_CRS,
            original_crs=acquired.original_crs,
            spatial_extent=prepared.spatial_extent,
            temporal_extent=prepared.temporal_extent,
            source_identity_fields=("identificatie", "volgnummer"),
            raw=RawLayerMetadata(
                name=str(feature_type["id"]),
                description=(
                    None
                    if feature_type.get("description") is None
                    else str(feature_type["description"])
                ),
                schema=feature_type,
                access=RawAccessMetadata(
                    dataset=raw_dataset_access,
                    feature_type=raw_feature_access,
                    reuse_license=(
                        None
                        if dataset.get("license") is None
                        else str(dataset["license"])
                    ),
                ),
                provenance=AcquisitionProvenance(
                    endpoint=WFS_URL,
                    dataset_version="v1",
                    wfs_version="2.0.0",
                    feature_type="app:buurten",
                    query=WFS_QUERY,
                    retrieved_at=retrieved_at,
                    source_content_hash=f"sha256:{acquired.content_hash}",
                    dataset_schema_content_hash=(
                        f"sha256:{sha256(dataset_response.content)}"
                    ),
                    feature_schema_content_hash=(
                        f"sha256:{sha256(feature_response.content)}"
                    ),
                    api_key_required=False,
                    page_count=acquired.page_count,
                ),
            ),
            enriched=None,
            eligibility=EligibilityDecision(
                dataset_access=raw_dataset_access,
                feature_type_access=effective_feature_access,
                policy_basis=CATALOG_ELIGIBILITY_POLICY,
            ),
            vector=prepared.vector,
            quality=prepared.quality,
        )
        return PreparedNeighborhoodLayer(
            layer=layer,
            geoparquet_data=prepared.data,
        )

    @staticmethod
    def _validate_metadata(
        dataset: dict[str, object],
        feature_type: dict[str, object],
    ) -> tuple[str, str | None, str]:
        """Pin the exact stable gebieden/buurten metadata and derive access.

        Returns (dataset access, raw feature-type access, effective
        feature-type access) per the two-level public-access policy.
        """

        try:
            versions = cast(dict[str, object], dataset["versions"])
            version = cast(dict[str, object], versions["v1"])
            tables = cast(list[dict[str, object]], version["tables"])
            schema = cast(dict[str, object], feature_type["schema"])
            properties = cast(dict[str, object], schema["properties"])
            geometry = cast(dict[str, object], properties["geometrie"])
            has_neighborhoods = any(
                table.get("id") == "buurten"
                and table.get("$ref") == "buurten/v1"
                for table in tables
            )
        except (KeyError, TypeError) as error:
            raise UnsupportedSourceError(
                "Official Dataset metadata is incomplete."
            ) from error
        observed_schema_version = feature_type.get("version")
        if observed_schema_version != FEATURE_SCHEMA_VERSION:
            raise UnsupportedSourceError(
                "Ingestion requires stable gebieden:v1/app:buurten metadata "
                "with schema version "
                f"{FEATURE_SCHEMA_VERSION!r}; observed "
                f"{observed_schema_version!r}."
            )
        if (
            dataset.get("id") != "gebieden"
            or version.get("status") != "stable"
            or not has_neighborhoods
            or feature_type.get("id") != "buurten"
            or feature_type.get("status") != "stable"
            or geometry.get("$ref")
            != "https://geojson.org/schema/Polygon.json"
        ):
            raise UnsupportedSourceError(
                "Ingestion requires stable gebieden:v1/app:buurten metadata."
            )

        return require_public_access(
            dataset.get("auth"),
            feature_type.get("auth"),
        )
