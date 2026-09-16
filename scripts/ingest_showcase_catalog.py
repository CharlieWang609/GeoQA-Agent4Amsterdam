# SPDX-License-Identifier: GPL-3.0-only

"""Publish the governed Showcase Catalog snapshot."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from catalog_stores import azure_store
from data_pipeline.showcase_catalog import ShowcaseCatalogIngestion


def main() -> None:
    with httpx.Client(timeout=120) as client:
        version = ShowcaseCatalogIngestion(
            azure_store(),
            client,
            clock=lambda: datetime.now(UTC),
        ).ingest()
    print(version)


if __name__ == "__main__":
    main()
