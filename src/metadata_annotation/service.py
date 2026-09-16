# SPDX-License-Identifier: GPL-3.0-only

"""Deterministic publication around provider-generated metadata annotations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import cast

from data_pipeline.catalog import CatalogPublisher, CatalogReader
from data_pipeline.errors import ConcurrentPublicationError
from data_pipeline.models import (
    AnnotationProvenance,
    AnnotationStatus,
    AttributeMetadata,
    CatalogLayer,
    EnrichedAttributeMetadata,
    EnrichedLayerMetadata,
    MeasurementScale,
    SemanticAnnotation,
)
from data_pipeline.serialization import canonical_json
from data_pipeline.storage import ObjectStore
from geoqa_agent.governance import (
    GOVERNED_LAYER_SEMANTIC_LABELS,
    RELEVANT_ATTRIBUTE_NAMES,
)
from geoqa_agent.structured_artifacts import (
    ArtifactContract,
    ArtifactRequest,
    StructuredArtifact,
    StructuredArtifactClient,
)


class AnnotationArtifactError(ValueError):
    """Raised when a valid artifact targets undeclared Catalog entities."""


class MetadataAnnotationJob:
    """Enrich and atomically republish an accepted governed Catalog shape."""

    def __init__(
        self,
        storage: ObjectStore,
        client: StructuredArtifactClient,
    ) -> None:
        self._storage = storage
        self._client = client

    def enrich_current(
        self,
        *,
        expected_catalog_version: str | None = None,
    ) -> str:
        """Generate annotations and publish one new immutable Catalog version."""
        catalog = CatalogReader(self._storage).current()
        if (
            expected_catalog_version is not None
            and catalog.version != expected_catalog_version
        ):
            raise ConcurrentPublicationError(
                "Catalog pointer changed before semantic enrichment."
            )
        input_document = _input_document(catalog.version, catalog.layers)
        artifact = self._client.generate(
            ArtifactRequest(
                contract=ArtifactContract.METADATA_ANNOTATION,
                input_text=canonical_json(input_document).decode(),
            )
        )
        candidates = _layer_candidates(artifact, catalog.layers)
        dataset_annotations = self._dataset_annotations(
            artifact,
            catalog.layers,
            input_document,
        )
        enriched_layers = tuple(
            replace(
                layer,
                enriched=self._enrich_layer(
                    layer,
                    candidates.get((layer.dataset_id, layer.layer_id)),
                    artifact,
                    input_document,
                    dataset_annotations[layer.dataset_id],
                ),
            )
            for layer in catalog.layers
        )
        return CatalogPublisher(self._storage).publish_snapshot(
            enriched_layers,
            base_version=catalog.version,
        )

    def _dataset_annotations(
        self,
        artifact: StructuredArtifact,
        layers: tuple[CatalogLayer, ...],
        input_document: Mapping[str, object],
    ) -> dict[str, tuple[SemanticAnnotation, SemanticAnnotation]]:
        """Gate the per-dataset title/description translations once, so
        every Layer of a dataset shares one consistent annotation pair."""

        expected = {layer.dataset_id for layer in layers}
        candidates: dict[str, Mapping[str, object]] = {}
        for candidate in cast(
            list[Mapping[str, object]], artifact.data["datasets"]
        ):
            dataset_id = str(candidate["dataset_id"])
            if dataset_id not in expected:
                raise AnnotationArtifactError(
                    f"Annotation targets an unknown Dataset: {dataset_id}."
                )
            if dataset_id in candidates:
                raise AnnotationArtifactError(
                    f"Annotation repeats a Dataset: {dataset_id}."
                )
            candidates[dataset_id] = candidate
        annotations: dict[str, tuple[SemanticAnnotation, SemanticAnnotation]] = {}
        for dataset_input in cast(
            list[Mapping[str, object]], input_document["datasets"]
        ):
            dataset_id = str(dataset_input["dataset_id"])
            proposed = candidates.get(dataset_id)
            evidence_refs = set(
                cast(Mapping[str, object], dataset_input["evidence"])
            )
            annotations[dataset_id] = (
                _annotation_value(
                    None if proposed is None else proposed["title_en"],
                    artifact,
                    evidence_refs,
                    required_evidence_groups=(frozenset({"raw.title"}),),
                ),
                _annotation_value(
                    None if proposed is None else proposed["description_en"],
                    artifact,
                    evidence_refs,
                    required_evidence_groups=(frozenset({"raw.description"}),),
                ),
            )
        return annotations

    def _enrich_layer(
        self,
        layer: CatalogLayer,
        candidate: Mapping[str, object] | None,
        artifact: StructuredArtifact,
        input_document: Mapping[str, object],
        dataset_annotations: tuple[SemanticAnnotation, SemanticAnnotation],
    ) -> EnrichedLayerMetadata:
        """Turn the model's candidate into gated annotations; each field is
        resolved only with complete evidence and a passing consistency check."""

        provenance = AnnotationProvenance(
            provider=artifact.provenance.provider,
            model=artifact.provenance.model,
            role_settings={
                "reasoning_effort": (
                    artifact.provenance.settings.reasoning_effort
                ),
                "max_output_tokens": (
                    artifact.provenance.settings.max_output_tokens
                ),
            },
            prompt_version=artifact.provenance.prompt_version,
            schema_version=artifact.provenance.schema_version,
        )
        evidence_refs = _evidence_refs(layer, input_document)
        semantic_source_refs = frozenset({"raw.name", "raw.description"})
        relevant_attributes = _relevant_attributes(layer)
        attribute_candidates = _attribute_candidates(
            layer,
            candidate,
            relevant_attributes,
        )
        return EnrichedLayerMetadata(
            name_en=_annotation_value(
                None if candidate is None else candidate["name_en"],
                artifact,
                evidence_refs,
                required_evidence_groups=(frozenset({"raw.name"}),),
            ),
            description_en=_annotation_value(
                None if candidate is None else candidate["description_en"],
                artifact,
                evidence_refs,
                required_evidence_groups=(frozenset({"raw.description"}),),
            ),
            semantic_label=_annotation_value(
                None if candidate is None else candidate["semantic_label"],
                artifact,
                evidence_refs,
                required_evidence_groups=(semantic_source_refs,),
                consistent=(
                    None
                    if candidate is None
                    else _layer_semantic_label_is_consistent(
                        layer,
                        cast(Mapping[str, object], candidate["semantic_label"])[
                            "value"
                        ],
                    )
                ),
            ),
            attributes=tuple(
                self._enrich_attribute(
                    layer,
                    attribute,
                    attribute_candidates.get(attribute.name),
                    artifact,
                    evidence_refs,
                )
                for attribute in relevant_attributes
            ),
            provenance=provenance,
            dataset_title_en=dataset_annotations[0],
            dataset_description_en=dataset_annotations[1],
        )

    def _enrich_attribute(
        self,
        layer: CatalogLayer,
        attribute: AttributeMetadata,
        candidate: Mapping[str, object] | None,
        artifact: StructuredArtifact,
        evidence_refs: set[str],
    ) -> EnrichedAttributeMetadata:
        prefix = f"technical.attributes.{attribute.name}"
        schema_ref = frozenset({f"raw.schema.{attribute.name}"})
        sample_ref = frozenset({f"{prefix}.sample_values"})
        return EnrichedAttributeMetadata(
            name=attribute.name,
            name_en=_annotation_value(
                None if candidate is None else candidate["name_en"],
                artifact,
                evidence_refs,
                required_evidence_groups=(schema_ref,),
            ),
            description_en=_annotation_value(
                None if candidate is None else candidate["description_en"],
                artifact,
                evidence_refs,
                required_evidence_groups=(schema_ref, sample_ref),
            ),
            semantic_label=_annotation_value(
                None if candidate is None else candidate["semantic_label"],
                artifact,
                evidence_refs,
                required_evidence_groups=(schema_ref, sample_ref),
            ),
            measurement_scale=(
                _governed_annotation(
                    artifact,
                    MeasurementScale.NOMINAL.value,
                    evidence_ref="technical.source_identity_fields",
                )
                if attribute.name in layer.source_identity_fields
                else _annotation_value(
                    None if candidate is None else candidate["measurement_scale"],
                    artifact,
                    evidence_refs,
                    required_evidence_groups=(
                        frozenset({f"{prefix}.source_type"}),
                        sample_ref,
                    ),
                    consistent=(
                        None
                        if candidate is None
                        else _measurement_scale_is_consistent(
                            attribute,
                            cast(
                                Mapping[str, object], candidate["measurement_scale"]
                            )["value"],
                        )
                    ),
                )
            ),
        )


def _governed_annotation(
    artifact: StructuredArtifact,
    value: str,
    *,
    evidence_ref: str,
) -> SemanticAnnotation:
    """A value fixed by governance (the identity fields' nominal scale)
    rather than proposed by the model."""

    return SemanticAnnotation(
        value=value,
        source="governance",
        evidence_refs=(evidence_ref,),
        confidence=1.0,
        version=artifact.provenance.schema_version,
        status=AnnotationStatus.RESOLVED,
    )


def _input_document(
    catalog_version: str,
    layers: tuple[CatalogLayer, ...],
) -> dict[str, object]:
    dataset_ids = list(dict.fromkeys(layer.dataset_id for layer in layers))
    return {
        "catalog_version": catalog_version,
        "existing_semantic_labels": (
            _existing_semantic_labels(layers)
        ),
        "datasets": [
            _dataset_input(dataset_id, layers)
            for dataset_id in dataset_ids
        ],
        "layers": [_layer_input(layer) for layer in layers],
    }


def _dataset_input(
    dataset_id: str,
    layers: tuple[CatalogLayer, ...],
) -> dict[str, object]:
    """Build one dataset's translation-only prompt input.

    Datasets get no semantic label; the model only
    translates the official title and description, citing the dataset's
    own evidence refs.
    """

    layer = next(
        layer for layer in layers if layer.dataset_id == dataset_id
    )
    evidence: dict[str, object] = {}
    _add_evidence(
        evidence,
        "raw.title",
        layer.raw.dataset_title,
    )
    _add_evidence(
        evidence,
        "raw.description",
        layer.raw.dataset_description,
    )
    existing = next(
        (
            {
                "title_en": enriched.dataset_title_en.value,
                "description_en": enriched.dataset_description_en.value,
            }
            for candidate in layers
            if candidate.dataset_id == dataset_id
            and (enriched := candidate.enriched) is not None
            and enriched.dataset_title_en is not None
            and enriched.dataset_description_en is not None
        ),
        None,
    )
    return {
        "dataset_id": dataset_id,
        "raw": {
            "title": layer.raw.dataset_title,
            "description": layer.raw.dataset_description,
        },
        "existing_annotations": existing,
        "evidence": evidence,
    }


def _existing_semantic_labels(
    layers: tuple[CatalogLayer, ...],
) -> list[str]:
    current_keys = {
        (layer.dataset_id, layer.layer_id) for layer in layers
    }
    labels = {
        label
        for key, governed_labels in GOVERNED_LAYER_SEMANTIC_LABELS.items()
        if key in current_keys
        for label in governed_labels
    }
    for layer in layers:
        if layer.enriched is None:
            continue
        layer_label = layer.enriched.semantic_label.value
        if layer_label is not None:
            labels.add(layer_label)
        labels.update(
            label
            for attribute in layer.enriched.attributes
            if (label := attribute.semantic_label.value) is not None
        )
    return sorted(labels)


def _layer_input(layer: CatalogLayer) -> dict[str, object]:
    """Build one layer's prompt input with an addressable evidence map.

    Every value the model may cite gets a stable evidence ref (e.g.
    "raw.name", "technical.attributes.id.sample_values"); annotations
    are later resolved only when they cite these offered refs.
    """

    key = (layer.dataset_id, layer.layer_id)
    governed_labels = GOVERNED_LAYER_SEMANTIC_LABELS.get(key, ())
    evidence: dict[str, object] = {}
    _add_evidence(evidence, "raw.name", layer.raw.name)
    _add_evidence(
        evidence,
        "raw.description",
        layer.raw.description,
    )
    _add_evidence(
        evidence,
        "raw.schema",
        layer.raw.schema,
    )
    technical: dict[str, object] = {"data_kind": layer.kind.value}
    if _add_evidence(
        evidence,
        "technical.geometry_types",
        layer.geometry_types,
    ):
        technical["geometry_types"] = layer.geometry_types
    if layer.raster is not None and _add_evidence(
        evidence, "technical.pixel_size", layer.raster.pixel_size
    ):
        technical["pixel_size"] = layer.raster.pixel_size
    if _add_evidence(
        evidence,
        "technical.source_identity_fields",
        layer.source_identity_fields,
    ):
        technical["source_identity_fields"] = layer.source_identity_fields
    technical_attributes: dict[str, dict[str, object]] = {}
    properties = _schema_properties(layer)
    for attribute in layer.attributes:
        source_name = (
            "geometrie"
            if attribute.name == "geometry" and "geometrie" in properties
            else attribute.name
        )
        _add_evidence(
            evidence,
            f"raw.schema.{attribute.name}",
            properties.get(source_name),
        )
        prefix = f"technical.attributes.{attribute.name}"
        technical_attribute: dict[str, object] = {}
        for field, value in (
            ("source_type", attribute.source_type),
            ("storage_type", attribute.storage_type),
            ("unit", attribute.unit),
            ("sample_values", attribute.sample_values),
        ):
            if _add_evidence(
                evidence,
                f"{prefix}.{field}",
                value,
            ):
                technical_attribute[field] = value
        technical_attributes[attribute.name] = technical_attribute
    technical["attributes"] = technical_attributes
    return {
        "dataset_id": layer.dataset_id,
        "layer_id": layer.layer_id,
        "raw": {
            "name": layer.raw.name,
            "description": layer.raw.description,
            "schema": layer.raw.schema,
        },
        "technical": technical,
        "governed_semantic_labels": list(governed_labels),
        "relevant_attributes": [
            attribute.name for attribute in _relevant_attributes(layer)
        ],
        "existing_annotations": _existing_annotations(
            layer
        ),
        "evidence": evidence,
    }


def _add_evidence(
    evidence: dict[str, object],
    reference: str,
    value: object,
) -> bool:
    # Only non-empty values become citable evidence refs.
    if (
        value is None
        or value == ""
        or value == ()
        or value == []
        or value == {}
    ):
        return False
    evidence[reference] = value
    return True


def _schema_properties(layer: CatalogLayer) -> Mapping[str, object]:
    schema = layer.raw.schema.get("schema")
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties")
    return properties if isinstance(properties, dict) else {}


def _existing_annotations(layer: CatalogLayer) -> dict[str, object] | None:
    if layer.enriched is None:
        return None
    return {
        "name_en": layer.enriched.name_en.value,
        "description_en": layer.enriched.description_en.value,
        "semantic_label": layer.enriched.semantic_label.value,
    }


def _layer_candidates(
    artifact: StructuredArtifact,
    layers: tuple[CatalogLayer, ...],
) -> dict[tuple[str, str], Mapping[str, object]]:
    expected = {(layer.dataset_id, layer.layer_id) for layer in layers}
    candidates: dict[tuple[str, str], Mapping[str, object]] = {}
    for candidate in cast(list[Mapping[str, object]], artifact.data["layers"]):
        key = (str(candidate["dataset_id"]), str(candidate["layer_id"]))
        if key not in expected:
            raise AnnotationArtifactError(
                f"Annotation targets an unknown Catalog Layer: {key}."
            )
        if key in candidates:
            raise AnnotationArtifactError(
                f"Annotation repeats a Catalog Layer: {key}."
            )
        candidates[key] = candidate
    return candidates


def _evidence_refs(
    layer: CatalogLayer,
    input_document: Mapping[str, object],
) -> set[str]:
    for layer_input in cast(
        list[Mapping[str, object]], input_document["layers"]
    ):
        if (
            layer_input["dataset_id"] == layer.dataset_id
            and layer_input["layer_id"] == layer.layer_id
        ):
            return set(cast(Mapping[str, object], layer_input["evidence"]))
    raise AssertionError("Catalog Layer input was not constructed.")


def _relevant_attributes(layer: CatalogLayer) -> tuple[AttributeMetadata, ...]:
    """The governed attribute subset of a vector layer; every band of a raster."""

    if layer.raster is not None:
        return layer.raster.bands
    names = RELEVANT_ATTRIBUTE_NAMES[(layer.dataset_id, layer.layer_id)]
    return tuple(
        attribute for attribute in layer.attributes if attribute.name in names
    )


def _attribute_candidates(
    layer: CatalogLayer,
    candidate: Mapping[str, object] | None,
    relevant: tuple[AttributeMetadata, ...],
) -> dict[str, Mapping[str, object]]:
    if candidate is None:
        return {}
    expected = {attribute.name for attribute in relevant}
    candidates: dict[str, Mapping[str, object]] = {}
    for item in cast(list[Mapping[str, object]], candidate["attributes"]):
        name = str(item["name"])
        if name not in expected:
            # The model sees the whole schema and sometimes annotates
            # attributes beyond the governed relevant set; those are noise.
            continue
        if name in candidates:
            raise AnnotationArtifactError(
                "Annotation repeats an attribute: "
                f"{layer.dataset_id}/{layer.layer_id}/{name}."
            )
        candidates[name] = item
    return candidates


def _annotation_value(
    document: object | None,
    artifact: StructuredArtifact,
    allowed_evidence_refs: set[str],
    *,
    required_evidence_groups: tuple[frozenset[str], ...],
    consistent: bool | None = True,
) -> SemanticAnnotation:
    """Wrap one proposed value, deciding RESOLVED vs UNRESOLVED.

    RESOLVED requires: a value, evidence refs drawn only from the refs
    actually offered to the model, at least one ref from every required
    group, and a passing deterministic consistency check. Anything else
    is stored UNRESOLVED and blocks downstream matching.
    """

    if document is None:
        value = None
        evidence_refs: tuple[str, ...] = ()
        confidence = 0.0
    else:
        values = cast(Mapping[str, object], document)
        value = str(values["value"])
        evidence_refs = tuple(
            str(reference)
            for reference in cast(list[object], values["evidence_refs"])
        )
        confidence = float(cast(float, values["confidence"]))
    evidence_is_complete = bool(evidence_refs) and all(
        reference in allowed_evidence_refs for reference in evidence_refs
    ) and all(
        any(reference in group for reference in evidence_refs)
        for group in required_evidence_groups
    )
    resolved = (
        value is not None
        and evidence_is_complete
        and consistent is True
    )
    return SemanticAnnotation(
        value=value,
        source="llm",
        evidence_refs=evidence_refs,
        confidence=confidence,
        version=artifact.provenance.schema_version,
        status=(
            AnnotationStatus.RESOLVED
            if resolved
            else AnnotationStatus.UNRESOLVED
        ),
    )


def _layer_semantic_label_is_consistent(
    layer: CatalogLayer,
    value: object,
) -> bool:
    governed_labels = GOVERNED_LAYER_SEMANTIC_LABELS.get(
        (layer.dataset_id, layer.layer_id)
    )
    return governed_labels is None or str(value) in governed_labels


def _measurement_scale_is_consistent(
    attribute: AttributeMetadata,
    value: object,
) -> bool:
    """Plausibility gate: the proposed measurement scale must fit the
    attribute's source type, unit, and sample values."""

    scale = MeasurementScale(str(value))
    samples = tuple(
        sample for sample in attribute.sample_values if sample is not None
    )
    if not samples:
        return False
    numeric_samples = all(
        isinstance(sample, int | float) and not isinstance(sample, bool)
        for sample in samples
    )
    if attribute.unit is not None:
        return scale is MeasurementScale.NUMERIC and numeric_samples
    source_type = attribute.source_type.lower()
    if "date" in source_type or "time" in source_type:
        return scale is MeasurementScale.ORDINAL
    if "bool" in source_type or "string" in source_type:
        return scale is MeasurementScale.NOMINAL
    if "int" in source_type or "number" in source_type or "float" in source_type:
        return numeric_samples and scale is not MeasurementScale.ORDINAL
    return False
