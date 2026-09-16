# SPDX-License-Identifier: GPL-3.0-only

"""Catalog eligibility rules shared by governed source adapters."""

from __future__ import annotations

from data_pipeline.errors import IneligibleLayerError

PUBLIC_ACCESS = {"public", "openbaar"}


def require_public_access(
    dataset_access_value: object,
    feature_type_access_value: object,
) -> tuple[str, str | None, str]:
    """Resolve Amsterdam Schema access inheritance and enforce ADR-0005."""
    raw_dataset_access = (
        dataset_access_value if isinstance(dataset_access_value, str) else None
    )
    raw_feature_type_access = (
        feature_type_access_value
        if isinstance(feature_type_access_value, str)
        else None
    )
    if (
        raw_dataset_access is None
        or raw_dataset_access.casefold() not in PUBLIC_ACCESS
    ):
        raise IneligibleLayerError("Dataset is not public.")

    # Amsterdam Schema table access inherits the Dataset classification when
    # the table has no explicit auth value. Raw metadata remains unchanged.
    effective_feature_type_access = (
        raw_feature_type_access or raw_dataset_access
    )
    if effective_feature_type_access.casefold() not in PUBLIC_ACCESS:
        raise IneligibleLayerError("Feature Type is not public.")
    return (
        raw_dataset_access,
        raw_feature_type_access,
        effective_feature_type_access,
    )
