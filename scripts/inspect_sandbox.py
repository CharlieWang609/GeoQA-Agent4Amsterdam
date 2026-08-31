# SPDX-License-Identifier: GPL-3.0-only

"""Print current Live Sandbox sessions as JSON without mutating Azure Storage."""

from __future__ import annotations

import json
import os
import sys

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from app.api.sandbox_inventory import inspect_sandbox
from data_pipeline.azure_storage import AzureBlobObjectStore


def main() -> int:
    account_name = _required_environment("AZURE_STORAGE_ACCOUNT_NAME")
    container_name = _required_environment("DATA_FILESYSTEM_NAME")
    credential = DefaultAzureCredential()
    service = BlobServiceClient(
        account_url=f"https://{account_name}.blob.core.windows.net",
        credential=credential,
    )
    try:
        storage = AzureBlobObjectStore(
            service.get_container_client(container_name)
        )
        entries = inspect_sandbox(storage)
    finally:
        service.close()
        credential.close()
    print(
        json.dumps(
            [entry.model_dump(mode="json") for entry in entries],
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        print(f"{name} is required.", file=sys.stderr)
        raise SystemExit(2)
    return value.strip()


if __name__ == "__main__":
    raise SystemExit(main())
