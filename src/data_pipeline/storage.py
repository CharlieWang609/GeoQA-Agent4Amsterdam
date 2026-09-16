# SPDX-License-Identifier: GPL-3.0-only

"""Persistent object-store seam used by ingestion and the sandbox API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from data_pipeline.errors import ConcurrentPublicationError
from data_pipeline.serialization import sha256


@dataclass(frozen=True)
class StoredObject:
    """Object contents plus the token used for conditional replacement."""

    data: bytes
    etag: str


class ObjectStore(Protocol):
    """Operations implemented by the Azure Blob adapter and the in-memory store."""

    def read(self, key: str) -> StoredObject | None: ...

    def put_immutable(self, key: str, data: bytes) -> None: ...

    def compare_and_swap(
        self,
        key: str,
        data: bytes,
        expected_etag: str | None,
    ) -> None: ...

    def uri(self, key: str) -> str: ...

    def set_expiry(self, key: str, expires_at: datetime) -> None: ...

    def list_keys(self, prefix: str = "") -> tuple[str, ...]: ...

    def delete(self, key: str) -> None: ...


class InMemoryObjectStore:
    """Object-store adapter used by tests and local development."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._expiries: dict[str, datetime] = {}

    def read(self, key: str) -> StoredObject | None:
        data = self._objects.get(key)
        if data is None:
            return None
        return StoredObject(data=data, etag=sha256(data))

    def put_immutable(self, key: str, data: bytes) -> None:
        existing = self._objects.get(key)
        if existing is not None and existing != data:
            raise ValueError(f"Immutable object already exists: {key}")
        self._objects[key] = data

    def compare_and_swap(
        self,
        key: str,
        data: bytes,
        expected_etag: str | None,
    ) -> None:
        existing = self.read(key)
        actual_etag = existing.etag if existing else None
        if actual_etag != expected_etag:
            raise ConcurrentPublicationError(
                "Catalog pointer changed during publication."
            )
        self._objects[key] = data

    def uri(self, key: str) -> str:
        return f"memory://catalog/{key}"

    def set_expiry(self, key: str, expires_at: datetime) -> None:
        if key not in self._objects:
            raise LookupError(key)
        self._expiries[key] = expires_at

    def list_keys(self, prefix: str = "") -> tuple[str, ...]:
        return tuple(
            key for key in sorted(self._objects) if key.startswith(prefix)
        )

    def delete(self, key: str) -> None:
        self._objects.pop(key, None)
        self._expiries.pop(key, None)
