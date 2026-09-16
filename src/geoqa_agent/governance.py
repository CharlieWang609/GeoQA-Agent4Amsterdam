# SPDX-License-Identifier: GPL-3.0-only

"""Governed catalog vocabulary and the task specification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


from data_pipeline.models import AnnotationStatus, EnrichedAttributeMetadata


GOVERNED_LAYER_SEMANTIC_LABELS: Mapping[
    tuple[str, str], tuple[str, ...]
] = {
    ("gebieden", "buurten"): ("neighborhood",),
    ("gebieden", "wijken"): ("district",),
    ("gebieden", "stadsdelen"): ("borough",),
    ("sport", "openbaresportplek"): ("sports location",),
    ("sport", "aanbieder"): ("sports provider",),
    ("sport", "gymzaal"): ("gymnasium",),
    ("sport", "zwembad"): ("swimming pool",),
    ("sport", "hal"): ("sports hall",),
    ("sport", "park"): ("sports park",),
    ("sport", "veld"): ("sports field",),
    ("sport", "hardlooproute"): ("running route",),
    ("huishoudelijkafval", "container"): ("waste container",),
    ("huishoudelijkafval", "cluster"): ("waste container cluster",),
    ("bouwstroompunten", "bouwstroompunten"): ("construction power point",),
    ("touringcars", "haltes"): ("coach stop",),
    ("varen", "opafstapplaats"): ("boat boarding point",),
    ("varen", "ligplaats"): ("mooring",),
    ("ecologie", "faunavoorzieningen"): ("fauna facility",),
    ("fietspaaltjes", "fietspaaltjes"): ("bicycle bollard",),
    ("verkeersinformatiesystemen", "verkeersinformatiesystemen"): (
        "traffic information system",
    ),
    ("winkelgebieden", "winkelgebieden"): ("shopping area",),
    ("ahn", "dtm"): ("terrain elevation",),
    ("ahn", "dsm"): ("surface elevation",),
    ("worldcover", "landcover"): ("land cover",),
    ("gee", "sentinel2"): ("satellite imagery",),
    ("gee", "dynamicworld"): ("land cover probability",),
}
RELEVANT_ATTRIBUTE_NAMES = {
    ("gebieden", "buurten"): {
        "identificatie",
        "volgnummer",
        "naam",
    },
    ("gebieden", "wijken"): {"identificatie", "volgnummer", "naam"},
    ("gebieden", "stadsdelen"): {"identificatie", "volgnummer", "naam"},
    ("sport", "openbaresportplek"): {"id", "naam"},
    ("sport", "aanbieder"): {"id"},
    ("sport", "gymzaal"): {"id", "naam", "type"},
    ("sport", "zwembad"): {"id", "naam", "type"},
    ("sport", "hal"): {"id", "naam", "type"},
    ("sport", "park"): {"id", "omschrijving", "objectsubtype"},
    ("sport", "veld"): {"id", "sportfunctie", "soort_ondergrond"},
    ("sport", "hardlooproute"): {"id", "naam", "categorie"},
    ("huishoudelijkafval", "container"): {"id", "type", "fractie_omschrijving"},
    ("huishoudelijkafval", "cluster"): {"id", "status"},
    ("bouwstroompunten", "bouwstroompunten"): {"id", "locatie", "capaciteit"},
    ("touringcars", "haltes"): {"id", "omschrijving", "plaatsen"},
    ("varen", "opafstapplaats"): {"id", "tekst_on_mouseover", "op_en_afstap"},
    ("varen", "ligplaats"): {"id", "naam_vaartuig", "ligplaats_segment"},
    ("ecologie", "faunavoorzieningen"): {"id", "objectnaam", "type"},
    ("fietspaaltjes", "fietspaaltjes"): {"id", "street", "count"},
    ("verkeersinformatiesystemen", "verkeersinformatiesystemen"): {
        "id",
        "object_soort",
        "type",
    },
    ("winkelgebieden", "winkelgebieden"): {"id", "gebiedsnaam", "categorienaam"},
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
            attribute.measurement_scale,
        )
    )


class TemporalMode(StrEnum):
    """Controlled temporal interpretation used for snapshot matching."""

    CURRENT_SNAPSHOT = "current_snapshot"
    EXPLICIT = "explicit"


@dataclass(frozen=True)
class RoleSpecification:
    """One semantic input the question needs, grounded to exactly one layer.

    The role name is the binding ref the workflow refers to; the rest is
    what a Catalog Layer must satisfy to ground it. Raster roles carry no
    geometry types and no identity fields: they never key an answer row.
    """

    role: str
    semantic_label: str
    identity_fields: tuple[str, ...]
    data_kind: str
    geometry_types: tuple[str, ...]


@dataclass(frozen=True)
class GoalSpecification:
    """The shape of the answer.

    ``per`` names the roles whose identities key one result row (one role:
    a value per unit; two roles: a pair table); ``value_scale`` is the
    measurement scale of the value column. ``aggregation`` says how the
    value arises (count of objects, sum/mean/min/max of ``value_attribute``,
    a density, a distance) so that "how many bollards" over records that
    each carry a bollard count is a sum, not a count.
    ``selection`` says the answer needs the whole keyed table and then a
    subset of its rows (every unit with its count, then the zero-count
    units); a condition that only limits which rows exist is not one.
    """

    per: tuple[str, ...]
    value_name: str
    value_scale: str
    aggregation: str
    value_attribute: str | None
    selection: bool


@dataclass(frozen=True)
class QuantityConstraint:
    """A number from the question that must reach the plan as a literal."""

    name: str
    value: float
    unit: str | None


@dataclass(frozen=True)
class PeriodConstraint:
    """A date range the question states for selecting observations (an
    imagery season, a year of measurements); ISO dates, end exclusive.
    It must reach the plan as literals, like a quantity."""

    name: str
    start: str
    end: str


@dataclass(frozen=True)
class TaskSpecification:
    """Tool-independent semantics inferred before workflow generation."""

    required_output: str
    roles: tuple[RoleSpecification, ...]
    goal: GoalSpecification
    quantities: tuple[QuantityConstraint, ...]
    periods: tuple[PeriodConstraint, ...]
    constraints: tuple[str, ...]
    spatial_extent: str
    temporal_mode: TemporalMode
    temporal_meaning: str
    target_transformation: tuple[str, ...]

    def role(self, name: str) -> RoleSpecification:
        for role in self.roles:
            if role.role == name:
                return role
        raise KeyError(f"Task Specification has no role named {name}.")


@dataclass(frozen=True)
class KeyColumn:
    """One result-table key column and the role identity field it carries."""

    role: str
    field: str
    column: str


@dataclass(frozen=True)
class ResultContract:
    """The result-table shape the goal implies: key columns, one value
    column, and the geometry of the first ``per`` role."""

    key_columns: tuple[KeyColumn, ...]
    value_column: str
    value_scale: str
    geometry_role: str
    selection: bool

    @property
    def columns(self) -> tuple[str, ...]:
        return (*(item.column for item in self.key_columns), self.value_column, "geometry")

    def document(self, task: TaskSpecification) -> dict[str, object]:
        """The contract as the planner sees it."""

        return {
            "result_table_ref": (
                "one row per "
                + " x ".join(task.role(role).semantic_label for role in self.per_roles)
                + f" with exactly the key columns {list(self.columns[:-2])}, the "
                f"value column '{self.value_column}' ({self.value_scale}), and the "
                f"geometry of the {task.role(self.geometry_role).semantic_label}. "
                "Layers that share column names collide in joins (id becomes "
                "id_left/id_right): set prefix on the joined side so the key "
                "columns keep their names, and name the value column via the "
                "operation's distance_field / field / output_field or rename it"
            ),
            "final_output_ref": (
                "the subset of result-table rows answering the required output, "
                "with the same columns"
                if self.selection
                else "the result table itself (or a same-shaped copy)"
            ),
            "diagnostic_refs": (
                "retained intermediate outputs explaining excluded records "
                "(for example objects that matched no unit)"
            ),
        }

    @property
    def per_roles(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.role for item in self.key_columns))


def result_contract(task: TaskSpecification) -> ResultContract:
    """Derive the result-table contract from the goal.

    A single keyed role keeps its identity field names as columns; a pair
    table prefixes each field with its role so the two sides stay apart.
    """

    per = task.goal.per
    key_columns = tuple(
        KeyColumn(
            role=role,
            field=field,
            column=field if len(per) == 1 else f"{role}_{field}",
        )
        for role in per
        for field in task.role(role).identity_fields
    )
    return ResultContract(
        key_columns=key_columns,
        value_column=task.goal.value_name,
        value_scale=task.goal.value_scale,
        geometry_role=per[0],
        selection=task.goal.selection,
    )
