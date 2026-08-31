# SPDX-License-Identifier: GPL-3.0-only

"""Governed vector normalization and quality diagnostics."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, cast

import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import CRS, Transformer
from shapely import from_wkb, get_coordinates
from shapely.errors import ShapelyError
from shapely.geometry import shape
from shapely.ops import transform

from data_pipeline.errors import InvalidSnapshotError
from data_pipeline.models import (
    AttributeMetadata,
    QualityDiagnostic,
    QualityIndicators,
    TemporalExtent,
    VectorMetadata,
)
from data_pipeline.serialization import canonical_json

CANONICAL_CRS = "EPSG:28992"


@dataclass(frozen=True)
class PreparedGeoParquet:
    """Serialized data plus the technical metadata derived with it."""

    data: bytes
    spatial_extent: tuple[float, float, float, float] | None
    temporal_extent: TemporalExtent
    vector: VectorMetadata
    quality: QualityIndicators


def build_neighborhood_geoparquet(
    payload: bytes,
    *,
    original_crs: str,
    source_schema: Mapping[str, object],
    retrieved_at: datetime,
) -> PreparedGeoParquet:
    """Select valid analytical supports and reproject their geometries."""
    features = _active_features(payload, retrieved_at)
    transformer = Transformer.from_crs(
        original_crs,
        CANONICAL_CRS,
        always_xy=True,
    )
    rows: list[dict[str, object]] = []
    geometry_types: set[str] = set()
    bounds: list[tuple[float, float, float, float]] = []
    beginnings: list[datetime] = []
    endings: list[datetime | None] = []
    diagnostic_refs: dict[str, list[str]] = {
        "invalid_geometry": [],
        "topologically_invalid_geometry": [],
        "impossible_coordinate": [],
        "excluded_record": [],
    }
    for feature in features:
        properties = cast(Mapping[str, object], feature["properties"])
        record_ref = f"{properties['identificatie']}:{properties['volgnummer']}"
        try:
            source_geometry = shape(cast(dict[str, Any], feature["geometry"]))
            projected = transform(transformer.transform, source_geometry)
        except (
            ShapelyError,
            AttributeError,
            IndexError,
            KeyError,
            OverflowError,
            TypeError,
            ValueError,
        ):
            diagnostic_refs["invalid_geometry"].append(record_ref)
            diagnostic_refs["excluded_record"].append(record_ref)
            continue
        if projected.is_empty:
            diagnostic_refs["invalid_geometry"].append(record_ref)
        valid_topology = projected.is_valid
        if not valid_topology:
            diagnostic_refs["topologically_invalid_geometry"].append(record_ref)
        possible_coordinate = (
            _geometry_has_finite_coordinates(source_geometry)
            and _geometry_has_finite_coordinates(projected)
            and _geometry_is_in_crs_area(source_geometry, original_crs)
            and _geometry_is_in_crs_area(projected, CANONICAL_CRS)
        )
        if not possible_coordinate:
            diagnostic_refs["impossible_coordinate"].append(record_ref)
        if projected.is_empty or not valid_topology or not possible_coordinate:
            diagnostic_refs["excluded_record"].append(record_ref)
            continue
        row = dict(properties)
        row["geometry"] = projected.wkb
        rows.append(row)
        geometry_types.add(projected.geom_type)
        bounds.append(projected.bounds)
        beginnings.append(_required_datetime(properties["begin_geldigheid"]))
        endings.append(_optional_datetime(properties["eind_geldigheid"]))

    if not rows:
        raise InvalidSnapshotError(
            "Neighborhood snapshot has no valid analytical support."
        )
    table = pa.Table.from_pylist(rows)
    extent = (
        min(item[0] for item in bounds),
        min(item[1] for item in bounds),
        max(item[2] for item in bounds),
        max(item[3] for item in bounds),
    )
    geo_metadata = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": sorted(geometry_types),
                "crs": CRS.from_user_input(CANONICAL_CRS).to_json_dict(),
                "bbox": list(extent),
            }
        },
    }
    metadata = dict(table.schema.metadata or {})
    metadata[b"geo"] = canonical_json(geo_metadata)
    table = table.replace_schema_metadata(metadata)
    output = pa.BufferOutputStream()
    pq.write_table(table, output, compression="zstd")

    attributes = _attribute_metadata(
        source_schema,
        table,
        tuple(sorted(geometry_types)),
    )
    ordered_diagnostics = (
        "invalid_geometry",
        "topologically_invalid_geometry",
        "impossible_coordinate",
        "excluded_record",
    )
    diagnostics = tuple(
        QualityDiagnostic(
            category=category,
            count=len(diagnostic_refs[category]),
            record_refs=tuple(diagnostic_refs[category]),
        )
        for category in ordered_diagnostics
        if diagnostic_refs[category]
    )
    return PreparedGeoParquet(
        data=output.getvalue().to_pybytes(),
        spatial_extent=extent,
        temporal_extent=TemporalExtent(
            start=min(beginnings),
            end=None if any(value is None for value in endings) else max(
                cast(list[datetime], endings)
            ),
        ),
        vector=VectorMetadata(
            geometry_types=tuple(sorted(geometry_types)),
            feature_count=len(rows),
            attributes=attributes,
        ),
        quality=QualityIndicators(
            invalid_geometry_count=len(
                set(diagnostic_refs["invalid_geometry"])
                | set(diagnostic_refs["topologically_invalid_geometry"])
            ),
            diagnostics=diagnostics,
        ),
    )


def build_sports_geoparquet(
    payload: bytes,
    *,
    original_crs: str,
    source_schema: Mapping[str, object],
    retrieved_at: datetime,
    support_geoparquet: bytes,
    record_ref_prefix: str = "openbaresportplek",
) -> PreparedGeoParquet:
    """Normalize governed Sport points through the analytical extent gate.

    Only finite, valid Points covered by a normalized neighborhood support
    are stored. Every excluded source record remains auditable through its
    reason-specific diagnostics and the ``excluded_record`` category.
    """
    try:
        document = json.loads(payload)
        candidates = document["features"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise InvalidSnapshotError(
            "Sports snapshot is not a GeoJSON FeatureCollection."
        ) from error
    if document.get("type") != "FeatureCollection" or not isinstance(
        candidates, list
    ):
        raise InvalidSnapshotError(
            "Sports snapshot is not a GeoJSON FeatureCollection."
        )

    transformer = Transformer.from_crs(
        original_crs,
        CANONICAL_CRS,
        always_xy=True,
    )
    rows: list[dict[str, object]] = []
    schema_row: dict[str, object] | None = None
    geometry_types: set[str] = set()
    stored_geometry_bounds: list[tuple[float, float, float, float]] = []
    coverage_bounds: list[tuple[float, float, float, float]] = []
    diagnostic_refs: dict[str, list[str]] = {
        "null_source_identity": [],
        "null_geometry": [],
        "invalid_geometry": [],
        "boundary": [],
        "unmatched": [],
        "topologically_invalid_geometry": [],
        "impossible_coordinate": [],
        "out_of_support_coordinate": [],
        "excluded_record": [],
    }
    identity_refs: dict[bytes, list[str]] = {}
    supports = _valid_neighborhood_supports(support_geoparquet)
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise InvalidSnapshotError("Every sports feature must be an object.")
        properties = candidate.get("properties")
        geometry = candidate.get("geometry")
        if not isinstance(properties, dict):
            raise InvalidSnapshotError(
                "Every sports feature needs properties."
            )
        record_ref = _record_ref(candidate, index, record_ref_prefix)
        source_identity = properties.get("id")
        if source_identity is None:
            diagnostic_refs["null_source_identity"].append(record_ref)
        else:
            identity_refs.setdefault(
                canonical_json(source_identity),
                [],
            ).append(record_ref)
        row = dict(properties)
        if schema_row is None:
            schema_row = dict(properties)
            schema_row["geometry"] = None
        if geometry is None:
            diagnostic_refs["null_geometry"].append(record_ref)
            diagnostic_refs["excluded_record"].append(record_ref)
            continue
        if not isinstance(geometry, dict):
            diagnostic_refs["invalid_geometry"].append(record_ref)
            diagnostic_refs["excluded_record"].append(record_ref)
            continue
        if geometry.get("type") != "Point":
            diagnostic_refs["invalid_geometry"].append(record_ref)
            diagnostic_refs["excluded_record"].append(record_ref)
            continue
        try:
            source_geometry = shape(cast(dict[str, Any], geometry))
            projected = transform(transformer.transform, source_geometry)
        except (
            ShapelyError,
            AttributeError,
            IndexError,
            KeyError,
            OverflowError,
            TypeError,
            ValueError,
        ):
            diagnostic_refs["invalid_geometry"].append(record_ref)
            diagnostic_refs["excluded_record"].append(record_ref)
            continue
        if projected.geom_type != "Point" or projected.is_empty:
            diagnostic_refs["invalid_geometry"].append(record_ref)
            if not projected.is_valid:
                diagnostic_refs["topologically_invalid_geometry"].append(
                    record_ref
                )
            diagnostic_refs["excluded_record"].append(record_ref)
            continue
        if not projected.is_valid:
            diagnostic_refs["topologically_invalid_geometry"].append(record_ref)

        # Spatial classification: strictly-within points contribute to the
        # published coverage extent; boundary and outside points become
        # diagnostics. Both source and canonical CRS areas are checked because
        # an out-of-area coordinate may still project to finite garbage.
        if not (
            _point_has_finite_coordinates(source_geometry)
            and _point_has_finite_coordinates(projected)
        ):
            diagnostic_refs["impossible_coordinate"].append(record_ref)
            diagnostic_refs["unmatched"].append(record_ref)
            diagnostic_refs["excluded_record"].append(record_ref)
            continue
        if not projected.is_valid:
            diagnostic_refs["excluded_record"].append(record_ref)
            continue
        source_point = source_geometry
        include_record = False
        if not (
            _geometry_is_in_crs_area(source_point, original_crs)
            and _geometry_is_in_crs_area(projected, CANONICAL_CRS)
        ):
            diagnostic_refs["impossible_coordinate"].append(record_ref)
            diagnostic_refs["unmatched"].append(record_ref)
            diagnostic_refs["excluded_record"].append(record_ref)
        elif any(projected.within(support) for support in supports):
            coverage_bounds.append(projected.bounds)
            include_record = True
        elif any(support.covers(projected) for support in supports):
            coverage_bounds.append(projected.bounds)
            diagnostic_refs["boundary"].append(record_ref)
            diagnostic_refs["unmatched"].append(record_ref)
            include_record = True
        else:
            diagnostic_refs["out_of_support_coordinate"].append(record_ref)
            diagnostic_refs["unmatched"].append(record_ref)
            diagnostic_refs["excluded_record"].append(record_ref)

        if not include_record:
            continue
        row["geometry"] = projected.wkb
        rows.append(row)
        geometry_types.add(projected.geom_type)
        stored_geometry_bounds.append(projected.bounds)

    if not candidates:
        raise InvalidSnapshotError("Sports snapshot contains no records.")
    if rows:
        table = pa.Table.from_pylist(rows)
    else:
        assert schema_row is not None
        table = pa.Table.from_pylist([schema_row]).slice(0, 0)
    # An all-null geometry column infers as the null type; force binary so
    # the file still reads as WKB-encoded GeoParquet.
    geometry_index = table.schema.get_field_index("geometry")
    if pa.types.is_null(table.schema.field(geometry_index).type):
        table = table.set_column(
            geometry_index,
            "geometry",
            pa.array([None] * len(rows), type=pa.binary()),
        )
    stored_geometry_extent = None if not stored_geometry_bounds else (
        min(item[0] for item in stored_geometry_bounds),
        min(item[1] for item in stored_geometry_bounds),
        max(item[2] for item in stored_geometry_bounds),
        max(item[3] for item in stored_geometry_bounds),
    )
    coverage_extent = None if not coverage_bounds else (
        min(item[0] for item in coverage_bounds),
        min(item[1] for item in coverage_bounds),
        max(item[2] for item in coverage_bounds),
        max(item[3] for item in coverage_bounds),
    )
    geometry_metadata: dict[str, object] = {
        "encoding": "WKB",
        "geometry_types": sorted(geometry_types),
        "crs": CRS.from_user_input(CANONICAL_CRS).to_json_dict(),
    }
    if stored_geometry_extent is not None:
        geometry_metadata["bbox"] = list(stored_geometry_extent)
    geo_metadata = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {"geometry": geometry_metadata},
    }
    metadata = dict(table.schema.metadata or {})
    metadata[b"geo"] = canonical_json(geo_metadata)
    table = table.replace_schema_metadata(metadata)
    output = pa.BufferOutputStream()
    pq.write_table(table, output, compression="zstd")
    duplicate_refs = tuple(
        record_ref
        for refs in identity_refs.values()
        if len(refs) > 1
        for record_ref in refs
    )
    ordered_diagnostics = (
        "null_source_identity",
        "null_geometry",
        "invalid_geometry",
        "duplicate_source_identity",
        "boundary",
        "unmatched",
        "topologically_invalid_geometry",
        "impossible_coordinate",
        "out_of_support_coordinate",
        "excluded_record",
    )
    if duplicate_refs:
        diagnostic_refs["duplicate_source_identity"] = list(duplicate_refs)
    diagnostics = tuple(
        QualityDiagnostic(
            category=category,
            count=len(diagnostic_refs[category]),
            record_refs=tuple(diagnostic_refs[category]),
        )
        for category in ordered_diagnostics
        if diagnostic_refs.get(category)
    )
    return PreparedGeoParquet(
        data=output.getvalue().to_pybytes(),
        # The published extent is the coverage extent (points within or on
        # supports), not the raw bbox of every stored geometry.
        spatial_extent=coverage_extent,
        temporal_extent=TemporalExtent(start=retrieved_at, end=None),
        vector=VectorMetadata(
            geometry_types=tuple(sorted(geometry_types)),
            feature_count=len(rows),
            attributes=_attribute_metadata(
                source_schema,
                table,
                tuple(sorted(geometry_types)),
            ),
        ),
        quality=QualityIndicators(
            invalid_geometry_count=len(
                set(diagnostic_refs["invalid_geometry"])
                | set(diagnostic_refs["topologically_invalid_geometry"])
            ),
            diagnostics=diagnostics,
        ),
    )


def _record_ref(
    feature: Mapping[str, object],
    index: int,
    prefix: str,
) -> str:
    feature_id = feature.get("id")
    if feature_id is not None:
        return str(feature_id)
    properties = feature.get("properties")
    if isinstance(properties, Mapping) and properties.get("id") is not None:
        return f"{prefix}.{properties['id']}"
    fallback = f"feature:{index}"
    if prefix == "openbaresportplek":
        return fallback
    return f"{prefix}.{fallback}"


def _valid_neighborhood_supports(geoparquet: bytes) -> tuple[Any, ...]:
    table = pq.read_table(pa.BufferReader(geoparquet), columns=["geometry"])
    metadata = table.schema.metadata or {}
    try:
        geo = json.loads(metadata[b"geo"])
        primary_column = str(geo["primary_column"])
        column = cast(dict[str, object], geo["columns"][primary_column])
        support_crs = CRS.from_json_dict(
            cast(dict[str, object], column["crs"])
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidSnapshotError(
            "Neighborhood support has no usable GeoParquet CRS metadata."
        ) from error
    if support_crs != CRS.from_user_input(CANONICAL_CRS):
        raise InvalidSnapshotError(
            "Neighborhood support is not normalized to EPSG:28992."
        )
    geometries = [
        from_wkb(value)
        for value in table.column("geometry").to_pylist()
        if value is not None
    ]
    valid = [geometry for geometry in geometries if geometry.is_valid]
    if not valid:
        raise InvalidSnapshotError(
            "Neighborhood snapshot has no valid analytical support."
        )
    return tuple(valid)


def _point_has_finite_coordinates(point: Any) -> bool:
    return math.isfinite(point.x) and math.isfinite(point.y)


def _geometry_has_finite_coordinates(geometry: Any) -> bool:
    return all(
        math.isfinite(float(value))
        for coordinate in get_coordinates(geometry)
        for value in coordinate[:2]
    )


def _geometry_is_in_crs_area(geometry: Any, crs_value: str) -> bool:
    crs = CRS.from_user_input(crs_value)
    area = crs.area_of_use
    geodetic = crs.geodetic_crs
    if area is None or geodetic is None:
        return True
    try:
        geodetic_geometry = transform(
            Transformer.from_crs(crs, geodetic, always_xy=True).transform,
            geometry,
        )
    except (ShapelyError, TypeError, ValueError):
        return False
    return all(
        math.isfinite(float(longitude))
        and math.isfinite(float(latitude))
        and area.west <= longitude <= area.east
        and area.south <= latitude <= area.north
        for longitude, latitude, *_ in get_coordinates(geodetic_geometry)
    )

def _active_features(
    payload: bytes,
    retrieved_at: datetime,
) -> tuple[dict[str, object], ...]:
    """Select neighborhood features active at retrieval time.

    Unverifiable validity, missing geometry, or missing identity on an
    active feature fails the whole snapshot rather than dropping rows.
    """

    try:
        document = json.loads(payload)
        candidates = document["features"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise InvalidSnapshotError(
            "Snapshot is not a GeoJSON FeatureCollection."
        ) from error
    if document.get("type") != "FeatureCollection" or not isinstance(
        candidates, list
    ):
        raise InvalidSnapshotError("Snapshot is not a GeoJSON FeatureCollection.")

    active: list[dict[str, object]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise InvalidSnapshotError("Every GeoJSON feature must be an object.")
        properties = candidate.get("properties")
        if not isinstance(properties, dict):
            raise InvalidSnapshotError("Every feature needs properties.")
        missing_validity = {
            field
            for field in ("begin_geldigheid", "eind_geldigheid")
            if field not in properties
        }
        if missing_validity:
            raise InvalidSnapshotError(
                "Neighborhood validity is unverifiable; missing: "
                + ", ".join(sorted(missing_validity))
            )
        beginning = _required_datetime(properties["begin_geldigheid"])
        ending = _optional_datetime(properties["eind_geldigheid"])
        if not (
            beginning <= retrieved_at
            and (ending is None or retrieved_at < ending)
        ):
            continue
        geometry = candidate.get("geometry")
        if not isinstance(geometry, dict):
            raise InvalidSnapshotError("Every active feature needs geometry.")
        missing_identity = {
            field
            for field in ("identificatie", "volgnummer")
            if properties.get(field) is None
        }
        if missing_identity:
            raise InvalidSnapshotError(
                "Active neighborhood is missing source identity: "
                + ", ".join(sorted(missing_identity))
            )
        active.append(candidate)
    if not active:
        raise InvalidSnapshotError("Snapshot contains no active neighborhoods.")
    return tuple(active)


def _required_datetime(value: object) -> datetime:
    parsed = _optional_datetime(value)
    if parsed is None:
        raise InvalidSnapshotError("Neighborhood validity start is required.")
    return parsed


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidSnapshotError("Validity timestamps must be ISO-8601 strings.")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidSnapshotError(f"Invalid validity timestamp: {value}") from error


def _attribute_metadata(
    source_schema: Mapping[str, object],
    table: pa.Table,
    geometry_types: tuple[str, ...],
) -> tuple[AttributeMetadata, ...]:
    """Pair each stored column with its source schema type and sample values."""

    schema = source_schema.get("schema")
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    attributes: list[AttributeMetadata] = []
    for field in table.schema:
        source = properties.get(field.name, {})
        if field.name == "geometry":
            # The Dutch source schema names the geometry column "geometrie".
            source = properties.get("geometrie", source)
            samples: tuple[object, ...] = geometry_types
        else:
            samples = _sample_values(table.column(field.name).to_pylist())
        attributes.append(
            AttributeMetadata(
                name=field.name,
                source_type=_source_type(source),
                storage_type=str(field.type),
                unit=_source_unit(source),
                sample_values=samples,
            )
        )
    return tuple(attributes)


def _source_type(source: object) -> str:
    if not isinstance(source, dict):
        return "unknown"
    if "$ref" in source:
        return str(source["$ref"])
    source_type = str(source.get("type", "unknown"))
    source_format = source.get("format")
    return f"{source_type}:{source_format}" if source_format else source_type


def _source_unit(source: object) -> str | None:
    if not isinstance(source, dict) or source.get("unit") is None:
        return None
    return str(source["unit"])


def _sample_values(values: list[object]) -> tuple[object, ...]:
    # Up to five distinct non-null values, deduplicated by canonical JSON.
    samples: list[object] = []
    identities: set[bytes] = set()
    for value in values:
        if value is None:
            continue
        identity = canonical_json(value)
        if identity in identities:
            continue
        identities.add(identity)
        samples.append(value)
        if len(samples) == 5:
            break
    return tuple(samples)
