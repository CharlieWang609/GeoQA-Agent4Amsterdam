# SPDX-License-Identifier: GPL-3.0-only

"""Semantic vocabulary and typed task-family specifications."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Mapping, TypeAlias

from data_pipeline.models import AnnotationStatus, EnrichedAttributeMetadata


GOVERNED_LAYER_SEMANTIC_LABELS: Mapping[
    tuple[str, str], tuple[str, ...]
] = {
    ("gebieden", "buurten"): ("neighborhood",),
    ("sport", "openbaresportplek"): ("sports location",),
    ("sport", "aanbieder"): ("sports provider",),
    ("sport", "gymzaal"): ("gymnasium",),
    ("sport", "zwembad"): ("swimming pool",),
}
RELEVANT_ATTRIBUTE_NAMES = {
    ("gebieden", "buurten"): {
        "identificatie",
        "volgnummer",
        "naam",
    },
    ("sport", "openbaresportplek"): {"id", "naam"},
    ("sport", "aanbieder"): {"id"},
    ("sport", "gymzaal"): {"id", "naam", "type"},
    ("sport", "zwembad"): {"id", "naam", "type"},
}


def data_binding_attribute_is_resolved(
    attribute: EnrichedAttributeMetadata,
) -> bool:
    """Require the complete governed annotation set for Data Binding."""

    return all(
        value.status is AnnotationStatus.RESOLVED
        for value in (
            attribute.name_en,
            attribute.description_en,
            attribute.semantic_label,
            attribute.ccd_meaning,
        )
    )


class TemporalMode(StrEnum):
    """Controlled temporal interpretation used for snapshot matching."""

    CURRENT_SNAPSHOT = "current_snapshot"
    EXPLICIT = "explicit"


@dataclass(frozen=True)
class SupportSpecification:
    """The spatial support and source-state identity requested by the task."""

    semantic_label: str
    source_state: str
    identity_fields: tuple[str, ...]


@dataclass(frozen=True)
class CountedObjectSpecification:
    """The objects and distinct source identity counted by the task."""

    semantic_label: str
    distinct_by: str


@dataclass(frozen=True)
class TaskSpecification:
    """Tool-independent semantics inferred before workflow generation."""

    required_output: str
    support: SupportSpecification
    counted_objects: CountedObjectSpecification
    constraints: tuple[str, ...]
    spatial_extent: str
    temporal_mode: TemporalMode
    temporal_meaning: str
    target_transformation: tuple[str, ...]


@dataclass(frozen=True)
class NearestPointSpecification:
    """One directional point role in a nearest-neighbour task."""

    semantic_label: str
    identity_fields: tuple[str, ...]


@dataclass(frozen=True)
class NearestDistanceSpecification:
    """Explicitly bounded distance semantics for the nearest template."""

    method: Literal["planar_euclidean", "network"]
    crs: str
    unit: str
    nearest_targets: int
    maximum_distance_m: float | None
    retain_all_ties: bool


@dataclass(frozen=True)
class NearestTaskSpecification:
    """Directional source-to-target planar nearest Task Specification."""

    required_output: str
    source_points: NearestPointSpecification
    target_points: NearestPointSpecification
    distance: NearestDistanceSpecification
    constraints: tuple[str, ...]
    spatial_extent: str
    temporal_mode: TemporalMode
    temporal_meaning: str
    target_transformation: tuple[str, ...]


CapabilityTaskSpecification: TypeAlias = (
    TaskSpecification | NearestTaskSpecification
)


class BindingRole(StrEnum):
    """Typed semantic input roles declared by the task families."""

    SUPPORTS = "supports"
    COUNTED_OBJECTS = "counted_objects"
    SOURCE_POINTS = "source_points"
    TARGET_POINTS = "target_points"


TaskFamily = Literal["count", "nearest"]


def task_family(task: CapabilityTaskSpecification) -> TaskFamily:
    """Name the task family a typed specification belongs to."""

    return "nearest" if isinstance(task, NearestTaskSpecification) else "count"


def task_roles(task: CapabilityTaskSpecification) -> tuple[BindingRole, ...]:
    """The semantic input roles a task family requires, in binding order."""

    if isinstance(task, NearestTaskSpecification):
        return (BindingRole.SOURCE_POINTS, BindingRole.TARGET_POINTS)
    return (BindingRole.SUPPORTS, BindingRole.COUNTED_OBJECTS)


def role_identity_fields(
    task: CapabilityTaskSpecification,
    role: BindingRole,
) -> tuple[str, ...]:
    """The source identity fields the task declares for one role."""

    if isinstance(task, NearestTaskSpecification):
        if role is BindingRole.SOURCE_POINTS:
            return task.source_points.identity_fields
        return task.target_points.identity_fields
    if role is BindingRole.SUPPORTS:
        return task.support.identity_fields
    return (task.counted_objects.distinct_by,)
