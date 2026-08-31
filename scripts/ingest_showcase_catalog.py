# SPDX-License-Identifier: GPL-3.0-only

"""Publish the governed five-Layer Showcase Catalog snapshot."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient

from data_pipeline.azure_storage import AzureBlobObjectStore
from data_pipeline.showcase_catalog import ShowcaseCatalogIngestion


def main() -> None:
    """Run Showcase ingestion without changing the frozen MVP command."""
    container = ContainerClient(
        account_url=os.environ["AZURE_STORAGE_ACCOUNT_URL"],
        container_name=os.environ["AZURE_STORAGE_CATALOG_CONTAINER"],
        credential=DefaultAzureCredential(),
    )
    with httpx.Client(timeout=120) as client:
        version = ShowcaseCatalogIngestion(
            AzureBlobObjectStore(container),
            client,
            clock=lambda: datetime.now(UTC),
        ).ingest()
    print(version)


if __name__ == "__main__":
    main()
