# SPDX-License-Identifier: GPL-3.0-only

"""Pinned WFS acquisition shared by the two MVP Catalog Layers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

import httpx
from pyproj import CRS

from data_pipeline.errors import UnsupportedSourceError
from data_pipeline.serialization import canonical_json, sha256


@dataclass(frozen=True)
class AcquiredFeatureCollection:
    """Complete WFS pages with source CRS and one aggregate identity."""

    payload: bytes
    original_crs: str
    content_hash: str
    page_count: int


def fetch_all_features(
    client: httpx.Client,
    endpoint: str,
    base_query: dict[str, str],
) -> AcquiredFeatureCollection:
    """Acquire every advertised page from one pinned WFS Feature Type."""
    pages: list[bytes] = []
    features: list[object] = []
    original_crs: str | None = None
    matched_count: int | None = None
    start_index = 0

    # Follow startIndex pagination while a "next" link is advertised, with
    # hard caps and cross-page consistency checks (CRS, numberMatched).
    for _ in range(1000):
        query = dict(base_query)
        if start_index:
            query["startIndex"] = str(start_index)
        response = client.get(
            endpoint,
            params=query,
            headers={"Accept": "application/geo+json, application/json"},
        )
        response.raise_for_status()
        pages.append(response.content)
        document = feature_collection_document(response.content)
        page_features = cast(list[object], document["features"])
        returned = document.get("numberReturned")
        if returned is not None and returned != len(page_features):
            raise UnsupportedSourceError(
                "WFS numberReturned does not match the response page."
            )
        page_crs = response_crs(document)
        if original_crs is None:
            original_crs = page_crs
        elif original_crs != page_crs:
            raise UnsupportedSourceError("WFS CRS changed between pages.")
        reported_match = document.get("numberMatched")
        if isinstance(reported_match, int):
            if matched_count is None:
                matched_count = reported_match
            elif matched_count != reported_match:
                raise UnsupportedSourceError(
                    "WFS numberMatched changed between pages."
                )
        features.extend(page_features)
        if not _has_next_link(document):
            break
        if not page_features:
            raise UnsupportedSourceError("WFS pagination made no progress.")
        start_index += len(page_features)
    else:
        raise UnsupportedSourceError("WFS pagination exceeded 1000 pages.")

    if matched_count is not None and matched_count != len(features):
        raise UnsupportedSourceError(
            "WFS pagination ended before all matched features were returned."
        )
    if original_crs is None:
        raise UnsupportedSourceError("WFS response did not declare a CRS.")
    payload = canonical_json(
        {
            "type": "FeatureCollection",
            "features": features,
        }
    )
    # The source content hash covers the raw pages; length-prefix framing
    # keeps multi-page identities unambiguous under concatenation.
    identity_bytes = (
        pages[0]
        if len(pages) == 1
        else b"".join(len(page).to_bytes(8, "big") + page for page in pages)
    )
    return AcquiredFeatureCollection(
        payload=payload,
        original_crs=original_crs,
        content_hash=sha256(identity_bytes),
        page_count=len(pages),
    )


def feature_collection_document(payload: bytes) -> dict[str, object]:
    """Decode the minimum GeoJSON FeatureCollection response contract."""
    try:
        document = json.loads(payload)
        features = document["features"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise UnsupportedSourceError(
            "WFS response is not a GeoJSON FeatureCollection."
        ) from error
    if document.get("type") != "FeatureCollection" or not isinstance(
        features, list
    ):
        raise UnsupportedSourceError(
            "WFS response is not a GeoJSON FeatureCollection."
        )
    return cast(dict[str, object], document)


def response_crs(document: dict[str, object]) -> str:
    """Return a normalized authority code from WFS GeoJSON metadata."""
    try:
        crs = cast(dict[str, object], document["crs"])
        properties = cast(dict[str, object], crs["properties"])
        declared = str(properties["name"])
        authority = CRS.from_user_input(declared).to_authority()
    except (KeyError, TypeError, ValueError) as error:
        raise UnsupportedSourceError(
            "WFS response has no usable CRS declaration."
        ) from error
    if authority is None:
        raise UnsupportedSourceError("WFS response CRS has no authority code.")
    return f"{authority[0]}:{authority[1]}"


def _has_next_link(document: dict[str, object]) -> bool:
    links = document.get("links", [])
    if not isinstance(links, list):
        raise UnsupportedSourceError("WFS links metadata must be a list.")
    return any(
        isinstance(link, dict) and link.get("rel") == "next" for link in links
    )
