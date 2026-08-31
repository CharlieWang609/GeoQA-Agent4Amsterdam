# SPDX-License-Identifier: GPL-3.0-only

"""Browser-safe spatial presentation for an owned Candidate Answer."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import Transformer
from shapely import from_wkb
from shapely.geometry import mapping
from shapely.ops import transform

from app.api.session_models import QuestionSession
from data_pipeline.storage import ObjectStore
from geoqa_agent.candidate_answer import (
    CandidateAnswerValue,
    NearestCandidateAnswer,
)


DISPLAY_CRS = "EPSG:4326"


class AnswerMapUnavailableError(ValueError):
    """The current session has no complete, readable Candidate Answer map."""


def build_answer_map(
    storage: ObjectStore,
    session: QuestionSession,
) -> dict[str, object]:
    """Return the validated result geometry in browser-display coordinates."""

    answer = session.candidate_answer
    job = session.execution_result
    if answer is None or job is None:
        raise AnswerMapUnavailableError(
            "A passing Candidate Answer is required before its Answer Map is available."
        )
    draft_id = answer.reproducibility.draft_id
    if job.draft_id != draft_id:
        raise AnswerMapUnavailableError(
            "The Candidate Answer execution and workflow provenance disagree."
        )
    executed_draft = next(
        (draft for draft in session.draft_versions if draft.draft_id == draft_id),
        None,
    )
    workflow = (
        executed_draft.concrete_workflow
        if executed_draft is not None
        else None
    )
    result_ref = workflow.get("result_table_ref") if workflow is not None else None
    if not isinstance(result_ref, str) or not result_ref:
        raise AnswerMapUnavailableError(
            "The Candidate Answer has no declared result-table geometry."
        )
    key = f"execution-jobs/{job.job_id}/outputs/{result_ref}.parquet"
    return _build_answer_map(
        storage,
        answer=answer,
        result_location=job.output_locations.get(result_ref),
        result_key=key,
    )


def _build_answer_map(
    storage: ObjectStore,
    *,
    answer: CandidateAnswerValue,
    result_location: str | None,
    result_key: str,
) -> dict[str, object]:
    """Project the pinned count layer to WGS84 GeoJSON: every support is a
    feature with its count, flagged is_selected when it belongs to the
    answer's final-output subset."""

    if result_location != storage.uri(result_key):
        raise AnswerMapUnavailableError(
            "The Candidate Answer result-table geometry is not pinned."
        )
    stored = storage.read(result_key)
    if stored is None:
        raise AnswerMapUnavailableError(
            "The Candidate Answer result-table geometry is unavailable."
        )
    if isinstance(answer, NearestCandidateAnswer):
        return _build_nearest_answer_map(answer, stored.data)
    identity_fields = answer.selected_geometry.feature_identity_fields
    count_field = answer.answer_map.count_field
    if len(identity_fields) != 2:
        raise AnswerMapUnavailableError(
            "The Candidate Answer map requires exactly two feature identity fields."
        )
    try:
        table = pq.ParquetFile(pa.BufferReader(stored.data)).read(
            columns=[*identity_fields, count_field, "geometry"],
            use_threads=False,
        )
    except (pa.ArrowException, OSError) as error:
        raise AnswerMapUnavailableError(
            "The Candidate Answer result-table geometry cannot be read."
        ) from error

    selected_identities = set(answer.selected_identities)
    project = _transformer(answer.answer_map.crs).transform
    features: list[dict[str, object]] = []
    try:
        for row in table.to_pylist():
            identity = (
                str(row[identity_fields[0]]),
                int(row[identity_fields[1]]),
            )
            count = int(row[count_field])
            geometry = transform(project, from_wkb(cast(bytes, row["geometry"])))
            features.append(
                {
                    "type": "Feature",
                    "geometry": mapping(geometry),
                    "properties": {
                        "identity": dict(zip(identity_fields, identity)),
                        "count_field": count_field,
                        "count": count,
                        "is_selected": identity in selected_identities,
                    },
                }
            )
    except (KeyError, TypeError, ValueError) as error:
        raise AnswerMapUnavailableError(
            "The Candidate Answer result-table geometry has an invalid shape."
        ) from error

    return {
        "type": "FeatureCollection",
        "candidate_answer_id": answer.candidate_answer_id,
        "title": answer.answer_map.title,
        "source_crs": answer.answer_map.crs,
        "display_crs": DISPLAY_CRS,
        "context": f"Selected-snapshot context for {answer.answer_map.title}",
        "features": features,
    }


def _build_nearest_answer_map(
    answer: NearestCandidateAnswer,
    data: bytes,
) -> dict[str, object]:
    """Project one feature per source, styled by its nearest distance."""

    identity_field = answer.answer_map.identity_field
    distance_field = answer.answer_map.distance_field
    try:
        table = pq.ParquetFile(pa.BufferReader(data)).read(
            columns=[identity_field, distance_field, "geometry"],
            use_threads=False,
        )
    except (pa.ArrowException, OSError) as error:
        raise AnswerMapUnavailableError(
            "The nearest result-table geometry cannot be read."
        ) from error

    project = _transformer(answer.answer_map.crs).transform
    by_source: dict[str, tuple[float, Any]] = {}
    try:
        for row in table.to_pylist():
            identity = str(row[identity_field])
            distance = float(row[distance_field])
            geometry = transform(project, from_wkb(cast(bytes, row["geometry"])))
            previous = by_source.get(identity)
            if previous is not None and previous[0] != distance:
                raise ValueError("Nearest ties disagree on distance.")
            by_source[identity] = distance, geometry
    except (KeyError, TypeError, ValueError) as error:
        raise AnswerMapUnavailableError(
            "The nearest result-table geometry has an invalid shape."
        ) from error

    features = [
        {
            "type": "Feature",
            "geometry": mapping(geometry),
            "properties": {
                "identity": {identity_field: identity},
                "distance_field": distance_field,
                "nearest_distance_m": distance,
            },
        }
        for identity, (distance, geometry) in sorted(by_source.items())
    ]
    return {
        "type": "FeatureCollection",
        "answer_kind": answer.answer_kind,
        "candidate_answer_id": answer.candidate_answer_id,
        "title": answer.answer_map.title,
        "source_crs": answer.answer_map.crs,
        "display_crs": DISPLAY_CRS,
        "context": f"Selected-snapshot context for {answer.answer_map.title}",
        "features": features,
    }


@lru_cache(maxsize=8)
def _transformer(source_crs: str) -> Transformer:
    return Transformer.from_crs(
        source_crs,
        DISPLAY_CRS,
        always_xy=True,
    )
