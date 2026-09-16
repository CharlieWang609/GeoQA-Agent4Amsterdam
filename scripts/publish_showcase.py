# SPDX-License-Identifier: GPL-3.0-only

"""Freeze accepted Live Sandbox sessions into the public worked examples.

    pixi run publish-showcase SESSION_ID [SESSION_ID ...]

Session ids come from ``pixi run inspect-sandbox``. Earth Engine
observation views are rendered while publishing, so the environment needs
``GEE_SERVICE_ACCOUNT_KEY`` when a session has such outputs.
"""

from __future__ import annotations

import sys

from app.api.showcase import publish_showcase
from catalog_stores import azure_store


def main(session_ids: list[str]) -> int:
    if not session_ids:
        raise SystemExit("usage: publish_showcase.py SESSION_ID [SESSION_ID ...]")
    storage = azure_store()
    for session_id in session_ids:
        session = publish_showcase(storage, session_id)
        print(f"published {session.session_id}: {session.question}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
