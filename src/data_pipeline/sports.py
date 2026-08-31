# SPDX-License-Identifier: GPL-3.0-only

"""Administrator ingestion for governed Amsterdam sports point Layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

import httpx
from pyproj import CRS

from data_pipeline.catalog import CATALOG_ELIGIBILITY_POLICY
from data_pipeline.errors import UnsupportedSourceError
from data_pipeline.geoparquet import CANONICAL_CRS, build_sports_geoparquet
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

DATASET_SCHEMA_URL = "https://schemas.data.amsterdam.nl/datasets/sport/dataset"
WFS_URL = "https://api.data.amsterdam.nl/v1/wfs/sport/v1"


@dataclass(frozen=True)
class SportsLayerDefinition:
    """Pinned schema and identity contract for one public Sport Feature Type."""

    layer_id: str
    schema_ref: str
    schema_version: str

    @property
    def feature_schema_url(self) -> str:
        return (
            "https://schemas.data.amsterdam.nl/datasets/sport/"
            f"{self.schema_ref}"
        )

    @property
    def wfs_query(self) -> dict[str, str]:
        return {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": f"app:{self.layer_id}",
            "outputFormat": "application/json",
            "count": "10000",
        }


PUBLIC_SPORTS_LOCATION = SportsLayerDefinition(
    layer_id="openbaresportplek",
    schema_ref="openbaresportplek/v2",
    schema_version="2.0.0",
)
SHOWCASE_SPORTS_LAYERS = (
    PUBLIC_SPORTS_LOCATION,
    SportsLayerDefinition(
        layer_id="aanbieder",
        schema_ref="aanbieder/v3",
        schema_version="2.0.0",
    ),
    SportsLayerDefinition(
        layer_id="gymzaal",
        schema_ref="gymzaal/v2",
        schema_version="2.0.0",
    ),
    SportsLayerDefinition(
        layer_id="zwembad",
        schema_ref="zwembad/v2",
        schema_version="2.0.0",
    ),
)

@dataclass(frozen=True)
class PreparedSportsLayer:
    """Catalog contract and bytes prepared before atomic publication."""

    layer: CatalogLayer
    geoparquet_data: bytes


class SportsIngestion:
    """Prepare one or more explicitly governed Sport point Feature Types."""

    def __init__(self, storage: ObjectStore, client: httpx.Client) -> None:
        self._storage = storage
        self._client = client

    def prepare_many(
        self,
        *,
        retrieved_at: datetime,
        support_geoparquet: bytes,
        definitions: tuple[SportsLayerDefinition, ...],
    ) -> tuple[PreparedSportsLayer, ...]:
        """Prepare an explicit Layer set against one pinned Dataset schema."""
        if not definitions:
            raise ValueError("At least one Sport Layer definition is required.")
        if any(
            definition not in SHOWCASE_SPORTS_LAYERS
            for definition in definitions
        ):
            raise ValueError("Only governed Sport Layer definitions are accepted.")
        dataset_response = self._client.get(DATASET_SCHEMA_URL)
        dataset_response.raise_for_status()
        dataset = cast(dict[str, object], dataset_response.json())
        return tuple(
            self._prepare_layer(
                definition=definition,
                dataset=dataset,
                dataset_schema_content=dataset_response.content,
                retrieved_at=retrieved_at,
                support_geoparquet=support_geoparquet,
            )
            for definition in definitions
        )

    def _prepare_layer(
        self,
        *,
        definition: SportsLayerDefinition,
        dataset: dict[str, object],
        dataset_schema_content: bytes,
        retrieved_at: datetime,
        support_geoparquet: bytes,
    ) -> PreparedSportsLayer:
        feature_response = self._client.get(definition.feature_schema_url)
        feature_response.raise_for_status()
        feature_type = cast(dict[str, object], feature_response.json())
        (
            raw_dataset_access,
            raw_feature_access,
            effective_feature_access,
        ) = self._validate_metadata(
            dataset,
            feature_type,
            definition,
        )

        query = definition.wfs_query
        acquired = fetch_all_features(self._client, WFS_URL, query)
        dataset_crs = str(dataset["crs"])
        if CRS.from_user_input(dataset_crs) != CRS.from_user_input(
            acquired.original_crs
        ):
            raise UnsupportedSourceError(
                "WFS response CRS conflicts with official Dataset metadata."
            )
        prepared = build_sports_geoparquet(
            acquired.payload,
            original_crs=acquired.original_crs,
            source_schema=feature_type,
            retrieved_at=retrieved_at,
            support_geoparquet=support_geoparquet,
            record_ref_prefix=definition.layer_id,
        )
        content_hash = sha256(prepared.data)
        dataset_key = f"datasets/sport/{definition.layer_id}/{content_hash}.parquet"
        self._storage.put_immutable(dataset_key, prepared.data)
        layer = CatalogLayer(
            dataset_id="sport",
            layer_id=definition.layer_id,
            dataset_version=content_hash,
            content_hash=f"sha256:{content_hash}",
            storage_path=self._storage.uri(dataset_key),
            format="GeoParquet",
            crs=CANONICAL_CRS,
            original_crs=acquired.original_crs,
            spatial_extent=prepared.spatial_extent,
            temporal_extent=prepared.temporal_extent,
            source_identity_fields=("id",),
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
                    feature_type=f"app:{definition.layer_id}",
                    query=query,
                    retrieved_at=retrieved_at,
                    source_content_hash=f"sha256:{acquired.content_hash}",
                    dataset_schema_content_hash=(
                        f"sha256:{sha256(dataset_schema_content)}"
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
        return PreparedSportsLayer(
            layer=layer,
            geoparquet_data=prepared.data,
        )

    @staticmethod
    def _validate_metadata(
        dataset: dict[str, object],
        feature_type: dict[str, object],
        definition: SportsLayerDefinition,
    ) -> tuple[str, str | None, str]:
        """Pin exact stable Sport Layer metadata and derive
        access levels per the two-level public-access policy."""

        try:
            versions = cast(dict[str, object], dataset["versions"])
            version = cast(dict[str, object], versions["v1"])
            tables = cast(list[dict[str, object]], version["tables"])
            schema = cast(dict[str, object], feature_type["schema"])
            properties = cast(dict[str, object], schema["properties"])
            geometry = cast(dict[str, object], properties["geometry"])
            has_layer = any(
                table.get("id") == definition.layer_id
                and table.get("$ref") == definition.schema_ref
                for table in tables
            )
        except (KeyError, TypeError) as error:
            raise UnsupportedSourceError(
                "Official sport Dataset metadata is incomplete."
            ) from error
        observed_schema_version = feature_type.get("version")
        if observed_schema_version != definition.schema_version:
            raise UnsupportedSourceError(
                "Ingestion requires stable sport:v1/app:"
                f"{definition.layer_id} metadata with schema version "
                f"{definition.schema_version!r}; observed "
                f"{observed_schema_version!r}."
            )
        if (
            dataset.get("id") != "sport"
            or version.get("status") != "stable"
            or not has_layer
            or feature_type.get("id") != definition.layer_id
            or feature_type.get("status") != "stable"
            or geometry.get("$ref")
            != "https://geojson.org/schema/Point.json"
        ):
            raise UnsupportedSourceError(
                "Ingestion requires stable sport:v1/app:"
                f"{definition.layer_id} metadata with Point geometry."
            )

        return require_public_access(
            dataset.get("auth"),
            feature_type.get("auth"),
        )
