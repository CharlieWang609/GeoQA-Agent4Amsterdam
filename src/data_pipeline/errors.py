# SPDX-License-Identifier: GPL-3.0-only

"""Stable errors exposed by governed ingestion and Catalog publication."""


class IneligibleLayerError(ValueError):
    """Raised when source access metadata fails the MVP policy."""


class UnsupportedSourceError(ValueError):
    """Raised when source metadata is not the frozen official WFS source."""


class InvalidSnapshotError(ValueError):
    """Raised when source data cannot satisfy the Catalog contract."""


class CatalogNotPublishedError(LookupError):
    """Raised when no complete Catalog version is available."""


class ConcurrentPublicationError(RuntimeError):
    """Raised when another publisher advances the Catalog first."""
