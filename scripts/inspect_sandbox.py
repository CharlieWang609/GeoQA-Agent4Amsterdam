# SPDX-License-Identifier: GPL-3.0-only

"""Print current Live Sandbox sessions as JSON without mutating Azure Storage."""

from __future__ import annotations

import json

from app.api.sandbox_inventory import inspect_sandbox
from catalog_stores import azure_store


def main() -> int:
    entries = inspect_sandbox(azure_store())
    print(
        json.dumps(
            [entry.model_dump(mode="json") for entry in entries],
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
