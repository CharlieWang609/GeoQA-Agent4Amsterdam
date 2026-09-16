# SPDX-License-Identifier: GPL-3.0-only

"""Immutable data contracts shared by ingestion and Catalog persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Mapping


@dataclass(frozen=True)
class AcquisitionProvenance:
    """Evidence needed to identify and replay the acquisition: a WFS
    feature type, a WCS coverage, or a window of a cloud-optimised GeoTIFF
    (``protocol``). The schema hashes exist for WFS sources only."""

    endpoint: str
    dataset_version: str
    wfs_version: str | None
    feature_type: str
    query: Mapping[str, str]
    retrieved_at: datetime
    source_content_hash: str
    dataset_schema_content_hash: str | None
    feature_schema_content_hash: str | None
    api_key_required: bool
    page_count: int
    protocol: str = "wfs"


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
    # Dataset-level display metadata from the official Amsterdam Schema,
    # defaulted so Catalog versions published before it remain readable.
    dataset_title: str | None = None
    dataset_description: str | None = None


class AnnotationStatus(StrEnum):
    """Whether deterministic checks permit an annotation downstream."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class DataKind(StrEnum):
    """What a Catalog Layer or a workflow ref is: vector features, one
    local raster, a remote image collection, or one remote image derived
    from it (computed on demand by the cloud engine)."""

    VECTOR = "vector"
    RASTER = "raster"
    IMAGE_COLLECTION = "image_collection"
    IMAGE = "image"


class MeasurementScale(StrEnum):
    """What arithmetic an attribute's values support: none (categories and
    identifiers), ordering only, or full numeric operations."""

    NOMINAL = "nominal"
    ORDINAL = "ordinal"
    NUMERIC = "numeric"


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
    measurement_scale: SemanticAnnotation


@dataclass(frozen=True)
class EnrichedLayerMetadata:
    """Semantic annotations kept separate from raw source metadata."""

    name_en: SemanticAnnotation
    description_en: SemanticAnnotation
    semantic_label: SemanticAnnotation
    attributes: tuple[EnrichedAttributeMetadata, ...]
    provenance: AnnotationProvenance
    # Dataset-level English display annotations. Translation only: Datasets
    # carry no semantic label. Defaulted so earlier Catalog versions remain
    # readable.
    dataset_title_en: SemanticAnnotation | None = None
    dataset_description_en: SemanticAnnotation | None = None


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
class RasterMetadata:
    """Raster-specific Catalog fields: the bands play the role attributes
    play for vector layers (annotated the same way), one nodata value and
    the pixel size in CRS units (None for a collection resolved at use)."""

    kind: DataKind
    bands: tuple[AttributeMetadata, ...]
    nodata: float | None
    pixel_size: float | None


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
    vector: VectorMetadata | None
    quality: QualityIndicators
    raster: RasterMetadata | None = None

    @property
    def kind(self) -> DataKind:
        return DataKind.VECTOR if self.raster is None else self.raster.kind

    @property
    def attributes(self) -> tuple[AttributeMetadata, ...]:
        """The annotatable columns: vector attributes or raster bands."""

        if self.raster is not None:
            return self.raster.bands
        assert self.vector is not None
        return self.vector.attributes

    @property
    def geometry_types(self) -> tuple[str, ...]:
        return () if self.vector is None else self.vector.geometry_types


@dataclass(frozen=True)
class CatalogVersion:
    """One complete immutable Catalog view."""

    version: str
    layers: tuple[CatalogLayer, ...]

    def layer(self, dataset_id: str, layer_id: str) -> CatalogLayer:
        """Return the unique layer with this dataset/layer key."""

        return next(
            layer
            for layer in self.layers
            if (layer.dataset_id, layer.layer_id) == (dataset_id, layer_id)
        )
