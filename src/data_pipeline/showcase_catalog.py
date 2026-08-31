# SPDX-License-Identifier: GPL-3.0-only

"""Five-Layer Showcase Catalog ingestion built on the certified MVP seam."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import httpx

from data_pipeline.catalog import CatalogPublisher
from data_pipeline.errors import UnsupportedSourceError
from data_pipeline.neighborhoods import NeighborhoodIngestion
from data_pipeline.sports import SHOWCASE_SPORTS_LAYERS, SportsIngestion
from data_pipeline.storage import ObjectStore


class ShowcaseCatalogIngestion:
    """Atomically publish neighborhoods plus four governed Sport point Layers."""

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
        """Prepare all five Layers and advance the Catalog pointer once."""
        retrieved_at = self._clock()
        neighborhood = NeighborhoodIngestion(
            self._storage,
            self._client,
        ).prepare(retrieved_at=retrieved_at)
        if neighborhood.layer.vector.geometry_types != ("Polygon",):
            raise UnsupportedSourceError(
                "The Showcase neighborhood Layer needs Polygon geometry."
            )
        sports = SportsIngestion(self._storage, self._client).prepare_many(
            retrieved_at=retrieved_at,
            support_geoparquet=neighborhood.geoparquet_data,
            definitions=SHOWCASE_SPORTS_LAYERS,
        )
        if any(
            prepared.layer.vector.geometry_types != ("Point",)
            for prepared in sports
        ):
            raise UnsupportedSourceError(
                "Every Showcase Sport Layer needs usable Point geometry."
            )
        return CatalogPublisher(self._storage).publish_snapshot(
            (neighborhood.layer, *(prepared.layer for prepared in sports))
        )
