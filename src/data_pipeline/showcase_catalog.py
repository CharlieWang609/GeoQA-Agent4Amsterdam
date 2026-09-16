# SPDX-License-Identifier: GPL-3.0-only

"""Showcase Catalog ingestion."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import httpx

from data_pipeline.catalog import CatalogPublisher
from data_pipeline.layers import (
    GOVERNED_OBJECT_LAYERS,
    GOVERNED_SUPPORT_LAYERS,
    NEIGHBORHOOD_LAYER,
    LayerIngestion,
)
from data_pipeline.collections import GOVERNED_COLLECTIONS, prepare_collection
from data_pipeline.rasters import GOVERNED_RASTER_LAYERS, RasterIngestion
from data_pipeline.storage import ObjectStore


class ShowcaseCatalogIngestion:
    """Atomically publish the governed support, object, raster and
    image-collection Layers."""

    def __init__(
        self,
        storage: ObjectStore,
        client: httpx.Client,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._storage = storage
        self._client = client
        self._clock = clock

    def ingest(self) -> str:
        """Prepare every Layer and advance the Catalog pointer once."""
        retrieved_at = self._clock()
        ingestion = LayerIngestion(self._storage, self._client)
        neighborhood = ingestion.prepare_support(
            NEIGHBORHOOD_LAYER, retrieved_at=retrieved_at
        )
        supports = tuple(
            ingestion.prepare_support(definition, retrieved_at=retrieved_at)
            for definition in GOVERNED_SUPPORT_LAYERS
        )
        objects = ingestion.prepare_objects(
            retrieved_at=retrieved_at,
            support_geoparquet=neighborhood.geoparquet_data,
            definitions=GOVERNED_OBJECT_LAYERS,
        )
        extent = neighborhood.layer.spatial_extent
        assert extent is not None
        rasters = tuple(
            RasterIngestion(self._storage, self._client).prepare(
                definition, extent=extent, retrieved_at=retrieved_at
            )
            for definition in GOVERNED_RASTER_LAYERS
        )
        collections = tuple(
            prepare_collection(
                self._storage, definition, extent=extent, retrieved_at=retrieved_at
            )
            for definition in GOVERNED_COLLECTIONS
        )
        return CatalogPublisher(self._storage).publish_snapshot(
            (
                neighborhood.layer,
                *(prepared.layer for prepared in supports),
                *(prepared.layer for prepared in objects),
                *(prepared.layer for prepared in rasters),
                *collections,
            )
        )
