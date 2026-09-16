# SPDX-License-Identifier: GPL-3.0-only

"""Catalog stores and environment access shared by the operator scripts."""

from __future__ import annotations

import os
import pickle
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient

from data_pipeline.azure_storage import AzureBlobObjectStore
from data_pipeline.showcase_catalog import ShowcaseCatalogIngestion
from data_pipeline.storage import InMemoryObjectStore
from geoqa_agent.model_clients import annotation_client
from metadata_annotation import MetadataAnnotationJob


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise SystemExit(f"{name} is required.")
    return value.strip()


def azure_store() -> AzureBlobObjectStore:
    """The deployed catalog container, addressed the way Terraform exports it."""

    account = required_environment("AZURE_STORAGE_ACCOUNT_NAME")
    container = required_environment("DATA_FILESYSTEM_NAME")
    return AzureBlobObjectStore(
        ContainerClient(
            account_url=f"https://{account}.blob.core.windows.net",
            container_name=container,
            credential=DefaultAzureCredential(),
        )
    )


def live_store() -> tuple[InMemoryObjectStore, str]:
    """Ingest the showcase layers from the WFS and annotate them in memory."""

    store = InMemoryObjectStore()
    with httpx.Client(timeout=120) as web:
        version = ShowcaseCatalogIngestion(
            store, web, clock=lambda: datetime.now(UTC)
        ).ingest()
    print(f"ingested live catalog {version[:12]}", file=sys.stderr)
    with annotation_client() as client:
        annotated = MetadataAnnotationJob(store, client).enrich_current()
    print(f"annotated catalog {annotated[:12]}", file=sys.stderr)
    return store, annotated


def load_store(path: Path) -> tuple[InMemoryObjectStore, str]:
    """Restore a store pickled by ``save_store``."""

    objects, catalog_version = pickle.loads(path.read_bytes())
    store = InMemoryObjectStore()
    for key, data in objects.items():
        store.put_immutable(key, data)
    print(f"loaded cached catalog {catalog_version[:12]}", file=sys.stderr)
    return store, catalog_version


def save_store(path: Path, store: InMemoryObjectStore, catalog_version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps((dict(store._objects), catalog_version)))
