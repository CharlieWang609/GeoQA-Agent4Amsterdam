# SPDX-License-Identifier: GPL-3.0-only

"""Browser-safe spatial presentation for an owned Candidate Answer."""

from __future__ import annotations

from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
from shapely import from_wkb
from shapely.geometry import mapping
from shapely.ops import transform

from app.api.catalog_layers import DISPLAY_CRS, display_transformer
from app.api.session_models import QuestionSession
from data_pipeline.storage import ObjectStore
from geoqa_agent.candidate_answer import CandidateAnswerValue


# The first of these columns in the result table names a feature for
# display; identity columns stay the key.
NAME_FIELDS = ("naam", "name", "buurtnaam", "wijknaam", "naam_aanbieder", "naam_sportfaciliteit")


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
    return _build_answer_map(storage, answer=answer, result_key=key)


def _build_answer_map(
    storage: ObjectStore,
    *,
    answer: CandidateAnswerValue,
    result_key: str,
) -> dict[str, object]:
    """Project the result table to WGS84 GeoJSON: one feature per distinct
    geometry-role identity, carrying the answer value and whether any of
    its rows belongs to the final-output subset."""

    stored = storage.read(result_key)
    assert stored is not None
    key_columns = [item.column for item in answer.answer_map.key_columns]
    geometry_columns = [
        item.column
        for item in answer.answer_map.key_columns
        if item.role == answer.answer_map.geometry_role
    ]
    value_field = answer.answer_map.value_field
    try:
        parquet = pq.ParquetFile(pa.BufferReader(stored.data))
        available = set(parquet.schema_arrow.names)
        name_field = next(
            (field for field in NAME_FIELDS if field in available and field not in key_columns),
            None,
        )
        table = parquet.read(
            columns=[*key_columns, *([] if name_field is None else [name_field]), value_field, "geometry"],
            use_threads=False,
        )
    except (pa.ArrowException, OSError) as error:
        raise AnswerMapUnavailableError(
            "The Candidate Answer result-table geometry cannot be read."
        ) from error
    selected_keys = set(answer.selected_keys)
    project = display_transformer(answer.answer_map.crs).transform
    features: dict[tuple[str, ...], dict[str, Any]] = {}
    try:
        for row in table.to_pylist():
            keys = tuple(str(row[name]) for name in key_columns)
            identity = tuple(str(row[name]) for name in geometry_columns)
            value = float(row[value_field])
            is_selected = keys in selected_keys
            feature = features.get(identity)
            if feature is None:
                geometry = transform(project, from_wkb(cast(bytes, row["geometry"])))
                features[identity] = {
                    "type": "Feature",
                    "geometry": mapping(geometry),
                    "properties": {
                        "identity": dict(zip(geometry_columns, identity)),
                        "name": (
                            None
                            if name_field is None or row[name_field] is None
                            else str(row[name_field])
                        ),
                        "value_field": value_field,
                        "value": value,
                        "is_selected": is_selected,
                    },
                }
                continue
            properties = feature["properties"]
            properties["value"] = min(properties["value"], value)
            properties["is_selected"] = properties["is_selected"] or is_selected
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
        "value_field": value_field,
        "value_scale": answer.answer_map.value_scale,
        "features": [features[key] for key in sorted(features)],
    }
