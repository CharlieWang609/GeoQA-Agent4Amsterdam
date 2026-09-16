# SPDX-License-Identifier: GPL-3.0-only

"""Azure Blob Storage adapter for immutable Catalog publication."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.storage.blob import ContainerClient
from azure.storage.filedatalake import DataLakeFileClient

from data_pipeline.errors import ConcurrentPublicationError
from data_pipeline.storage import StoredObject


class AzureBlobObjectStore:
    """Persist Catalog artifacts with Azure ETag compare-and-swap semantics."""

    def __init__(
        self,
        container: ContainerClient,
        *,
        expiry_client_factory: Callable[[str], DataLakeFileClient] | None = None,
    ) -> None:
        self._container = container
        self._expiry_client_factory = expiry_client_factory or (
            lambda key: DataLakeFileClient(
                account_url=(
                    f"https://{container.account_name}.dfs.core.windows.net"
                ),
                file_system_name=container.container_name,
                file_path=key,
                credential=container.credential,
            )
        )

    def read(self, key: str) -> StoredObject | None:
        blob = self._container.get_blob_client(key)
        try:
            download = blob.download_blob()
        except ResourceNotFoundError:
            return None
        return StoredObject(
            data=download.readall(),
            etag=str(download.properties.etag),
        )

    def put_immutable(self, key: str, data: bytes) -> None:
        blob = self._container.get_blob_client(key)
        try:
            blob.upload_blob(data, overwrite=False)
        except ResourceExistsError:
            existing = self.read(key)
            if existing is None or existing.data != data:
                raise ValueError(f"Immutable object already exists: {key}")

    def compare_and_swap(
        self,
        key: str,
        data: bytes,
        expected_etag: str | None,
    ) -> None:
        blob = self._container.get_blob_client(key)
        try:
            # None means "create only if absent"; an etag means "replace only
            # if unmodified" — both map onto Azure conditional uploads.
            if expected_etag is None:
                blob.upload_blob(data, overwrite=False)
            else:
                blob.upload_blob(
                    data,
                    overwrite=True,
                    etag=expected_etag,
                    match_condition=MatchConditions.IfNotModified,
                )
        except (ResourceExistsError, ResourceModifiedError) as error:
            raise ConcurrentPublicationError(
                "Catalog pointer changed during publication."
            ) from error

    def uri(self, key: str) -> str:
        return (
            f"az://{self._container.account_name}.blob.core.windows.net/"
            f"{self._container.container_name}/{key}"
        )

    def set_expiry(self, key: str, expires_at: datetime) -> None:
        # Blob expiry is only available through the Data Lake (dfs) endpoint
        # on HNS-enabled accounts, hence the separate client factory.
        self._expiry_client_factory(key).set_file_expiry(
            "Absolute",
            expires_on=expires_at,
        )

    def list_keys(self, prefix: str = "") -> tuple[str, ...]:
        # HNS-enabled accounts surface directories as zero-byte pseudo-blobs
        # carrying hdi_isfolder metadata; an object store lists only objects.
        return tuple(
            blob.name
            for blob in self._container.list_blobs(
                name_starts_with=prefix,
                include=["metadata"],
            )
            if not (blob.metadata or {}).get("hdi_isfolder")
        )

    def delete(self, key: str) -> None:
        try:
            self._container.delete_blob(key)
        except ResourceNotFoundError:
            return
