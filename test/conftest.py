# SPDX-License-Identifier: GPL-3.0-only

"""Shared fixtures: a synthetic in-memory catalog and a scripted planner."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pyproj import CRS
from shapely import to_wkb
from shapely.geometry import Point, Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_pipeline.catalog import CatalogPublisher  # noqa: E402
from data_pipeline.geoparquet import CANONICAL_CRS  # noqa: E402
from data_pipeline.models import (  # noqa: E402
    AcquisitionProvenance,
    AnnotationProvenance,
    AnnotationStatus,
    AttributeMetadata,
    CatalogLayer,
    EligibilityDecision,
    EnrichedAttributeMetadata,
    EnrichedLayerMetadata,
    QualityIndicators,
    RawAccessMetadata,
    RawLayerMetadata,
    SemanticAnnotation,
    TemporalExtent,
    VectorMetadata,
)
from data_pipeline.serialization import canonical_json, sha256  # noqa: E402
from data_pipeline.storage import InMemoryObjectStore  # noqa: E402
from geoqa_agent.structured_artifacts import (  # noqa: E402
    ArtifactContract,
    ArtifactProvenance,
    ArtifactRole,
    RoleSettings,
    StructuredArtifact,
)

RETRIEVED_AT = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
ABSTR = "https://quangis.github.io/tool/abstract#"

# Three neighbourhood polygons: A contains two pools, B one, C none.
SUPPORT_POLYGONS = {
    ("B1", 1): Polygon([(0, 0), (100, 0), (100, 100), (0, 100)]),
    ("B2", 1): Polygon([(100, 0), (200, 0), (200, 100), (100, 100)]),
    ("B3", 2): Polygon([(200, 0), (300, 0), (300, 100), (200, 100)]),
}
POOL_POINTS = {
    "p1": Point(10, 10),
    "p2": Point(20, 80),
    "p3": Point(150, 50),
    "p4": Point(150, 150),  # outside every support
}
# Nearest targets for the pools; h1 and h2 are exactly equidistant from p1.
HALL_POINTS = {
    "h1": Point(0, 10),
    "h2": Point(20, 10),
    "h3": Point(160, 50),
}


def geoparquet_bytes(
    columns: dict[str, list[object]],
    geometries: list[object],
) -> bytes:
    table = pa.table(
        {**columns, "geometry": [to_wkb(geometry) for geometry in geometries]}
    )
    geo_metadata = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": sorted(
                    {geometry.geom_type for geometry in geometries}
                ),
                "crs": CRS.from_user_input(CANONICAL_CRS).to_json_dict(),
            }
        },
    }
    table = table.replace_schema_metadata({b"geo": canonical_json(geo_metadata)})
    output = pa.BufferOutputStream()
    pq.write_table(table, output, compression="zstd")
    return output.getvalue().to_pybytes()


def annotation(value: str) -> SemanticAnnotation:
    return SemanticAnnotation(
        value=value,
        source="llm",
        evidence_refs=("raw.name",),
        confidence=0.9,
        version="v6",
        status=AnnotationStatus.RESOLVED,
    )


def attribute(name: str, ccd: str) -> EnrichedAttributeMetadata:
    return EnrichedAttributeMetadata(
        name=name,
        name_en=annotation(name),
        description_en=annotation(f"{name} description"),
        semantic_label=annotation(name),
        ccd_meaning=annotation(ccd),
    )


def make_layer(
    storage: InMemoryObjectStore,
    dataset_id: str,
    layer_id: str,
    *,
    label: str,
    ccd_meaning: str,
    geometry: tuple[str, ...],
    identity: tuple[str, ...],
    attrs: list[EnrichedAttributeMetadata],
    data: bytes,
) -> CatalogLayer:
    digest = sha256(data)
    key = f"datasets/{dataset_id}/{layer_id}/{digest}.parquet"
    storage.put_immutable(key, data)
    return CatalogLayer(
        dataset_id=dataset_id,
        layer_id=layer_id,
        dataset_version=digest,
        content_hash=f"sha256:{digest}",
        storage_path=storage.uri(key),
        format="GeoParquet",
        crs=CANONICAL_CRS,
        original_crs="EPSG:4326",
        spatial_extent=(0.0, 0.0, 300.0, 200.0),
        temporal_extent=TemporalExtent(start=RETRIEVED_AT, end=None),
        source_identity_fields=identity,
        raw=RawLayerMetadata(
            name=layer_id,
            description=None,
            schema={},
            access=RawAccessMetadata(
                dataset="OPENBAAR", feature_type="OPENBAAR", reuse_license=None
            ),
            provenance=AcquisitionProvenance(
                endpoint="https://example.test",
                dataset_version="v1",
                wfs_version="2.0.0",
                feature_type=f"app:{layer_id}",
                query={},
                retrieved_at=RETRIEVED_AT,
                source_content_hash="sha256:s",
                dataset_schema_content_hash="sha256:d",
                feature_schema_content_hash="sha256:f",
                api_key_required=False,
                page_count=1,
            ),
        ),
        enriched=EnrichedLayerMetadata(
            name_en=annotation(layer_id),
            description_en=annotation(f"{layer_id} layer"),
            semantic_label=annotation(label),
            ccd_meaning=annotation(ccd_meaning),
            attributes=tuple(attrs),
            provenance=AnnotationProvenance(
                provider="openai",
                model="m",
                role_settings={},
                prompt_version="v6",
                schema_version="s",
            ),
        ),
        eligibility=EligibilityDecision(
            dataset_access="OPENBAAR",
            feature_type_access="OPENBAAR",
            policy_basis="ADR-0005:mvp-two-level-public-access",
        ),
        vector=VectorMetadata(
            geometry_types=geometry,
            feature_count=len(SUPPORT_POLYGONS),
            attributes=tuple(
                AttributeMetadata(
                    name=item.name,
                    source_type="string",
                    storage_type="string",
                    unit=None,
                    sample_values=("x",),
                )
                for item in attrs
            ),
        ),
        quality=QualityIndicators(invalid_geometry_count=0, diagnostics=()),
    )


@pytest.fixture()
def storage() -> InMemoryObjectStore:
    return InMemoryObjectStore()


@pytest.fixture()
def catalog_version(storage: InMemoryObjectStore) -> str:
    supports_data = geoparquet_bytes(
        {
            "identificatie": [key[0] for key in SUPPORT_POLYGONS],
            "volgnummer": [key[1] for key in SUPPORT_POLYGONS],
            "naam": [f"Buurt {key[0]}" for key in SUPPORT_POLYGONS],
        },
        list(SUPPORT_POLYGONS.values()),
    )
    pools_data = geoparquet_bytes(
        {"id": list(POOL_POINTS), "naam": [f"Pool {k}" for k in POOL_POINTS]},
        list(POOL_POINTS.values()),
    )
    supports = make_layer(
        storage,
        "gebieden",
        "buurten",
        label="neighborhood",
        ccd_meaning="LatticeDS",
        geometry=("Polygon",),
        identity=("identificatie", "volgnummer"),
        attrs=[
            attribute("identificatie", "NominalA"),
            attribute("volgnummer", "NominalA"),
            attribute("naam", "NominalA"),
        ],
        data=supports_data,
    )
    pools = make_layer(
        storage,
        "sport",
        "zwembad",
        label="swimming pool",
        ccd_meaning="ObjectDS",
        geometry=("Point",),
        identity=("id",),
        attrs=[attribute("id", "NominalA"), attribute("naam", "NominalA")],
        data=pools_data,
    )
    halls_data = geoparquet_bytes(
        {"id": list(HALL_POINTS), "naam": [f"Hall {k}" for k in HALL_POINTS]},
        list(HALL_POINTS.values()),
    )
    halls = make_layer(
        storage,
        "sport",
        "sporthal",
        label="sports hall",
        ccd_meaning="ObjectDS",
        geometry=("Point",),
        identity=("id",),
        attrs=[attribute("id", "NominalA"), attribute("naam", "NominalA")],
        data=halls_data,
    )
    return CatalogPublisher(storage).publish_snapshot((supports, pools, halls))


class ScriptedClient:
    """Replays fixed interpretation/planning artifacts as if from the LLM.

    ``planning`` may be a single artifact or a sequence played in order
    (the last one repeats), so tests can script failed attempts before a
    success.
    """

    def __init__(self, interpretation: dict, planning: dict | list[dict]) -> None:
        self.interpretation = interpretation
        self._planning = (
            list(planning) if isinstance(planning, list) else [planning]
        )
        self.planning_inputs: list[str] = []

    def generate(self, request):  # noqa: ANN001 - StructuredArtifactClient protocol
        if request.contract is ArtifactContract.QUESTION_INTERPRETATION:
            data = self.interpretation
        else:
            self.planning_inputs.append(request.input_text)
            data = (
                self._planning[0]
                if len(self._planning) == 1
                else self._planning.pop(0)
            )
        return StructuredArtifact(
            data=data,
            provenance=ArtifactProvenance(
                provider="openai",
                model="gpt-5.4-mini-2026-03-17",
                role=ArtifactRole.PLANNING,
                settings=RoleSettings(reasoning_effort="low", max_output_tokens=8192),
                prompt_version="test",
                schema_version="test",
            ),
        )


COUNT_INTERPRETATION = {
    "question_phrases": [
        {
            "text": "neighborhoods",
            "normalized_meaning": "neighborhood units",
            "functional_role": "support",
            "referenced_phenomenon": "neighborhood",
            "referenced_property": None,
            "referenced_relation": None,
            "referenced_place": "Amsterdam",
            "referenced_time": None,
            "quantity": None,
            "unit": None,
            "candidate_ccd_meaning": "LatticeDS",
            "confidence": 0.9,
            "alternatives": [],
        },
    ],
    "task_specification": {
        "required_output": "neighborhoods with zero swimming pools",
        "support": {
            "semantic_label": "neighborhood",
            "source_state": "active at the pinned snapshot",
            "identity_fields": ["identificatie", "volgnummer"],
        },
        "counted_objects": {"semantic_label": "swimming pool", "distinct_by": "id"},
        "constraints": ["strict point-within-polygon"],
        "spatial_extent": "Amsterdam",
        "temporal_mode": "current_snapshot",
        "temporal_meaning": "the pinned snapshot",
        "target_transformation": [
            "count pools per neighborhood",
            "select zero counts",
        ],
    },
    "role_requirements": [
        {
            "role": "supports",
            "semantic_label": "neighborhood",
            "ccd_meaning": "LatticeDS",
            "geometry_types": ["Polygon"],
            "source_identity_fields": ["identificatie", "volgnummer"],
        },
        {
            "role": "counted_objects",
            "semantic_label": "swimming pool",
            "ccd_meaning": "ObjectDS",
            "geometry_types": ["Point"],
            "source_identity_fields": ["id"],
        },
    ],
    "assumptions": ["'pools' normalized to the swimming pool layer"],
    "unresolved_ambiguities": [],
}

COUNT_PLAN = {
    "abstract_workflow": {
        "steps": [
            {
                "step_id": "count-pools-by-neighborhood",
                "abstraction_id": f"{ABSTR}SpatialJoinCountTess",
                "input_refs": ["counted_objects", "supports"],
                "output_ref": "support_counts",
            },
            {
                "step_id": "select-zero-count",
                "abstraction_id": f"{ABSTR}SelectLayerByRatioTessObjects",
                "input_refs": ["support_counts"],
                "output_ref": "zero_supports",
            },
        ],
        "final_output_ref": "zero_supports",
    },
    "concrete_workflow": {
        "steps": [
            {
                "step_id": "join-points-within",
                "abstract_step_id": "count-pools-by-neighborhood",
                "algorithm_id": "geopandas:sjoin",
                "parameters": [
                    {"name": "input", "source": "ref", "value": "counted_objects"},
                    {"name": "predicate", "source": "literal", "value": "within"},
                    {"name": "join", "source": "ref", "value": "supports"},
                    {"name": "method", "source": "literal", "value": "one_to_many"},
                    {
                        "name": "discard_nonmatching",
                        "source": "literal",
                        "value": True,
                    },
                    {"name": "prefix", "source": "literal", "value": "support_"},
                ],
                "outputs": [
                    {"name": "output", "ref": "points_within", "kind": "sink"},
                    {"name": "non_matching", "ref": "unmatched_points", "kind": "sink"},
                    {"name": "joined_count", "ref": "joined_count", "kind": "result"},
                ],
            },
            {
                "step_id": "count-per-support",
                "abstract_step_id": "count-pools-by-neighborhood",
                "algorithm_id": "geopandas:countpointsinpolygon",
                "parameters": [
                    {"name": "polygons", "source": "ref", "value": "supports"},
                    {"name": "points", "source": "ref", "value": "points_within"},
                    {"name": "class_field", "source": "literal", "value": "id"},
                    {"name": "field", "source": "literal", "value": "object_count"},
                ],
                "outputs": [
                    {"name": "output", "ref": "support_counts", "kind": "sink"},
                ],
            },
            {
                "step_id": "select-zero",
                "abstract_step_id": "select-zero-count",
                "algorithm_id": "geopandas:filterbyexpression",
                "parameters": [
                    {"name": "input", "source": "ref", "value": "support_counts"},
                    {
                        "name": "expression",
                        "source": "literal",
                        "value": "object_count == 0",
                    },
                ],
                "outputs": [
                    {"name": "output", "ref": "zero_supports", "kind": "sink"},
                ],
            },
        ],
        "final_output_ref": "zero_supports",
        "result_table_ref": "support_counts",
        "diagnostic_refs": ["unmatched_points"],
    },
}

NEAREST_INTERPRETATION = {
    "question_phrases": [
        {
            "text": "nearest sports hall",
            "normalized_meaning": "nearest sports hall",
            "functional_role": "condition",
            "referenced_phenomenon": "sports hall",
            "referenced_property": None,
            "referenced_relation": "nearest",
            "referenced_place": None,
            "referenced_time": None,
            "quantity": None,
            "unit": None,
            "candidate_ccd_meaning": "ObjectDS",
            "confidence": 0.9,
            "alternatives": [],
        },
    ],
    "task_specification": {
        "required_output": "each pool and its nearest sports hall",
        "source_points": {"semantic_label": "swimming pool", "identity_fields": ["id"]},
        "target_points": {"semantic_label": "sports hall", "identity_fields": ["id"]},
        "distance": {
            "method": "planar_euclidean",
            "crs": "EPSG:28992",
            "unit": "metre",
            "nearest_targets": 1,
            "maximum_distance_m": None,
            "retain_all_ties": True,
        },
        "constraints": [],
        "spatial_extent": "Amsterdam",
        "temporal_mode": "current_snapshot",
        "temporal_meaning": "the pinned snapshot",
        "target_transformation": ["find nearest target per source"],
    },
    "role_requirements": [
        {
            "role": "source_points",
            "semantic_label": "swimming pool",
            "ccd_meaning": "ObjectDS",
            "geometry_types": ["Point"],
            "source_identity_fields": ["id"],
        },
        {
            "role": "target_points",
            "semantic_label": "sports hall",
            "ccd_meaning": "ObjectDS",
            "geometry_types": ["Point"],
            "source_identity_fields": ["id"],
        },
    ],
    "assumptions": [],
    "unresolved_ambiguities": [],
}

NEAREST_PLAN = {
    "abstract_workflow": {
        "steps": [
            {
                "step_id": "find-nearest",
                "abstraction_id": f"{ABSTR}NearPointObjects",
                "input_refs": ["source_points", "target_points"],
                "output_ref": "nearest_pairs",
            },
        ],
        "final_output_ref": "nearest_pairs",
    },
    "concrete_workflow": {
        "steps": [
            {
                "step_id": "join-nearest",
                "abstract_step_id": "find-nearest",
                "algorithm_id": "geopandas:sjoinnearest",
                "parameters": [
                    {"name": "input", "source": "ref", "value": "source_points"},
                    {"name": "target", "source": "ref", "value": "target_points"},
                    {"name": "fields_to_copy", "source": "literal", "value": "id"},
                    {
                        "name": "discard_nonmatching",
                        "source": "literal",
                        "value": False,
                    },
                    {"name": "prefix", "source": "literal", "value": "target_"},
                    {"name": "neighbors", "source": "literal", "value": 1},
                    {
                        "name": "distance_field",
                        "source": "literal",
                        "value": "distance_m",
                    },
                ],
                "outputs": [
                    {"name": "output", "ref": "nearest_join", "kind": "sink"},
                    {"name": "non_matching", "ref": "unmatched_sources", "kind": "sink"},
                    {"name": "joined_count", "ref": "joined", "kind": "result"},
                    {"name": "unjoinable_count", "ref": "unjoined", "kind": "result"},
                ],
            },
            {
                "step_id": "name-source-identity",
                "abstract_step_id": "find-nearest",
                "algorithm_id": "geopandas:renamefield",
                "parameters": [
                    {"name": "input", "source": "ref", "value": "nearest_join"},
                    {"name": "field", "source": "literal", "value": "id"},
                    {"name": "new_name", "source": "literal", "value": "source_id"},
                ],
                "outputs": [
                    {"name": "output", "ref": "nearest_pairs", "kind": "sink"},
                ],
            },
        ],
        "final_output_ref": "nearest_pairs",
        "result_table_ref": "nearest_pairs",
        "diagnostic_refs": ["unmatched_sources"],
    },
}
