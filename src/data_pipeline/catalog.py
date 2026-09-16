# SPDX-License-Identifier: GPL-3.0-only

"""Immutable JSON Catalog persistence and atomic version publication."""

from __future__ import annotations

import json

from pydantic import TypeAdapter

from data_pipeline.errors import (
    CatalogNotPublishedError,
    ConcurrentPublicationError,
    InvalidSnapshotError,
)
from data_pipeline.models import CatalogLayer, CatalogVersion
from data_pipeline.serialization import canonical_json, sha256
from data_pipeline.storage import ObjectStore, StoredObject

CATALOG_ELIGIBILITY_POLICY = "ADR-0005:mvp-two-level-public-access"
CURRENT_CATALOG_POINTER = "catalogs/current.json"

_LAYERS: TypeAdapter[tuple[CatalogLayer, ...]] = TypeAdapter(
    tuple[CatalogLayer, ...]
)


class CatalogPublisher:
    """Persist complete immutable Catalog versions behind one pointer."""

    def __init__(self, storage: ObjectStore) -> None:
        self._storage = storage

    def publish_snapshot(
        self,
        layers: tuple[CatalogLayer, ...],
        *,
        base_version: str | None = None,
    ) -> str:
        """Publish one complete replacement snapshot with a single pointer CAS.

        ``base_version`` lets a long enrichment chain assert the catalog it
        read is still current at publish time (optimistic concurrency).
        """

        if not layers:
            raise InvalidSnapshotError("A Catalog snapshot cannot be empty.")
        keys = [(layer.dataset_id, layer.layer_id) for layer in layers]
        if len(keys) != len(set(keys)):
            raise InvalidSnapshotError("Catalog snapshot Layer ids must be unique.")
        pointer = self._storage.read(CURRENT_CATALOG_POINTER)
        if base_version is not None and _pointer_version(pointer) != base_version:
            raise ConcurrentPublicationError(
                "Catalog pointer changed before enrichment publication."
            )
        ordered = tuple(layer for _, layer in sorted(zip(keys, layers)))
        catalog_data = _LAYERS.dump_json(ordered)
        version = sha256(catalog_data)
        self._storage.put_immutable(_version_key(version), catalog_data)
        self._storage.compare_and_swap(
            CURRENT_CATALOG_POINTER,
            canonical_json({"version": version}),
            pointer.etag if pointer else None,
        )
        return version


class CatalogReader:
    """Read only complete current or explicitly pinned Catalog versions."""

    def __init__(self, storage: ObjectStore) -> None:
        self._storage = storage

    def current(self) -> CatalogVersion:
        pointer = self._storage.read(CURRENT_CATALOG_POINTER)
        if pointer is None:
            raise CatalogNotPublishedError("No Catalog version is published.")
        version = _pointer_version(pointer)
        assert version is not None
        return self.get(version)

    def get(self, version: str) -> CatalogVersion:
        """Open an immutable version without consulting the current pointer."""

        stored = self._storage.read(_version_key(version))
        if stored is None:
            raise CatalogNotPublishedError(
                f"Catalog version is missing: {version}"
            )
        return CatalogVersion(
            version=version,
            layers=_LAYERS.validate_json(stored.data),
        )


def _version_key(version: str) -> str:
    return f"catalogs/versions/{version}.json"


def _pointer_version(pointer: StoredObject | None) -> str | None:
    if pointer is None:
        return None
    try:
        return str(json.loads(pointer.data)["version"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise CatalogNotPublishedError(
            "Current Catalog pointer is invalid."
        ) from error
