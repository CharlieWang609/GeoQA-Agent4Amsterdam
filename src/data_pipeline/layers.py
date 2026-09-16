# SPDX-License-Identifier: GPL-3.0-only

"""Administrator ingestion for the governed Amsterdam WFS Layers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import cast

import httpx
from pyproj import CRS

from data_pipeline.catalog import CATALOG_ELIGIBILITY_POLICY
from data_pipeline.errors import UnsupportedSourceError
from data_pipeline.geoparquet import (
    CANONICAL_CRS,
    PreparedGeoParquet,
    build_object_geoparquet,
    build_support_geoparquet,
)
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

SCHEMA_BASE_URL = "https://schemas.data.amsterdam.nl/datasets"
WFS_BASE_URL = "https://api.data.amsterdam.nl/v1/wfs"
# (payload, original CRS, feature-type schema) -> normalized GeoParquet
_Builder = Callable[[bytes, str, dict[str, object]], PreparedGeoParquet]


@dataclass(frozen=True)
class LayerDefinition:
    """Pinned schema, geometry and identity contract for one Feature Type.

    The Amsterdam Schema geometry property is named per dataset convention
    (``geometry`` or ``geometrie``); ``geometry_type`` is the GeoJSON shape
    the official schema declares for it.
    """

    dataset_id: str
    layer_id: str
    schema_ref: str
    schema_version: str
    geometry_type: str = "Point"
    geometry_property: str = "geometry"
    identity_fields: tuple[str, ...] = ("id",)

    @property
    def stored_geometry_types(self) -> frozenset[str]:
        """The GeoJSON types accepted for storage: singular and multi forms."""

        base = self.geometry_type.removeprefix("Multi")
        return frozenset({base, f"Multi{base}"})

    @property
    def dataset_schema_url(self) -> str:
        return f"{SCHEMA_BASE_URL}/{self.dataset_id}/dataset"

    @property
    def feature_schema_url(self) -> str:
        return f"{SCHEMA_BASE_URL}/{self.dataset_id}/{self.schema_ref}"

    @property
    def wfs_url(self) -> str:
        return f"{WFS_BASE_URL}/{self.dataset_id}/v1"

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


# The neighborhood polygons every object Layer is gated against.
NEIGHBORHOOD_LAYER = LayerDefinition(
    dataset_id="gebieden",
    layer_id="buurten",
    schema_ref="buurten/v1",
    schema_version="1.1.5",
    geometry_type="Polygon",
    geometry_property="geometrie",
    identity_fields=("identificatie", "volgnummer"),
)
# Coarser gebieden tessellations offered as alternative count supports.
GOVERNED_SUPPORT_LAYERS = (
    LayerDefinition(
        dataset_id="gebieden",
        layer_id="wijken",
        schema_ref="wijken/v1",
        schema_version="1.1.5",
        geometry_type="Polygon",
        geometry_property="geometrie",
        identity_fields=("identificatie", "volgnummer"),
    ),
    LayerDefinition(
        dataset_id="gebieden",
        layer_id="stadsdelen",
        schema_ref="stadsdelen/v1",
        schema_version="1.1.3",
        geometry_type="Polygon",
        geometry_property="geometrie",
        identity_fields=("identificatie", "volgnummer"),
    ),
)
GOVERNED_OBJECT_LAYERS = (
    LayerDefinition(
        dataset_id="sport",
        layer_id="openbaresportplek",
        schema_ref="openbaresportplek/v2",
        schema_version="2.0.0",
    ),
    LayerDefinition(
        dataset_id="sport",
        layer_id="aanbieder",
        schema_ref="aanbieder/v3",
        schema_version="2.0.0",
    ),
    LayerDefinition(
        dataset_id="sport",
        layer_id="gymzaal",
        schema_ref="gymzaal/v2",
        schema_version="2.0.0",
    ),
    LayerDefinition(
        dataset_id="sport",
        layer_id="zwembad",
        schema_ref="zwembad/v2",
        schema_version="2.0.0",
    ),
    LayerDefinition(
        dataset_id="sport",
        layer_id="hal",
        schema_ref="hal/v2",
        schema_version="2.0.0",
    ),
    LayerDefinition(
        dataset_id="sport",
        layer_id="park",
        schema_ref="park/v2",
        schema_version="2.0.0",
        geometry_type="MultiPolygon",
    ),
    LayerDefinition(
        dataset_id="sport",
        layer_id="veld",
        schema_ref="veld/v2",
        schema_version="2.0.0",
        geometry_type="MultiPolygon",
    ),
    LayerDefinition(
        dataset_id="sport",
        layer_id="hardlooproute",
        schema_ref="hardlooproute/v2",
        schema_version="2.0.0",
        geometry_type="MultiLineString",
    ),
    LayerDefinition(
        dataset_id="huishoudelijkafval",
        layer_id="container",
        schema_ref="container/v2",
        schema_version="2.2.0",
        geometry_property="geometrie",
    ),
    LayerDefinition(
        dataset_id="huishoudelijkafval",
        layer_id="cluster",
        schema_ref="cluster/v2",
        schema_version="2.2.0",
        geometry_property="geometrie",
    ),
    LayerDefinition(
        dataset_id="bouwstroompunten",
        layer_id="bouwstroompunten",
        schema_ref="bouwstroompunten/v1",
        schema_version="1.1.1",
    ),
    LayerDefinition(
        dataset_id="touringcars",
        layer_id="haltes",
        schema_ref="haltes/v1",
        schema_version="1.0.0",
    ),
    LayerDefinition(
        dataset_id="varen",
        layer_id="opafstapplaats",
        schema_ref="opafstapplaats/v1",
        schema_version="1.0.0",
        geometry_property="geometrie",
    ),
    LayerDefinition(
        dataset_id="varen",
        layer_id="ligplaats",
        schema_ref="ligplaats/v1",
        schema_version="1.0.0",
        geometry_property="geometrie",
    ),
    LayerDefinition(
        dataset_id="ecologie",
        layer_id="faunavoorzieningen",
        schema_ref="faunavoorzieningen/v1",
        schema_version="1.1.1",
        geometry_property="geometrie",
    ),
    LayerDefinition(
        dataset_id="fietspaaltjes",
        layer_id="fietspaaltjes",
        schema_ref="fietspaaltjes/v1",
        schema_version="1.0.0",
    ),
    LayerDefinition(
        dataset_id="verkeersinformatiesystemen",
        layer_id="verkeersinformatiesystemen",
        schema_ref="verkeersinformatiesystemen/v1",
        schema_version="1.1.1",
        geometry_property="geometrie",
    ),
    LayerDefinition(
        dataset_id="winkelgebieden",
        layer_id="winkelgebieden",
        schema_ref="winkelgebieden/v1",
        schema_version="1.0.0",
        geometry_type="MultiPolygon",
    ),
)


@dataclass(frozen=True)
class PreparedLayer:
    """Catalog contract and bytes prepared before atomic publication."""

    layer: CatalogLayer
    geoparquet_data: bytes


class LayerIngestion:
    """Acquire governed Feature Types and prepare their immutable Layers."""

    def __init__(self, storage: ObjectStore, client: httpx.Client) -> None:
        self._storage = storage
        self._client = client
        self._dataset_schemas: dict[str, tuple[dict[str, object], bytes]] = {}

    def prepare_support(
        self,
        definition: LayerDefinition,
        *,
        retrieved_at: datetime,
    ) -> PreparedLayer:
        """Prepare one active gebieden tessellation (neighborhoods gate objects)."""

        return self._prepare(
            definition,
            retrieved_at=retrieved_at,
            build=lambda payload, crs, schema: build_support_geoparquet(
                payload,
                original_crs=crs,
                source_schema=schema,
                retrieved_at=retrieved_at,
            ),
        )

    def prepare_objects(
        self,
        *,
        retrieved_at: datetime,
        support_geoparquet: bytes,
        definitions: tuple[LayerDefinition, ...],
    ) -> tuple[PreparedLayer, ...]:
        """Prepare object Layers gated by the prepared neighborhood support."""

        def build(definition: LayerDefinition) -> _Builder:
            return lambda payload, crs, schema: build_object_geoparquet(
                payload,
                original_crs=crs,
                source_schema=schema,
                retrieved_at=retrieved_at,
                support_geoparquet=support_geoparquet,
                record_ref_prefix=definition.layer_id,
                accepted_geometry_types=definition.stored_geometry_types,
            )

        return tuple(
            self._prepare(
                definition, retrieved_at=retrieved_at, build=build(definition)
            )
            for definition in definitions
        )

    def _prepare(
        self,
        definition: LayerDefinition,
        *,
        retrieved_at: datetime,
        build: _Builder,
    ) -> PreparedLayer:
        dataset, dataset_schema_content = self._dataset_schema(definition)
        feature_response = self._client.get(definition.feature_schema_url)
        feature_response.raise_for_status()
        feature_type = cast(dict[str, object], feature_response.json())
        (
            raw_dataset_access,
            raw_feature_access,
            effective_feature_access,
        ) = _validate_metadata(dataset, feature_type, definition)

        query = definition.wfs_query
        acquired = fetch_all_features(self._client, definition.wfs_url, query)
        if CRS.from_user_input(str(dataset["crs"])) != CRS.from_user_input(
            acquired.original_crs
        ):
            raise UnsupportedSourceError(
                "WFS response CRS conflicts with official Dataset metadata."
            )
        prepared = build(acquired.payload, acquired.original_crs, feature_type)
        content_hash = sha256(prepared.data)
        dataset_key = (
            f"datasets/{definition.dataset_id}/{definition.layer_id}/"
            f"{content_hash}.parquet"
        )
        self._storage.put_immutable(dataset_key, prepared.data)
        layer = CatalogLayer(
            dataset_id=definition.dataset_id,
            layer_id=definition.layer_id,
            dataset_version=content_hash,
            content_hash=f"sha256:{content_hash}",
            storage_path=self._storage.uri(dataset_key),
            format="GeoParquet",
            crs=CANONICAL_CRS,
            original_crs=acquired.original_crs,
            spatial_extent=prepared.spatial_extent,
            temporal_extent=prepared.temporal_extent,
            source_identity_fields=definition.identity_fields,
            raw=RawLayerMetadata(
                name=str(feature_type["id"]),
                description=_optional_text(feature_type.get("description")),
                schema=feature_type,
                access=RawAccessMetadata(
                    dataset=raw_dataset_access,
                    feature_type=raw_feature_access,
                    reuse_license=_optional_text(dataset.get("license")),
                ),
                dataset_title=_optional_text(dataset.get("title")),
                dataset_description=_optional_text(dataset.get("description")),
                provenance=AcquisitionProvenance(
                    endpoint=definition.wfs_url,
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
        return PreparedLayer(layer=layer, geoparquet_data=prepared.data)

    def _dataset_schema(
        self,
        definition: LayerDefinition,
    ) -> tuple[dict[str, object], bytes]:
        if definition.dataset_id not in self._dataset_schemas:
            response = self._client.get(definition.dataset_schema_url)
            response.raise_for_status()
            self._dataset_schemas[definition.dataset_id] = (
                cast(dict[str, object], response.json()),
                response.content,
            )
        return self._dataset_schemas[definition.dataset_id]


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _validate_metadata(
    dataset: dict[str, object],
    feature_type: dict[str, object],
    definition: LayerDefinition,
) -> tuple[str, str | None, str]:
    """Pin the exact stable Feature Type metadata and derive access levels.

    Returns (dataset access, raw feature-type access, effective feature-type
    access) per the two-level public-access policy.
    """

    try:
        versions = cast(dict[str, object], dataset["versions"])
        version = cast(dict[str, object], versions["v1"])
        tables = cast(list[dict[str, object]], version["tables"])
        schema = cast(dict[str, object], feature_type["schema"])
        properties = cast(dict[str, object], schema["properties"])
        geometry = cast(
            dict[str, object], properties[definition.geometry_property]
        )
        has_layer = any(
            table.get("id") == definition.layer_id
            and table.get("$ref") == definition.schema_ref
            for table in tables
        )
    except (KeyError, TypeError) as error:
        raise UnsupportedSourceError(
            f"Official {definition.dataset_id} Dataset metadata is incomplete."
        ) from error
    observed_schema_version = feature_type.get("version")
    if observed_schema_version != definition.schema_version:
        raise UnsupportedSourceError(
            f"Ingestion requires stable {definition.dataset_id}:v1/app:"
            f"{definition.layer_id} metadata with schema version "
            f"{definition.schema_version!r}; observed "
            f"{observed_schema_version!r}."
        )
    if (
        dataset.get("id") != definition.dataset_id
        or version.get("status") != "stable"
        or not has_layer
        or feature_type.get("id") != definition.layer_id
        or feature_type.get("status") != "stable"
        or geometry.get("$ref")
        not in (
            f"https://geojson.org/schema/{definition.geometry_type}.json",
            # Several datasets declare the generic schema although every
            # feature is a Point; the builder excludes other shapes per record.
            "https://geojson.org/schema/Geometry.json",
        )
    ):
        raise UnsupportedSourceError(
            f"Ingestion requires stable {definition.dataset_id}:v1/app:"
            f"{definition.layer_id} metadata with {definition.geometry_type} "
            "geometry."
        )

    return require_public_access(dataset.get("auth"), feature_type.get("auth"))
