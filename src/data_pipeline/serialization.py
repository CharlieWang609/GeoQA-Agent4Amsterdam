# SPDX-License-Identifier: GPL-3.0-only

"""Canonical encodings used for immutable content identities."""

from __future__ import annotations

import hashlib
import json


def canonical_json(value: object) -> bytes:
    """Encode JSON deterministically for hashing and persistence."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def sha256(data: bytes) -> str:
    """Return the lowercase SHA-256 identity without a scheme prefix."""
    return hashlib.sha256(data).hexdigest()
