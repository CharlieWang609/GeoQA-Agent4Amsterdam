# SPDX-License-Identifier: GPL-3.0-only

"""Deterministic publication around provider-generated metadata annotations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import cast

from data_pipeline.catalog import CatalogPublisher, CatalogReader
from data_pipeline.errors import (
    ConcurrentPublicationError,
    InvalidSnapshotError,
)
from data_pipeline.models import (
    AnnotationProvenance,
    AnnotationStatus,
    AttributeMetadata,
    CatalogLayer,
    EnrichedAttributeMetadata,
    EnrichedLayerMetadata,
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
from geoqa_agent.semantic_types import AttributeCCDMeaning, LayerCCDMeaning


FROZEN_LAYER_KEYS = {
    ("gebieden", "buurten"),
    ("sport", "openbaresportplek"),
}
SHOWCASE_LAYER_KEYS = {
    *FROZEN_LAYER_KEYS,
    ("sport", "aanbieder"),
    ("sport", "gymzaal"),
    ("sport", "zwembad"),
}
REGION_LAYER_MEANINGS = {
    LayerCCDMeaning.COVERAGE,
    LayerCCDMeaning.LATTICE,
    LayerCCDMeaning.PATCH,
    LayerCCDMeaning.CONTOUR,
}
NUMERIC_ATTRIBUTE_MEANINGS = {
    AttributeCCDMeaning.ORDINAL,
    AttributeCCDMeaning.INTERVAL,
    AttributeCCDMeaning.RATIO,
    AttributeCCDMeaning.COUNT,
    AttributeCCDMeaning.EXTENSIVE_RATIO,
    AttributeCCDMeaning.INTENSIVE_RATIO,
}


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
        keys = {(layer.dataset_id, layer.layer_id) for layer in catalog.layers}
        if keys not in (FROZEN_LAYER_KEYS, SHOWCASE_LAYER_KEYS):
            raise InvalidSnapshotError(
                "Semantic enrichment requires the frozen two-Layer Catalog "
                "or the governed five-Layer Showcase Catalog."
            )

        input_document = self._input_document(catalog.version, catalog.layers)
        artifact = self._client.generate(
            ArtifactRequest(
                contract=ArtifactContract.METADATA_ANNOTATION,
                input_text=canonical_json(input_document).decode(),
            )
        )
        candidates = self._layer_candidates(artifact, catalog.layers)
        enriched_layers = tuple(
            replace(
                layer,
                enriched=self._enrich_layer(
                    layer,
                    candidates.get((layer.dataset_id, layer.layer_id)),
                    artifact,
                    input_document,
                ),
            )
            for layer in catalog.layers
        )
        return CatalogPublisher(self._storage).publish_snapshot(
            enriched_layers,
            base_version=catalog.version,
        )

    @staticmethod
    def _input_document(
        catalog_version: str,
        layers: tuple[CatalogLayer, ...],
    ) -> dict[str, object]:
        return {
            "catalog_version": catalog_version,
            "existing_semantic_labels": (
                MetadataAnnotationJob._existing_semantic_labels(layers)
            ),
            "layers": [MetadataAnnotationJob._layer_input(layer) for layer in layers],
        }

    @staticmethod
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

    @staticmethod
    def _layer_input(layer: CatalogLayer) -> dict[str, object]:
        """Build one layer's prompt input with an addressable evidence map.

        Every value the model may cite gets a stable evidence ref (e.g.
        "raw.name", "technical.attributes.id.sample_values"); annotations
        are later resolved only when they cite these offered refs.
        """

        key = (layer.dataset_id, layer.layer_id)
        relevant = RELEVANT_ATTRIBUTE_NAMES[key]
        governed_labels = GOVERNED_LAYER_SEMANTIC_LABELS.get(key, ())
        evidence: dict[str, object] = {}
        MetadataAnnotationJob._add_evidence(evidence, "raw.name", layer.raw.name)
        MetadataAnnotationJob._add_evidence(
            evidence,
            "raw.description",
            layer.raw.description,
        )
        MetadataAnnotationJob._add_evidence(
            evidence,
            "raw.schema",
            layer.raw.schema,
        )
        technical: dict[str, object] = {}
        if MetadataAnnotationJob._add_evidence(
            evidence,
            "technical.geometry_types",
            layer.vector.geometry_types,
        ):
            technical["geometry_types"] = layer.vector.geometry_types
        if MetadataAnnotationJob._add_evidence(
            evidence,
            "technical.source_identity_fields",
            layer.source_identity_fields,
        ):
            technical["source_identity_fields"] = layer.source_identity_fields
        technical_attributes: dict[str, dict[str, object]] = {}
        properties = MetadataAnnotationJob._schema_properties(layer)
        for attribute in layer.vector.attributes:
            source_name = (
                "geometrie"
                if attribute.name == "geometry" and "geometrie" in properties
                else attribute.name
            )
            MetadataAnnotationJob._add_evidence(
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
                if MetadataAnnotationJob._add_evidence(
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
                attribute.name
                for attribute in layer.vector.attributes
                if attribute.name in relevant
            ],
            "existing_annotations": MetadataAnnotationJob._existing_annotations(
                layer
            ),
            "evidence": evidence,
        }

    @staticmethod
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

    @staticmethod
    def _schema_properties(layer: CatalogLayer) -> Mapping[str, object]:
        schema = layer.raw.schema.get("schema")
        if not isinstance(schema, dict):
            return {}
        properties = schema.get("properties")
        return properties if isinstance(properties, dict) else {}

    @staticmethod
    def _existing_annotations(layer: CatalogLayer) -> dict[str, object] | None:
        if layer.enriched is None:
            return None
        return {
            "name_en": layer.enriched.name_en.value,
            "description_en": layer.enriched.description_en.value,
            "semantic_label": layer.enriched.semantic_label.value,
            "ccd_meaning": layer.enriched.ccd_meaning.value,
        }

    @staticmethod
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

    def _enrich_layer(
        self,
        layer: CatalogLayer,
        candidate: Mapping[str, object] | None,
        artifact: StructuredArtifact,
        input_document: Mapping[str, object],
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
        evidence_refs = self._evidence_refs(layer, input_document)
        semantic_source_refs = frozenset({"raw.name", "raw.description"})
        relevant_attributes = self._relevant_attributes(layer)
        attribute_candidates = self._attribute_candidates(
            layer,
            candidate,
            relevant_attributes,
        )
        return EnrichedLayerMetadata(
            name_en=self._annotation_value(
                None if candidate is None else candidate["name_en"],
                artifact,
                evidence_refs,
                required_evidence_groups=(frozenset({"raw.name"}),),
            ),
            description_en=self._annotation_value(
                None if candidate is None else candidate["description_en"],
                artifact,
                evidence_refs,
                required_evidence_groups=(frozenset({"raw.description"}),),
            ),
            semantic_label=self._annotation_value(
                None if candidate is None else candidate["semantic_label"],
                artifact,
                evidence_refs,
                required_evidence_groups=(semantic_source_refs,),
                consistent=(
                    None
                    if candidate is None
                    else self._layer_semantic_label_is_consistent(
                        layer,
                        cast(Mapping[str, object], candidate["semantic_label"])[
                            "value"
                        ],
                    )
                ),
            ),
            ccd_meaning=self._annotation_value(
                None if candidate is None else candidate["ccd_meaning"],
                artifact,
                evidence_refs,
                required_evidence_groups=(
                    semantic_source_refs,
                    frozenset({"technical.geometry_types"}),
                ),
                consistent=(
                    None
                    if candidate is None
                    else self._layer_ccd_is_consistent(
                        layer,
                        cast(Mapping[str, object], candidate["ccd_meaning"])[
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
        )

    @staticmethod
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

    @staticmethod
    def _relevant_attributes(layer: CatalogLayer) -> tuple[AttributeMetadata, ...]:
        names = RELEVANT_ATTRIBUTE_NAMES[(layer.dataset_id, layer.layer_id)]
        return tuple(
            attribute
            for attribute in layer.vector.attributes
            if attribute.name in names
        )

    @staticmethod
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
                raise AnnotationArtifactError(
                    "Annotation targets an irrelevant or unknown attribute: "
                    f"{layer.dataset_id}/{layer.layer_id}/{name}."
                )
            if name in candidates:
                raise AnnotationArtifactError(
                    "Annotation repeats an attribute: "
                    f"{layer.dataset_id}/{layer.layer_id}/{name}."
                )
            candidates[name] = item
        return candidates

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
            name_en=self._annotation_value(
                None if candidate is None else candidate["name_en"],
                artifact,
                evidence_refs,
                required_evidence_groups=(schema_ref,),
            ),
            description_en=self._annotation_value(
                None if candidate is None else candidate["description_en"],
                artifact,
                evidence_refs,
                required_evidence_groups=(schema_ref, sample_ref),
            ),
            semantic_label=self._annotation_value(
                None if candidate is None else candidate["semantic_label"],
                artifact,
                evidence_refs,
                required_evidence_groups=(schema_ref, sample_ref),
            ),
            ccd_meaning=self._annotation_value(
                None if candidate is None else candidate["ccd_meaning"],
                artifact,
                evidence_refs,
                required_evidence_groups=(
                    frozenset({f"{prefix}.source_type"}),
                    sample_ref,
                ),
                consistent=(
                    None
                    if candidate is None
                    else self._attribute_ccd_is_consistent(
                        layer,
                        attribute,
                        cast(Mapping[str, object], candidate["ccd_meaning"])[
                            "value"
                        ],
                    )
                ),
            ),
        )

    @staticmethod
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

    @staticmethod
    def _layer_ccd_is_consistent(layer: CatalogLayer, value: object) -> bool:
        """Plausibility gate: the proposed layer meaning must be compatible
        with the layer's observed geometry types."""

        meaning = LayerCCDMeaning(str(value))
        geometry_types = set(layer.vector.geometry_types)
        if geometry_types and geometry_types <= {"Point", "MultiPoint"}:
            return meaning in {
                LayerCCDMeaning.OBJECT,
                LayerCCDMeaning.EVENT,
                LayerCCDMeaning.POINT_MEASURES,
            }
        if geometry_types and geometry_types <= {"Polygon", "MultiPolygon"}:
            return meaning in {
                LayerCCDMeaning.OBJECT,
                *REGION_LAYER_MEANINGS,
            }
        if geometry_types and geometry_types <= {"LineString", "MultiLineString"}:
            return meaning in {
                LayerCCDMeaning.OBJECT,
                LayerCCDMeaning.EVENT,
                LayerCCDMeaning.NETWORK,
            }
        return False

    @staticmethod
    def _layer_semantic_label_is_consistent(
        layer: CatalogLayer,
        value: object,
    ) -> bool:
        governed_labels = GOVERNED_LAYER_SEMANTIC_LABELS.get(
            (layer.dataset_id, layer.layer_id)
        )
        return governed_labels is None or str(value) in governed_labels

    @staticmethod
    def _attribute_ccd_is_consistent(
        layer: CatalogLayer,
        attribute: AttributeMetadata,
        value: object,
    ) -> bool:
        """Plausibility gate: the proposed measurement scale must fit the
        attribute's source type, unit, identity role, and sample values."""

        meaning = AttributeCCDMeaning(str(value))
        samples = tuple(
            sample for sample in attribute.sample_values if sample is not None
        )
        if not samples:
            return False
        if attribute.name in layer.source_identity_fields:
            return meaning is AttributeCCDMeaning.NOMINAL
        if attribute.unit is not None:
            return meaning in {
                AttributeCCDMeaning.RATIO,
                AttributeCCDMeaning.EXTENSIVE_RATIO,
                AttributeCCDMeaning.INTENSIVE_RATIO,
            } and all(
                isinstance(sample, int | float) and not isinstance(sample, bool)
                for sample in samples
            )
        source_type = attribute.source_type.lower()
        if "date" in source_type or "time" in source_type:
            return meaning in {
                AttributeCCDMeaning.ORDINAL,
                AttributeCCDMeaning.INTERVAL,
            }
        if "bool" in source_type:
            return meaning is AttributeCCDMeaning.BOOLEAN and all(
                isinstance(sample, bool) for sample in samples
            )
        if "string" in source_type:
            return meaning is AttributeCCDMeaning.NOMINAL
        if (
            "int" in source_type
            or "number" in source_type
            or "float" in source_type
        ):
            if not all(
                isinstance(sample, int | float) and not isinstance(sample, bool)
                for sample in samples
            ):
                return False
            if meaning is AttributeCCDMeaning.COUNT:
                return all(
                    isinstance(sample, int) and sample >= 0 for sample in samples
                )
            return meaning in NUMERIC_ATTRIBUTE_MEANINGS
        return False
