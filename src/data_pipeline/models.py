# SPDX-License-Identifier: GPL-3.0-only

"""Immutable data contracts shared by ingestion and Catalog persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping


@dataclass(frozen=True)
class AcquisitionProvenance:
    """Evidence needed to identify and replay the WFS acquisition."""

    endpoint: str
    dataset_version: str
    wfs_version: str
    feature_type: str
    query: Mapping[str, str]
    retrieved_at: datetime
    source_content_hash: str
    dataset_schema_content_hash: str
    feature_schema_content_hash: str
    api_key_required: bool
    page_count: int


@dataclass(frozen=True)
class RawAccessMetadata:
    """Source values before Amsterdam Schema inheritance or project policy."""

    dataset: str | None
    feature_type: str | None
    reuse_license: str | None


@dataclass(frozen=True)
class RawLayerMetadata:
    """Unmodified source metadata, separate from later enrichment."""

    name: str
    description: str | None
    schema: Mapping[str, object]
    access: RawAccessMetadata
    provenance: AcquisitionProvenance


class AnnotationStatus(StrEnum):
    """Whether deterministic checks permit an annotation downstream."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class SemanticAnnotation:
    """One semantic claim with its evidence and resolution decision."""

    value: str | None
    source: str
    evidence_refs: tuple[str, ...]
    confidence: float
    version: str
    status: AnnotationStatus


@dataclass(frozen=True)
class AnnotationProvenance:
    """Exact generation configuration for one enrichment run."""

    provider: str
    model: str
    role_settings: Mapping[str, object]
    prompt_version: str
    schema_version: str


@dataclass(frozen=True)
class EnrichedAttributeMetadata:
    """Semantic annotations for one relevant technical attribute."""

    name: str
    name_en: SemanticAnnotation
    description_en: SemanticAnnotation
    semantic_label: SemanticAnnotation
    ccd_meaning: SemanticAnnotation


@dataclass(frozen=True)
class EnrichedLayerMetadata:
    """Semantic annotations kept separate from raw source metadata."""

    name_en: SemanticAnnotation
    description_en: SemanticAnnotation
    semantic_label: SemanticAnnotation
    ccd_meaning: SemanticAnnotation
    attributes: tuple[EnrichedAttributeMetadata, ...]
    provenance: AnnotationProvenance


@dataclass(frozen=True)
class EligibilityDecision:
    """Effective two-level access decision and its project-policy basis."""

    dataset_access: str
    feature_type_access: str
    policy_basis: str


@dataclass(frozen=True)
class AttributeMetadata:
    """Technical source and persisted types for one vector attribute."""

    name: str
    source_type: str
    storage_type: str
    unit: str | None
    sample_values: tuple[object, ...]


@dataclass(frozen=True)
class VectorMetadata:
    """Vector-specific Catalog fields required by the MVP."""

    geometry_types: tuple[str, ...]
    feature_count: int
    attributes: tuple[AttributeMetadata, ...]


@dataclass(frozen=True)
class TemporalExtent:
    """Validity extent represented by the published source states."""

    start: datetime
    end: datetime | None


@dataclass(frozen=True)
class QualityDiagnostic:
    """One auditable data-quality category with affected source records."""

    category: str
    count: int
    record_refs: tuple[str, ...]


@dataclass(frozen=True)
class QualityIndicators:
    """Deterministic ingestion diagnostics available to Catalog readers."""

    invalid_geometry_count: int
    diagnostics: tuple[QualityDiagnostic, ...] = ()


@dataclass(frozen=True)
class CatalogLayer:
    """One immutable raw Catalog layer and its technical envelope."""

    dataset_id: str
    layer_id: str
    dataset_version: str
    content_hash: str
    storage_path: str
    format: str
    crs: str
    original_crs: str
    spatial_extent: tuple[float, float, float, float] | None
    temporal_extent: TemporalExtent
    source_identity_fields: tuple[str, ...]
    raw: RawLayerMetadata
    enriched: EnrichedLayerMetadata | None
    eligibility: EligibilityDecision
    vector: VectorMetadata
    quality: QualityIndicators


@dataclass(frozen=True)
class CatalogVersion:
    """One complete immutable Catalog view."""

    version: str
    layers: tuple[CatalogLayer, ...]
