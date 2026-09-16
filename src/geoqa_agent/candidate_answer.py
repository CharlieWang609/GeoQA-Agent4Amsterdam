# SPDX-License-Identifier: GPL-3.0-only

"""Package execution outputs into the reviewable Candidate Answer."""

from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
from typing import (
    Any,
    Callable,
    Iterable,
    Literal,
    Mapping,
    NoReturn,
    cast,
)

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict
from pyproj import CRS
from shapely import from_wkb

from data_pipeline.catalog import CatalogReader
from data_pipeline.models import CatalogVersion, QualityDiagnostic
from data_pipeline.serialization import canonical_json, sha256
from data_pipeline.storage import ObjectStore
from geoqa_agent.execution import (
    ExecutionJob,
)
from geoqa_agent.governance import KeyColumn, ResultContract, result_contract
from data_pipeline.geoparquet import CANONICAL_CRS
from geoqa_agent.workflow_models import (
    WorkflowDraft,
    WorkflowDraftRepository,
)



class _GeoParquet:
    """Projected, batch-oriented access to one bounded GeoParquet artifact."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        parquet = pq.ParquetFile(pa.BufferReader(data))
        self.schema = parquet.schema_arrow
        self.column_names = frozenset(self.schema.names)
        self.num_rows = parquet.metadata.num_rows

    def rows(self, columns: tuple[str, ...]) -> Iterable[Mapping[str, object]]:
        parquet = pq.ParquetFile(pa.BufferReader(self._data))
        for batch in parquet.iter_batches(
            batch_size=1024,
            columns=list(columns),
        ):
            yield from batch.to_pylist()


class ImmutableAnswerModel(BaseModel):
    """Base for answer records: frozen and rejecting unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateResultRow(ImmutableAnswerModel):
    """One result row: its key values (in key-column order, stringified)
    and the value attributed to them."""

    keys: tuple[str, ...]
    value: float


class CandidateDiagnostic(ImmutableAnswerModel):
    category: str
    count: int
    record_refs: tuple[str, ...]


class SpatialResult(ImmutableAnswerModel):
    location: str
    media_type: str
    crs: str
    feature_identity_fields: tuple[str, ...]
    feature_count: int


class ResultKeyColumn(ImmutableAnswerModel):
    role: str
    field: str
    column: str


class AnswerMapRepresentation(ImmutableAnswerModel):
    layer_ref: str
    geometry_location: str
    feature_count: int
    crs: str
    title: str
    key_columns: tuple[ResultKeyColumn, ...]
    geometry_role: str
    value_field: str
    value_scale: str


class ReproducibilityInput(ImmutableAnswerModel):
    capability_input_ref: str
    dataset_id: str
    layer_id: str
    dataset_version: str
    content_hash: str
    source_content_hash: str
    retrieved_at: datetime


class EffectiveParameterRecord(ImmutableAnswerModel):
    step_id: str
    algorithm_id: str
    parameters: Mapping[str, object]


class ReproducibilityEnvelope(ImmutableAnswerModel):
    """Everything needed to audit or re-run the answer: pinned inputs,
    prompt/schema/tool versions, runtime identity, and effective
    parameters."""

    execution_job_id: str
    draft_id: str
    validation_id: str
    catalog_version: str
    inputs: tuple[ReproducibilityInput, ...]
    annotation_versions: tuple[str, ...]
    tool_registry_version: str
    geopandas_version: str
    shapely_version: str
    code_commit: str
    planning_provider: str
    planning_model: str
    planning_role_settings: Mapping[str, object]
    planning_prompt_version: str
    planning_schema_version: str
    task_schema_version: str
    effective_parameters: tuple[EffectiveParameterRecord, ...]


AnswerConstructionCode = Literal[
    "diagnostic-shape-mismatch",
    "output-missing",
    "invalid-geoparquet",
    "crs-mismatch",
    "result-shape-mismatch",
    "invalid-result-row",
]


class CandidateAnswer(ImmutableAnswerModel):
    """The complete reviewable answer: a keyed result table whose shape the
    goal implies (key columns of the ``per`` roles, one value column), the
    selected subset answering the required output, and its geometry."""

    answer_kind: Literal["keyed-table"] = "keyed-table"
    candidate_answer_id: str
    constructed_at: datetime
    result_table: tuple[CandidateResultRow, ...]
    selected_keys: tuple[tuple[str, ...], ...]
    selected_geometry: SpatialResult
    answer_map: AnswerMapRepresentation
    diagnostics: tuple[CandidateDiagnostic, ...]
    summary: str
    reproducibility: ReproducibilityEnvelope

    def identity_payload(self) -> bytes:
        """Return the deterministic content used by the persisted identity."""
        return canonical_json(
            self.model_dump(mode="json", exclude={"candidate_answer_id"})
        )


CandidateAnswerValue = CandidateAnswer


class AnswerConstructionDiagnostic(ImmutableAnswerModel):
    code: AnswerConstructionCode
    message: str
    ref: str | None = None


class CandidateAnswerFailure(ImmutableAnswerModel):
    status: Literal["rejected"] = "rejected"
    phase: Literal["sanity-check"] = "sanity-check"
    evaluated_at: datetime
    diagnostics: tuple[AnswerConstructionDiagnostic, ...]


class CandidateAnswerRejected(ValueError):
    """A successful execution did not satisfy Candidate Answer checks."""

    def __init__(self, failure: CandidateAnswerFailure) -> None:
        super().__init__(failure.diagnostics[0].message)
        self.failure = failure


class CandidateAnswerBuilder:
    """Package one successful execution's outputs into a reviewable answer."""

    def __init__(
        self,
        *,
        storage: ObjectStore,
        evaluated_at: Callable[[], datetime],
    ) -> None:
        self._storage = storage
        self._evaluated_at = evaluated_at

    def construct(self, job: ExecutionJob) -> CandidateAnswerValue:
        """Turn one successful execution into a Candidate Answer or reject it.

        Sanity checks cover provenance, declared outputs, and GeoParquet/CRS
        row shape against the goal's result contract; any failure raises
        CandidateAnswerRejected. Comparison against an oracle lives in the
        evaluation harness, not in this serving path.
        """

        now = self._evaluated_at()
        draft = WorkflowDraftRepository(self._storage).get(job.draft_id)
        catalog = CatalogReader(self._storage).get(draft.catalog_version)
        task = draft.task_specification.value
        contract = result_contract(task)
        workflow = draft.concrete_workflow.value
        result_ref = workflow.result_table_ref
        final_ref = workflow.final_output_ref
        rows = self._keyed_rows(
            self._output_table(job, result_ref, now), now, ref=result_ref, contract=contract
        )
        if not rows:
            self._reject(now, "result-shape-mismatch", f"Output {result_ref} has no rows.", result_ref)
        selected = (
            rows
            if final_ref == result_ref
            else self._keyed_rows(
                self._output_table(job, final_ref, now), now, ref=final_ref, contract=contract
            )
        )
        # The final output must be a coherent subset of the result table.
        values_by_keys = dict(rows)
        for keys, value in selected:
            if values_by_keys.get(keys) != value:
                self._reject(
                    now,
                    "result-shape-mismatch",
                    f"Final output row is not part of the result table: {keys!r}.",
                    final_ref,
                )
        selected_keys = tuple(sorted(keys for keys, _ in selected))
        bindings = {
            binding.capability_input_ref: binding for binding in draft.data_bindings.value
        }
        layers = {
            role: catalog.layer(binding.dataset_id, binding.layer_id)
            for role, binding in bindings.items()
        }
        diagnostics = _candidate_diagnostics(
            tuple(
                diagnostic
                for layer in layers.values()
                for diagnostic in layer.quality.diagnostics
            ),
            {
                ref: self._output_table(job, ref, now)
                for ref in workflow.diagnostic_refs
                if job.output_locations[ref].endswith(".parquet")
                if ref in job.output_locations
            },
        )
        spatial = SpatialResult(
            location=job.output_locations[final_ref],
            media_type="application/vnd.apache.parquet",
            crs=CANONICAL_CRS,
            feature_identity_fields=tuple(item.column for item in contract.key_columns),
            feature_count=len(selected_keys),
        )
        answer_map = AnswerMapRepresentation(
            layer_ref=final_ref,
            geometry_location=spatial.location,
            feature_count=spatial.feature_count,
            crs=spatial.crs,
            title=task.required_output,
            key_columns=tuple(
                ResultKeyColumn(role=item.role, field=item.field, column=item.column)
                for item in contract.key_columns
            ),
            geometry_role=contract.geometry_role,
            value_field=contract.value_column,
            value_scale=contract.value_scale,
        )
        units = " x ".join(task.role(role).semantic_label for role in contract.per_roles)
        summary = (
            f"In the selected snapshot, {len(selected_keys)} of {len(rows)} "
            f"{units} rows answer the required output ({task.required_output}); "
            f"the value column {contract.value_column} carries a "
            f"{contract.value_scale} measurement. Results reflect only the "
            "registered records in the pinned snapshot."
        )
        answer = CandidateAnswer(
            candidate_answer_id="",
            constructed_at=now,
            result_table=tuple(
                CandidateResultRow(keys=keys, value=value) for keys, value in rows
            ),
            selected_keys=selected_keys,
            selected_geometry=spatial,
            answer_map=answer_map,
            diagnostics=diagnostics,
            summary=summary,
            reproducibility=_reproducibility(job, draft, catalog),
        )
        return answer.model_copy(
            update={
                "candidate_answer_id": f"sha256:{sha256(answer.identity_payload())}"
            }
        )

    def _output_table(
        self,
        job: ExecutionJob,
        ref: str,
        now: datetime,
    ) -> _GeoParquet:
        """Load a declared execution output only from its pinned storage key."""

        key = f"execution-jobs/{job.job_id}/outputs/{ref}.parquet"
        stored = self._storage.read(key)
        if stored is None:
            self._reject(
                now,
                "output-missing",
                f"Declared execution output is unavailable: {ref}.",
                ref,
            )
        return self._read_geoparquet(stored.data, ref, now)


    def _read_geoparquet(
        self,
        data: bytes,
        ref: str,
        now: datetime,
    ) -> _GeoParquet:
        """Parse GeoParquet metadata and require the frozen EPSG:28992 CRS."""

        try:
            table = _GeoParquet(data)
            metadata = table.schema.metadata or {}
            geo = json.loads(metadata[b"geo"])
            primary = str(geo["primary_column"])
            column = cast(Mapping[str, object], geo["columns"])[primary]
            crs_document = cast(Mapping[str, object], column)["crs"]
            crs = CRS.from_json_dict(cast(dict[str, object], crs_document))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._reject(
                now,
                "invalid-geoparquet",
                f"Output has no usable GeoParquet geometry and CRS: {ref}.",
                ref,
            )
        if crs != CRS.from_user_input(CANONICAL_CRS):
            self._reject(
                now,
                "crs-mismatch",
                f"Output {ref} must use {CANONICAL_CRS}.",
                ref,
            )
        return table

    def _keyed_rows(
        self,
        table: _GeoParquet,
        now: datetime,
        *,
        ref: str,
        contract: ResultContract,
    ) -> tuple[tuple[tuple[str, ...], float], ...]:
        """Parse rows as (keys, value) against the result contract.

        Keys are the contract's key columns, stringified; the value must be
        finite (and a non-negative integer for a count); null keys, empty or
        invalid geometries, and duplicate keys with conflicting values are
        rejected. Exact duplicate rows (a retained tie under a per-source
        key) collapse into one.
        """

        columns = contract.columns
        if not set(columns).issubset(table.column_names):
            self._reject(
                now,
                "result-shape-mismatch",
                f"Output {ref} lacks the contract columns {columns}; it has "
                f"{sorted(table.column_names)}.",
                ref,
            )
        key_columns = tuple(item.column for item in contract.key_columns)
        parsed: dict[tuple[str, ...], float] = {}
        for row in table.rows(columns):
            keys = _row_keys(row, key_columns)
            value = _row_value(row[contract.value_column], contract.value_scale)
            geometry = _geometry(row["geometry"])
            if (
                keys is None
                or value is None
                or parsed.get(keys, value) != value
                or geometry is None
                or geometry.is_empty
                or not geometry.is_valid
            ):
                self._reject(
                    now,
                    "invalid-result-row",
                    f"Output {ref} contains an invalid row: {keys!r}.",
                    ref,
                )
            parsed[keys] = value
        return tuple(sorted(parsed.items()))

    def _reject(
        self,
        now: datetime,
        code: AnswerConstructionCode,
        message: str,
        ref: str | None = None,
    ) -> NoReturn:
        raise CandidateAnswerRejected(
            CandidateAnswerFailure(
                evaluated_at=now,
                diagnostics=(
                    AnswerConstructionDiagnostic(
                        code=code,
                        message=message,
                        ref=ref,
                    ),
                ),
            )
        )


def _candidate_diagnostics(
    catalog_diagnostics: tuple[QualityDiagnostic, ...],
    retained: Mapping[str, _GeoParquet],
) -> tuple[CandidateDiagnostic, ...]:
    """Report ingestion-time data-quality diagnostics from the pinned
    catalog layers plus every retained diagnostic output of the workflow,
    each as a category named by its ref with one record ref per row."""

    collected: dict[str, list[str]] = {}
    for diagnostic in catalog_diagnostics:
        refs = collected.setdefault(diagnostic.category, [])
        for record_ref in diagnostic.record_refs:
            _append_unique(refs, record_ref)
    for ref, table in retained.items():
        refs = collected.setdefault(ref, [])
        identity_column = "id" if "id" in table.column_names else None
        for index, row in enumerate(
            table.rows((identity_column,) if identity_column else ())
        ):
            identity = row.get(identity_column) if identity_column else None
            _append_unique(
                refs,
                f"{ref}.{identity}" if identity is not None else f"{ref}:feature:{index}",
            )
    return tuple(
        CandidateDiagnostic(
            category=category,
            count=len(refs),
            record_refs=tuple(refs),
        )
        for category, refs in sorted(collected.items())
    )


def _reproducibility(
    job: ExecutionJob,
    draft: WorkflowDraft,
    catalog: CatalogVersion,
) -> ReproducibilityEnvelope:
    """Assemble the audit envelope from the job, draft, and pinned catalog."""

    assert job.runtime is not None
    inputs = tuple(
        ReproducibilityInput(
            capability_input_ref=binding.capability_input_ref,
            dataset_id=binding.dataset_id,
            layer_id=binding.layer_id,
            dataset_version=binding.dataset_version,
            content_hash=binding.content_hash,
            source_content_hash=catalog.layer(
                binding.dataset_id, binding.layer_id
            ).raw.provenance.source_content_hash,
            retrieved_at=catalog.layer(
                binding.dataset_id, binding.layer_id
            ).raw.provenance.retrieved_at,
        )
        for binding in draft.data_bindings.value
    )
    annotation_versions = tuple(
        sorted(
            {
                annotation.version
                for layer in catalog.layers
                if layer.enriched is not None
                for annotation in (
                    layer.enriched.name_en,
                    layer.enriched.description_en,
                    layer.enriched.semantic_label,
                    *(
                        value
                        for attribute in layer.enriched.attributes
                        for value in (
                            attribute.name_en,
                            attribute.description_en,
                            attribute.semantic_label,
                            attribute.measurement_scale,
                        )
                    ),
                )
            }
        )
    )
    provenance = draft.provenance
    return ReproducibilityEnvelope(
        execution_job_id=job.job_id,
        draft_id=draft.draft_id,
        validation_id=job.validation_id,
        catalog_version=catalog.version,
        inputs=inputs,
        annotation_versions=annotation_versions,
        tool_registry_version=draft.tool_registry_version,
        geopandas_version=job.runtime.geopandas,
        shapely_version=job.runtime.shapely,
        code_commit=job.runtime.code_commit,
        planning_provider=provenance.provider,
        planning_model=provenance.model,
        planning_role_settings={
            "reasoning_effort": provenance.settings.reasoning_effort,
            "max_output_tokens": provenance.settings.max_output_tokens,
        },
        planning_prompt_version=provenance.prompt_version,
        planning_schema_version=provenance.schema_version,
        task_schema_version=draft.task_specification.provenance.schema_version,
        effective_parameters=tuple(
            EffectiveParameterRecord(
                step_id=step.step_id,
                algorithm_id=step.algorithm_id,
                parameters={
                    name: _serializable_parameter(value)
                    for name, value in step.parameters.items()
                },
            )
            for step in job.effective_steps
        ),
    )


def _row_keys(
    row: Mapping[str, object],
    key_columns: tuple[str, ...],
) -> tuple[str, ...] | None:
    """Stringify the key columns; a null component invalidates the row."""

    values = tuple(row[name] for name in key_columns)
    if any(value is None for value in values):
        return None
    return tuple(str(value) for value in values)


def _row_value(value: object, scale: str) -> float | None:
    """A finite numeric value; a count must be a non-negative integer."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if scale == "CountA" and (number < 0 or not number.is_integer()):
        return None
    return number


def _geometry(value: object) -> Any | None:
    if value is None:
        return None
    try:
        return from_wkb(cast(str | bytes, value))
    except (TypeError, ValueError):
        return None


def _append_unique(refs: list[str], record_ref: str) -> None:
    if record_ref not in refs:
        refs.append(record_ref)


def _serializable_parameter(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    return value


