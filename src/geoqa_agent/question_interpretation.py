# SPDX-License-Identifier: GPL-3.0-only

"""Question interpretation and deterministic Catalog data matching."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Mapping, TypeAlias, cast

from data_pipeline.catalog import CatalogReader
from data_pipeline.geoparquet import CANONICAL_CRS
from data_pipeline.models import (
    AnnotationStatus,
    CatalogLayer,
    CatalogVersion,
    SemanticAnnotation,
)
from data_pipeline.serialization import canonical_json
from data_pipeline.storage import ObjectStore
from geoqa_agent.governance import (
    BindingRole,
    CapabilityTaskSpecification,
    CountedObjectSpecification,
    NearestDistanceSpecification,
    NearestPointSpecification,
    NearestTaskSpecification,
    SupportSpecification,
    TaskSpecification,
    TemporalMode,
    data_binding_attribute_is_resolved,
    task_roles,
)
from geoqa_agent.structured_artifacts import (
    ArtifactContract,
    ArtifactProvenance,
    ArtifactRequest,
    StructuredArtifactClient,
)


class FunctionalRole(StrEnum):
    """Question Phrase roles used by the project-level interpretation."""

    MEASURE = "measure"
    CONDITION = "condition"
    SUBCONDITION = "subcondition"
    SUPPORT = "support"
    SPATIAL_EXTENT = "spatial_extent"
    TEMPORAL_EXTENT = "temporal_extent"


@dataclass(frozen=True)
class QuestionPhrase:
    """One normalized, provenance-linked span of the submitted question."""

    text: str
    normalized_meaning: str
    functional_role: FunctionalRole
    referenced_phenomenon: str | None
    referenced_property: str | None
    referenced_relation: str | None
    referenced_place: str | None
    referenced_time: str | None
    quantity: str | None
    unit: str | None
    candidate_ccd_meaning: str | None
    confidence: float
    alternatives: tuple[str, ...]
    provenance: ArtifactProvenance


@dataclass(frozen=True)
class MatchAssessment:
    """One independently observable data-matching decision."""

    passed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DataBinding:
    """One semantic role pinned to an immutable Catalog Layer."""

    role: BindingRole
    capability_input_ref: str
    catalog_version: str
    dataset_id: str
    layer_id: str
    dataset_version: str
    content_hash: str
    topical_relevance: MatchAssessment
    analytical_compatibility: MatchAssessment
    analytical_ccd_meaning: str | None = None


@dataclass(frozen=True)
class CandidateEvaluation:
    """A closest candidate layer and its separate matching assessments."""

    candidate_kind: Literal["catalog_layer"]
    candidate_id: str
    topical_relevance: MatchAssessment
    analytical_compatibility: MatchAssessment
    rejection_reasons: tuple[str, ...]


@dataclass(frozen=True)
class FailedRole:
    """A required role that cannot be grounded in the pinned Catalog."""

    role: str
    rejection_reasons: tuple[str, ...]
    closest_candidates: tuple[CandidateEvaluation, ...]


@dataclass(frozen=True)
class SupportedInterpretation:
    """A fully grounded interpretation ready for workflow generation."""

    question: str
    catalog_version: str
    question_phrases: tuple[QuestionPhrase, ...]
    task_specification: CapabilityTaskSpecification
    assumptions: tuple[str, ...]
    unresolved_ambiguities: tuple[str, ...]
    bindings: tuple[DataBinding, ...]
    provenance: ArtifactProvenance


@dataclass(frozen=True)
class UnsupportedInterpretation:
    """A structured terminal result produced before workflow generation."""

    question: str
    catalog_version: str
    question_phrases: tuple[QuestionPhrase, ...]
    task_specification: CapabilityTaskSpecification
    assumptions: tuple[str, ...]
    unresolved_ambiguities: tuple[str, ...]
    failed_roles: tuple[FailedRole, ...]
    provenance: ArtifactProvenance
    artifact_data: Mapping[str, object]


InterpretationResult: TypeAlias = (
    SupportedInterpretation | UnsupportedInterpretation
)


def interpretation_diagnostic_codes(
    value: UnsupportedInterpretation,
) -> tuple[str, ...]:
    """Return stable codes for persisted interpretation repair provenance."""

    return tuple(
        "unresolved-ambiguity" if failure.role == "interpretation" else "role-grounding"
        for failure in value.failed_roles
    )


def is_repairable_interpretation_failure(
    value: UnsupportedInterpretation,
) -> bool:
    """Grounding failures may be retried with diagnostics; genuine
    ambiguities need the user, not another model attempt."""

    return bool(value.failed_roles) and not value.unresolved_ambiguities


def interpretation_repair_context_document(
    value: UnsupportedInterpretation,
    *,
    attempt: int,
) -> dict[str, object]:
    """Serialize one failed interpretation for a bounded provider repair."""

    diagnostics = [
        {
            "code": code,
            "message": reason,
            "ref": failure.role,
        }
        for failure, code in zip(
            value.failed_roles,
            interpretation_diagnostic_codes(value),
            strict=True,
        )
        for reason in failure.rejection_reasons
    ]
    return {
        "attempt": attempt,
        "failed_artifact": dict(value.artifact_data),
        "diagnostics": diagnostics,
    }


@dataclass(frozen=True)
class _RoleRequirement:
    role: BindingRole
    semantic_label: str
    ccd_meaning: str
    geometry_types: tuple[str, ...]
    source_identity_fields: tuple[str, ...]


@dataclass(frozen=True)
class _CandidateMatch:
    layer: CatalogLayer
    topical: MatchAssessment
    analytical: MatchAssessment

    @property
    def accepted(self) -> bool:
        return self.topical.passed and self.analytical.passed

    def evaluation(self) -> CandidateEvaluation:
        return CandidateEvaluation(
            candidate_kind="catalog_layer",
            candidate_id=f"{self.layer.dataset_id}/{self.layer.layer_id}",
            topical_relevance=self.topical,
            analytical_compatibility=self.analytical,
            rejection_reasons=(
                *(() if self.topical.passed else self.topical.reasons),
                *(() if self.analytical.passed else self.analytical.reasons),
            ),
        )


# Static, family-level descriptions shown to the interpretation model: what
# each supported analysis family means and which semantic roles it binds.
TASK_FAMILY_DOCUMENTS: tuple[Mapping[str, object], ...] = (
    {
        "family": "count",
        "meaning": (
            "Count distinct point objects within polygon support units and "
            "answer from the per-support counts (including explicit zeros)."
        ),
        "roles": [
            {
                "role": BindingRole.SUPPORTS.value,
                "meaning": (
                    "the polygon layer whose units partition space and "
                    "receive the counts"
                ),
            },
            {
                "role": BindingRole.COUNTED_OBJECTS.value,
                "meaning": "the point layer whose distinct objects are counted",
            },
        ],
    },
    {
        "family": "nearest",
        "meaning": (
            "Relate each source point to its nearest target point(s) by "
            "planar distance and report the pairs and distances."
        ),
        "roles": [
            {
                "role": BindingRole.SOURCE_POINTS.value,
                "meaning": "the point layer each result row starts from",
            },
            {
                "role": BindingRole.TARGET_POINTS.value,
                "meaning": "the point layer searched for nearest matches",
            },
        ],
    },
)


class QuestionInterpretationAndMatchingService:
    """Interpret one question and fail closed before workflow generation."""

    def __init__(
        self,
        *,
        storage: ObjectStore,
        client: StructuredArtifactClient,
    ) -> None:
        self._catalog_reader = CatalogReader(storage)
        self._client = client

    def interpret(
        self,
        question: str,
        *,
        catalog_version: str,
        repair_context: Mapping[str, object] | None = None,
    ) -> InterpretationResult:
        """Interpret against one pinned Catalog and ground every role.

        The LLM proposes phrases, a family-typed Task Specification, and
        role requirements; grounding each role in exactly one Catalog Layer
        is deterministic. Any failure yields UnsupportedInterpretation.
        """

        if not question.strip():
            raise ValueError("question must not be empty")
        catalog = self._catalog_reader.get(catalog_version)
        input_document = self._input_document(question, catalog)
        if repair_context is not None:
            input_document["repair"] = dict(repair_context)
        artifact = self._client.generate(
            ArtifactRequest(
                contract=ArtifactContract.QUESTION_INTERPRETATION,
                input_text=canonical_json(input_document).decode(),
            )
        )
        data = artifact.data
        phrases = self._question_phrases(data, artifact.provenance)
        task = self._task_specification(data)
        requirements = self._role_requirements(data)
        assumptions = _strings(data, "assumptions")
        unresolved_ambiguities = _strings(data, "unresolved_ambiguities")

        failed_roles: list[FailedRole] = []
        if unresolved_ambiguities:
            failed_roles.append(
                FailedRole(
                    role="interpretation",
                    rejection_reasons=tuple(
                        f"Essential intent is unresolved: {item}"
                        for item in unresolved_ambiguities
                    ),
                    closest_candidates=(),
                )
            )

        expected_roles = task_roles(task)
        by_role = {requirement.role: requirement for requirement in requirements}
        if len(requirements) != len(expected_roles) or set(by_role) != set(
            expected_roles
        ):
            failed_roles.append(
                FailedRole(
                    role="interpretation",
                    rejection_reasons=(
                        "Role requirements must cover exactly the task "
                        f"family's roles: {[role.value for role in expected_roles]}.",
                    ),
                    closest_candidates=(),
                )
            )
            requirements_to_match: tuple[_RoleRequirement, ...] = ()
        else:
            requirements_to_match = tuple(
                by_role[role] for role in expected_roles
            )

        # Ground each role in the Catalog: exactly one accepted layer per
        # role, otherwise the role fails with ranked closest candidates.
        bindings: list[DataBinding] = []
        for requirement in requirements_to_match:
            matches = tuple(
                self._evaluate_layer(layer, requirement, task)
                for layer in catalog.layers
            )
            accepted = tuple(match for match in matches if match.accepted)
            if len(accepted) != 1:
                reason = (
                    f"No pinned Catalog Layer grounds {requirement.role.value}."
                    if not accepted
                    else (
                        "Multiple pinned Catalog Layers ground "
                        f"{requirement.role.value}."
                    )
                )
                failed_roles.append(
                    FailedRole(
                        role=requirement.role.value,
                        rejection_reasons=(reason,),
                        closest_candidates=tuple(
                            match.evaluation()
                            for match in sorted(
                                matches,
                                key=lambda item: (
                                    not item.topical.passed,
                                    not item.analytical.passed,
                                    item.layer.dataset_id,
                                    item.layer.layer_id,
                                ),
                            )
                        ),
                    )
                )
                continue
            match = accepted[0]
            bindings.append(
                DataBinding(
                    role=requirement.role,
                    capability_input_ref=requirement.role.value,
                    catalog_version=catalog.version,
                    dataset_id=match.layer.dataset_id,
                    layer_id=match.layer.layer_id,
                    dataset_version=match.layer.dataset_version,
                    content_hash=match.layer.content_hash,
                    topical_relevance=match.topical,
                    analytical_compatibility=match.analytical,
                    analytical_ccd_meaning=(
                        None
                        if match.layer.enriched is None
                        else match.layer.enriched.ccd_meaning.value
                    ),
                )
            )

        if failed_roles:
            return UnsupportedInterpretation(
                question=question,
                catalog_version=catalog.version,
                question_phrases=phrases,
                task_specification=task,
                assumptions=assumptions,
                unresolved_ambiguities=unresolved_ambiguities,
                failed_roles=tuple(failed_roles),
                provenance=artifact.provenance,
                artifact_data=data,
            )
        return SupportedInterpretation(
            question=question,
            catalog_version=catalog.version,
            question_phrases=phrases,
            task_specification=task,
            assumptions=assumptions,
            unresolved_ambiguities=unresolved_ambiguities,
            bindings=tuple(bindings),
            provenance=artifact.provenance,
        )

    def _input_document(
        self,
        question: str,
        catalog: CatalogVersion,
    ) -> dict[str, object]:
        """Build the interpretation prompt input: the question, the annotated
        Catalog, and the supported task families - never an expected answer."""

        return {
            "question": question,
            "catalog_version": catalog.version,
            "catalog_layers": [
                self._layer_document(layer) for layer in catalog.layers
            ],
            "task_families": [dict(item) for item in TASK_FAMILY_DOCUMENTS],
        }

    @staticmethod
    def _layer_document(layer: CatalogLayer) -> dict[str, object]:
        enriched = layer.enriched
        return {
            "dataset_id": layer.dataset_id,
            "layer_id": layer.layer_id,
            "dataset_version": layer.dataset_version,
            "content_hash": layer.content_hash,
            "crs": layer.crs,
            "geometry_types": list(layer.vector.geometry_types),
            "source_identity_fields": list(layer.source_identity_fields),
            "temporal_extent": {
                "start": layer.temporal_extent.start.isoformat(),
                "end": (
                    None
                    if layer.temporal_extent.end is None
                    else layer.temporal_extent.end.isoformat()
                ),
            },
            "raw": {
                "name": layer.raw.name,
                "description": layer.raw.description,
            },
            "enriched": None
            if enriched is None
            else {
                "name_en": _annotation_document(enriched.name_en),
                "description_en": _annotation_document(enriched.description_en),
                "semantic_label": _annotation_document(enriched.semantic_label),
                "ccd_meaning": _annotation_document(enriched.ccd_meaning),
                "attributes": [
                    {
                        "name": attribute.name,
                        "semantic_label": _annotation_document(
                            attribute.semantic_label
                        ),
                        "ccd_meaning": _annotation_document(attribute.ccd_meaning),
                    }
                    for attribute in enriched.attributes
                ],
                "provenance": {
                    "provider": enriched.provenance.provider,
                    "model": enriched.provenance.model,
                    "role_settings": dict(enriched.provenance.role_settings),
                    "prompt_version": enriched.provenance.prompt_version,
                    "schema_version": enriched.provenance.schema_version,
                },
            },
        }

    @staticmethod
    def _evaluate_layer(
        layer: CatalogLayer,
        requirement: _RoleRequirement,
        task: CapabilityTaskSpecification,
    ) -> _CandidateMatch:
        """Assess one layer for a role along two independent dimensions.

        Topical relevance: does the layer concern the right phenomenon and
        time? Analytical compatibility: are its annotations, geometry, CRS,
        and identity fields what the proposed requirement needs?
        """

        semantic_label = (
            None if layer.enriched is None else layer.enriched.semantic_label.value
        )
        topical_failures: list[str] = []
        if (
            semantic_label is None
            or semantic_label.casefold() != requirement.semantic_label.casefold()
        ):
            topical_failures.append(
                f"Semantic label {semantic_label!r} does not concern "
                f"{requirement.semantic_label!r}."
            )
        if not _temporal_meaning_matches_snapshot(
            layer,
            task.temporal_mode,
            task.temporal_meaning,
        ):
            topical_failures.append(
                "Catalog Layer snapshot does not cover requested temporal "
                f"meaning {task.temporal_meaning!r}."
            )
        topical = MatchAssessment(
            passed=not topical_failures,
            reasons=tuple(topical_failures)
            if topical_failures
            else (
                "Catalog semantic label concerns the required phenomenon "
                "and the requested time matches the pinned snapshot.",
            ),
        )

        # Analytical compatibility is assessed independently of the topical
        # outcome so rejections can report both failure kinds.
        failures: list[str] = []
        if layer.enriched is None:
            failures.append("Catalog Layer has no semantic annotations.")
        else:
            if layer.enriched.semantic_label.status is not AnnotationStatus.RESOLVED:
                failures.append("Catalog Layer semantic label is unresolved.")
            if layer.enriched.ccd_meaning.status is not AnnotationStatus.RESOLVED:
                failures.append("Catalog Layer CCD meaning is unresolved.")
            if layer.enriched.ccd_meaning.value != requirement.ccd_meaning:
                failures.append(
                    f"Catalog Layer CCD meaning {layer.enriched.ccd_meaning.value!r} "
                    f"does not satisfy the requirement {requirement.ccd_meaning!r}."
                )
            attribute_annotations = {
                attribute.name: attribute
                for attribute in layer.enriched.attributes
            }
            for field in requirement.source_identity_fields:
                annotation = attribute_annotations.get(field)
                if annotation is None:
                    failures.append(
                        f"Required source identity attribute {field!r} has no "
                        "semantic annotation."
                    )
                elif not data_binding_attribute_is_resolved(annotation):
                    failures.append(
                        f"Required source identity attribute {field!r} has "
                        "unresolved semantic annotations."
                    )
        if not set(layer.vector.geometry_types).intersection(
            requirement.geometry_types
        ):
            failures.append(
                f"Geometry types {layer.vector.geometry_types!r} do not satisfy "
                f"{requirement.geometry_types!r}."
            )
        if not set(requirement.source_identity_fields).issubset(
            layer.source_identity_fields
        ):
            failures.append(
                "Catalog Layer lacks required source identity fields: "
                + ", ".join(requirement.source_identity_fields)
                + "."
            )
        if layer.crs != CANONICAL_CRS:
            failures.append(
                f"Catalog Layer CRS {layer.crs!r} is not the canonical "
                f"{CANONICAL_CRS!r}."
            )
        analytical = MatchAssessment(
            passed=not failures,
            reasons=tuple(failures)
            if failures
            else (
                "Resolved CCD, geometry, CRS, and identity requirements are "
                "compatible.",
            ),
        )
        return _CandidateMatch(
            layer=layer,
            topical=topical,
            analytical=analytical,
        )

    @staticmethod
    def _question_phrases(
        data: Mapping[str, object],
        provenance: ArtifactProvenance,
    ) -> tuple[QuestionPhrase, ...]:
        return tuple(
            QuestionPhrase(
                text=_string(item, "text"),
                normalized_meaning=_string(item, "normalized_meaning"),
                functional_role=FunctionalRole(_string(item, "functional_role")),
                referenced_phenomenon=_optional_string(
                    item, "referenced_phenomenon"
                ),
                referenced_property=_optional_string(item, "referenced_property"),
                referenced_relation=_optional_string(item, "referenced_relation"),
                referenced_place=_optional_string(item, "referenced_place"),
                referenced_time=_optional_string(item, "referenced_time"),
                quantity=_optional_string(item, "quantity"),
                unit=_optional_string(item, "unit"),
                candidate_ccd_meaning=_optional_string(
                    item, "candidate_ccd_meaning"
                ),
                confidence=cast(float, item["confidence"]),
                alternatives=_strings(item, "alternatives"),
                provenance=provenance,
            )
            for item in _mappings(data, "question_phrases")
        )

    @staticmethod
    def _task_specification(
        data: Mapping[str, object],
    ) -> CapabilityTaskSpecification:
        task = _mapping(data, "task_specification")
        if "source_points" in task:
            source = _mapping(task, "source_points")
            target = _mapping(task, "target_points")
            distance = _mapping(task, "distance")
            nearest_targets = distance.get("nearest_targets")
            maximum_distance = distance.get("maximum_distance_m")
            retain_all_ties = distance.get("retain_all_ties")
            if type(nearest_targets) is not int:
                raise ValueError("nearest_targets must be an integer")
            if maximum_distance is not None and not isinstance(
                maximum_distance, (int, float)
            ):
                raise ValueError("maximum_distance_m must be numeric or null")
            if not isinstance(retain_all_ties, bool):
                raise ValueError("retain_all_ties must be a boolean")
            return NearestTaskSpecification(
                required_output=_string(task, "required_output"),
                source_points=NearestPointSpecification(
                    semantic_label=_string(source, "semantic_label"),
                    identity_fields=_strings(source, "identity_fields"),
                ),
                target_points=NearestPointSpecification(
                    semantic_label=_string(target, "semantic_label"),
                    identity_fields=_strings(target, "identity_fields"),
                ),
                distance=NearestDistanceSpecification(
                    method=cast(
                        Literal["planar_euclidean", "network"],
                        _string(distance, "method"),
                    ),
                    crs=_string(distance, "crs"),
                    unit=_string(distance, "unit"),
                    nearest_targets=nearest_targets,
                    maximum_distance_m=(
                        None
                        if maximum_distance is None
                        else float(maximum_distance)
                    ),
                    retain_all_ties=retain_all_ties,
                ),
                constraints=_strings(task, "constraints"),
                spatial_extent=_string(task, "spatial_extent"),
                temporal_mode=TemporalMode(_string(task, "temporal_mode")),
                temporal_meaning=_string(task, "temporal_meaning"),
                target_transformation=_strings(task, "target_transformation"),
            )
        support = _mapping(task, "support")
        counted_objects = _mapping(task, "counted_objects")
        return TaskSpecification(
            required_output=_string(task, "required_output"),
            support=SupportSpecification(
                semantic_label=_string(support, "semantic_label"),
                source_state=_string(support, "source_state"),
                identity_fields=_strings(support, "identity_fields"),
            ),
            counted_objects=CountedObjectSpecification(
                semantic_label=_string(counted_objects, "semantic_label"),
                distinct_by=_string(counted_objects, "distinct_by"),
            ),
            constraints=_strings(task, "constraints"),
            spatial_extent=_string(task, "spatial_extent"),
            temporal_mode=TemporalMode(_string(task, "temporal_mode")),
            temporal_meaning=_string(task, "temporal_meaning"),
            target_transformation=_strings(task, "target_transformation"),
        )

    @staticmethod
    def _role_requirements(
        data: Mapping[str, object],
    ) -> tuple[_RoleRequirement, ...]:
        return tuple(
            _RoleRequirement(
                role=BindingRole(_string(item, "role")),
                semantic_label=_string(item, "semantic_label"),
                ccd_meaning=_string(item, "ccd_meaning"),
                geometry_types=_strings(item, "geometry_types"),
                source_identity_fields=_strings(item, "source_identity_fields"),
            )
            for item in _mappings(data, "role_requirements")
        )


def task_specification_document(
    task: CapabilityTaskSpecification,
) -> dict[str, object]:
    """Serialize a Task Specification to its canonical JSON document shape."""

    if isinstance(task, NearestTaskSpecification):
        return {
            "required_output": task.required_output,
            "source_points": {
                "semantic_label": task.source_points.semantic_label,
                "identity_fields": list(task.source_points.identity_fields),
            },
            "target_points": {
                "semantic_label": task.target_points.semantic_label,
                "identity_fields": list(task.target_points.identity_fields),
            },
            "distance": {
                "method": task.distance.method,
                "crs": task.distance.crs,
                "unit": task.distance.unit,
                "nearest_targets": task.distance.nearest_targets,
                "maximum_distance_m": task.distance.maximum_distance_m,
                "retain_all_ties": task.distance.retain_all_ties,
            },
            "constraints": list(task.constraints),
            "spatial_extent": task.spatial_extent,
            "temporal_mode": task.temporal_mode.value,
            "temporal_meaning": task.temporal_meaning,
            "target_transformation": list(task.target_transformation),
        }
    return {
        "required_output": task.required_output,
        "support": {
            "semantic_label": task.support.semantic_label,
            "source_state": task.support.source_state,
            "identity_fields": list(task.support.identity_fields),
        },
        "counted_objects": {
            "semantic_label": task.counted_objects.semantic_label,
            "distinct_by": task.counted_objects.distinct_by,
        },
        "constraints": list(task.constraints),
        "spatial_extent": task.spatial_extent,
        "temporal_mode": task.temporal_mode.value,
        "temporal_meaning": task.temporal_meaning,
        "target_transformation": list(task.target_transformation),
    }


def _annotation_document(annotation: SemanticAnnotation) -> dict[str, object]:
    return {
        "value": annotation.value,
        "source": annotation.source,
        "evidence_refs": list(annotation.evidence_refs),
        "confidence": annotation.confidence,
        "version": annotation.version,
        "status": annotation.status.value,
    }


def _temporal_meaning_matches_snapshot(
    layer: CatalogLayer,
    temporal_mode: TemporalMode,
    temporal_meaning: str,
) -> bool:
    # CURRENT_SNAPSHOT always matches the pinned layer; an as-of request must
    # mention one of the layer's temporal-extent boundary dates or years.
    if temporal_mode is TemporalMode.CURRENT_SNAPSHOT:
        return True
    normalized = " ".join(temporal_meaning.casefold().split())
    extent = layer.temporal_extent
    candidates = {
        extent.start.date().isoformat(),
        str(extent.start.year),
    }
    if extent.end is not None:
        candidates.update(
            {
                extent.end.date().isoformat(),
                str(extent.end.year),
            }
        )
    return any(candidate in normalized for candidate in candidates)


# Plain casts (not runtime checks): the artifact payload was already
# validated against the interpretation JSON schema by the structured client.


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    return cast(Mapping[str, object], value[key])


def _mappings(
    value: Mapping[str, object],
    key: str,
) -> tuple[Mapping[str, object], ...]:
    return tuple(
        cast(Mapping[str, object], item)
        for item in cast(list[object], value[key])
    )


def _string(value: Mapping[str, object], key: str) -> str:
    return cast(str, value[key])


def _optional_string(value: Mapping[str, object], key: str) -> str | None:
    return cast(str | None, value.get(key))


def _strings(value: Mapping[str, object], key: str) -> tuple[str, ...]:
    return tuple(cast(list[str], value[key]))
