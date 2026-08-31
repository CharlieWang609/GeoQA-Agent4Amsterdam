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
    Protocol,
    cast,
)

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict
from pyproj import CRS
from shapely import from_wkb

from data_pipeline.catalog import CatalogReader
from data_pipeline.models import CatalogLayer, CatalogVersion, QualityDiagnostic
from data_pipeline.serialization import canonical_json, sha256
from data_pipeline.storage import ObjectStore
from geoqa_agent.execution import (
    AdvisoryOverride,
    ExecutionJob,
    ExecutionJobStatus,
    validation_is_authorized,
)
from geoqa_agent.governance import (
    BindingRole,
    NearestTaskSpecification,
    TaskSpecification,
)
from data_pipeline.geoparquet import CANONICAL_CRS
from geoqa_agent.workflow_planning import (
    COUNT_COLUMN,
    ValidationStatus,
    WorkflowDraft,
    WorkflowDraftRepository,
)


CCD_ONTOLOGY = Path(__file__).resolve().parents[2] / "ontology" / "ccd.ttl"
ABSTRACT_TOOL_ONTOLOGY = (
    Path(__file__).resolve().parents[2] / "ontology" / "tools" / "abstract.ttl"
)
GEOPANDAS_TOOL_ONTOLOGY = (
    Path(__file__).resolve().parents[2] / "ontology" / "tools" / "geopandas.ttl"
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
    identificatie: str
    volgnummer: int
    count: int


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


class AnswerMapRepresentation(ImmutableAnswerModel):
    layer_ref: str
    geometry_location: str
    feature_count: int
    crs: str
    title: str
    count_field: str


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
    prompt/schema/ontology/tool versions, runtime identity, and effective
    parameters."""

    execution_job_id: str
    draft_id: str
    validation_id: str
    catalog_version: str
    inputs: tuple[ReproducibilityInput, ...]
    annotation_versions: tuple[str, ...]
    ontology_versions: Mapping[str, str]
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
    validation_status: Literal["pass", "pass_with_warnings", "fail"] = "pass"
    advisory_override: AdvisoryOverride | None = None


AnswerConstructionCode = Literal[
    "execution-not-successful",
    "runtime-provenance-missing",
    "reproducibility-input-missing",
    "validation-not-passed",
    "diagnostic-shape-mismatch",
    "output-missing",
    "invalid-geoparquet",
    "crs-mismatch",
    "result-shape-mismatch",
    "invalid-result-row",
]


class CandidateAnswer(ImmutableAnswerModel):
    """The complete reviewable answer; its id content-addresses all fields."""

    candidate_answer_id: str
    constructed_at: datetime
    result_table: tuple[CandidateResultRow, ...]
    # The final-output subset answering the required output (for the
    # original showcase question these were the zero-count supports; any
    # count-valued selection is legitimate).
    selected_identities: tuple[tuple[str, int], ...]
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


class NearestCandidateResultRow(ImmutableAnswerModel):
    source_id: str
    target_id: str
    distance_m: float


class NearestCandidateDiagnostic(ImmutableAnswerModel):
    category: str
    count: int
    record_refs: tuple[str, ...]


class NearestAnswerMapRepresentation(ImmutableAnswerModel):
    layer_ref: str
    geometry_location: str
    feature_count: int
    crs: str
    title: str
    identity_field: str
    distance_field: str


class NearestCandidateAnswer(ImmutableAnswerModel):
    """Reviewable directional source-to-target nearest result."""

    answer_kind: Literal["point-to-point-euclidean-nearest"] = (
        "point-to-point-euclidean-nearest"
    )
    candidate_answer_id: str
    constructed_at: datetime
    result_table: tuple[NearestCandidateResultRow, ...]
    source_geometry: SpatialResult
    answer_map: NearestAnswerMapRepresentation
    diagnostics: tuple[NearestCandidateDiagnostic, ...]
    summary: str
    sanity_checks: tuple[str, ...]
    reproducibility: ReproducibilityEnvelope

    def identity_payload(self) -> bytes:
        return canonical_json(
            self.model_dump(mode="json", exclude={"candidate_answer_id"})
        )


CandidateAnswerValue = CandidateAnswer | NearestCandidateAnswer


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


class CandidateAnswerConstructor(Protocol):
    """Question Session boundary for automatic answer construction."""

    def construct(self, job: ExecutionJob) -> CandidateAnswerValue: ...


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
        row shape; any failure raises CandidateAnswerRejected. Comparison
        against the frozen reference oracle lives in the evaluation harness,
        not in this serving path.
        """

        now = self._evaluated_at()
        if job.status is not ExecutionJobStatus.SUCCEEDED:
            self._reject(
                now,
                "execution-not-successful",
                "Only a successful Execution Result can become a Candidate Answer.",
            )
        if job.runtime is None or not all(
            (job.runtime.geopandas, job.runtime.shapely, job.runtime.code_commit)
        ):
            self._reject(
                now,
                "runtime-provenance-missing",
                "Execution runtime provenance is incomplete.",
            )
        drafts = WorkflowDraftRepository(self._storage)
        try:
            draft = drafts.get(job.draft_id)
            validation = drafts.get_validation_version(
                job.draft_id,
                job.validation_id,
            )
            catalog = CatalogReader(self._storage).get(draft.catalog_version)
        except (LookupError, ValueError) as error:
            self._reject(
                now,
                "reproducibility-input-missing",
                f"Pinned Candidate Answer input is unavailable: {error}",
            )
        # A warned validation is only acceptable with the recorded advisory
        # override on the job's own authorization.
        if validation.status is not ValidationStatus.PASS and (
            not validation_is_authorized(validation, job.authorization)
        ):
            self._reject(
                now,
                "validation-not-passed",
                "The exact execution Validation result did not pass.",
            )
        if isinstance(
            draft.task_specification.value,
            NearestTaskSpecification,
        ):
            return self._construct_nearest(
                job,
                draft,
                catalog,
                validation_status=validation.status,
                advisory_override=job.authorization.advisory_override,
                now=now,
            )
        result_ref = draft.concrete_workflow.value.result_table_ref
        geometry_ref = draft.concrete_workflow.value.final_output_ref
        result_table = self._output_table(job, result_ref, now)
        selected_table = self._output_table(job, geometry_ref, now)
        unmatched_table = (
            self._output_table(job, "unmatched_points", now)
            if "unmatched_points" in draft.concrete_workflow.value.diagnostic_refs
            else None
        )
        rows = self._result_rows(result_table, result_ref, now)
        selected = self._selected_rows(selected_table, geometry_ref, now)
        # The final output must be a coherent subset of the result table.
        counts_by_identity = dict(rows)
        for identity, count in selected:
            if counts_by_identity.get(identity) != count:
                self._reject(
                    now,
                    "result-shape-mismatch",
                    "Final output row is not part of the result table: "
                    f"{identity!r}.",
                    geometry_ref,
                )
        selected_identities = tuple(sorted(identity for identity, _ in selected))
        counted_binding = _binding(draft, BindingRole.COUNTED_OBJECTS)
        counted_layer = _layer(
            catalog,
            counted_binding.dataset_id,
            counted_binding.layer_id,
        )
        diagnostics = _candidate_diagnostics(
            counted_layer.quality.diagnostics,
            unmatched_table,
            record_prefix=counted_layer.layer_id,
        )
        result_rows = tuple(
            CandidateResultRow(
                identificatie=identity[0],
                volgnummer=identity[1],
                count=count,
            )
            for identity, count in rows
        )
        task = draft.task_specification.value
        assert isinstance(task, TaskSpecification)
        spatial = SpatialResult(
            location=job.output_locations[geometry_ref],
            media_type="application/vnd.apache.parquet",
            crs=CANONICAL_CRS,
            feature_identity_fields=task.support.identity_fields,
            feature_count=len(selected_identities),
        )
        answer_map = AnswerMapRepresentation(
            layer_ref=geometry_ref,
            geometry_location=spatial.location,
            feature_count=spatial.feature_count,
            crs=spatial.crs,
            title=task.required_output,
            count_field=COUNT_COLUMN,
        )
        summary = (
            f"In the selected snapshot, {len(selected_identities)} of "
            f"{len(result_rows)} {task.support.semantic_label} units answer "
            f"the required output ({task.required_output}). Counts reflect "
            "only the registered records in the pinned snapshot."
        )
        answer = CandidateAnswer(
            candidate_answer_id="",
            constructed_at=now,
            result_table=result_rows,
            selected_identities=selected_identities,
            selected_geometry=spatial,
            answer_map=answer_map,
            diagnostics=diagnostics,
            summary=summary,
            reproducibility=_reproducibility(
                job,
                draft,
                catalog,
                validation_status=validation.status,
                advisory_override=job.authorization.advisory_override,
            ),
        )
        return answer.model_copy(
            update={
                "candidate_answer_id": f"sha256:{sha256(answer.identity_payload())}"
            }
        )

    def _construct_nearest(
        self,
        job: ExecutionJob,
        draft: WorkflowDraft,
        catalog: CatalogVersion,
        *,
        validation_status: ValidationStatus,
        advisory_override: AdvisoryOverride | None,
        now: datetime,
    ) -> NearestCandidateAnswer:
        """Build a nearest answer from governed pair rows and source geometry."""

        workflow = draft.concrete_workflow.value
        result = self._output_table(job, workflow.result_table_ref, now)
        unmatched = (
            self._output_table(job, "unmatched_sources", now)
            if "unmatched_sources" in workflow.diagnostic_refs
            else None
        )
        bindings = {
            binding.capability_input_ref: binding
            for binding in draft.data_bindings.value
        }
        source_binding = bindings["source_points"]
        target_binding = bindings["target_points"]
        source_layer = _layer(
            catalog,
            source_binding.dataset_id,
            source_binding.layer_id,
        )
        target_layer = _layer(
            catalog,
            target_binding.dataset_id,
            target_binding.layer_id,
        )
        result_rows = self._nearest_rows(result, now)
        matched_sources = {row.source_id for row in result_rows}
        unmatched_refs = (
            ()
            if unmatched is None
            else self._unmatched_source_refs(
                unmatched,
                source_layer.layer_id,
                now,
            )
        )

        spatial = SpatialResult(
            location=job.output_locations[workflow.final_output_ref],
            media_type="application/vnd.apache.parquet",
            crs=CANONICAL_CRS,
            feature_identity_fields=("source_id",),
            feature_count=len(matched_sources),
        )
        answer_map = NearestAnswerMapRepresentation(
            layer_ref=workflow.final_output_ref,
            geometry_location=spatial.location,
            feature_count=spatial.feature_count,
            crs=spatial.crs,
            title="Source points styled by nearest-target distance",
            identity_field="source_id",
            distance_field="distance_m",
        )
        # Ingestion-time data-quality diagnostics from the pinned catalog
        # layers, plus the workflow's own unmatched-source output.
        quality: dict[str, list[str]] = {}
        for layer in (source_layer, target_layer):
            for diagnostic in layer.quality.diagnostics:
                collected = quality.setdefault(diagnostic.category, [])
                for record_ref in diagnostic.record_refs:
                    _append_unique(collected, record_ref)
        diagnostics = tuple(
            NearestCandidateDiagnostic(
                category=category,
                count=len(refs),
                record_refs=tuple(refs),
            )
            for category, refs in sorted(quality.items())
            if refs
        ) + (
            NearestCandidateDiagnostic(
                category="unmatched_sources",
                count=len(unmatched_refs),
                record_refs=unmatched_refs,
            ),
        )
        summary = (
            f"Computed planar Euclidean nearest targets for "
            f"{len(matched_sources)} source points in EPSG:28992 metres; "
            f"the result contains {len(result_rows)} source-target rows and "
            f"{len(unmatched_refs)} unmatched sources."
        )
        answer = NearestCandidateAnswer(
            candidate_answer_id="",
            constructed_at=now,
            result_table=result_rows,
            source_geometry=spatial,
            answer_map=answer_map,
            diagnostics=diagnostics,
            summary=summary,
            sanity_checks=(
                "result rows carry source, target, and finite distance",
                "distances are non-negative EPSG:28992 metres",
                "equidistant targets retain separate result rows",
            ),
            reproducibility=_reproducibility(
                job,
                draft,
                catalog,
                validation_status=validation_status,
                advisory_override=advisory_override,
            ),
        )
        return answer.model_copy(
            update={
                "candidate_answer_id": f"sha256:{sha256(answer.identity_payload())}"
            }
        )


    def _nearest_rows(
        self,
        table: _GeoParquet,
        now: datetime,
    ) -> tuple[NearestCandidateResultRow, ...]:
        required = {"source_id", "target_id", "distance_m", "geometry"}
        if not required.issubset(table.column_names):
            self._reject(
                now,
                "result-shape-mismatch",
                "Nearest output lacks source_id, target_id, distance_m, or geometry.",
                "nearest_pairs",
            )
        parsed: list[NearestCandidateResultRow] = []
        pairs: set[tuple[str, str]] = set()
        for raw in table.rows(("source_id", "target_id", "distance_m", "geometry")):
            source_id = _nearest_identity(raw.get("source_id"))
            target_id = _nearest_identity(raw.get("target_id"))
            distance = raw.get("distance_m")
            point = _point(raw.get("geometry"))
            if (
                source_id is None
                or target_id is None
                or isinstance(distance, bool)
                or not isinstance(distance, (int, float))
                or not math.isfinite(float(distance))
                or float(distance) < 0
                or point is None
                or not point.is_valid
                or not _finite_point(point)
                or (source_id, target_id) in pairs
            ):
                self._reject(
                    now,
                    "invalid-result-row",
                    "Nearest output contains an invalid or duplicate pair row.",
                    "nearest_pairs",
                )
            pairs.add((source_id, target_id))
            parsed.append(
                NearestCandidateResultRow(
                    source_id=source_id,
                    target_id=target_id,
                    distance_m=float(distance),
                )
            )
        parsed.sort(key=lambda row: (row.source_id, row.target_id))
        return tuple(parsed)

    def _unmatched_source_refs(
        self,
        table: _GeoParquet,
        record_prefix: str,
        now: datetime,
    ) -> tuple[str, ...]:
        if "id" not in table.column_names:
            self._reject(
                now,
                "diagnostic-shape-mismatch",
                "Unmatched source diagnostics must retain source identity.",
                "unmatched_sources",
            )
        refs: list[str] = []
        for row in table.rows(("id",)):
            identity = row.get("id")
            if identity is None or not str(identity):
                self._reject(
                    now,
                    "diagnostic-shape-mismatch",
                    "Unmatched source diagnostics contain an invalid identity.",
                    "unmatched_sources",
                )
            _append_unique(refs, f"{record_prefix}.{identity}")
        return tuple(sorted(refs))


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

    def _result_rows(
        self,
        table: _GeoParquet,
        ref: str,
        now: datetime,
    ) -> tuple[tuple[tuple[str, int], int], ...]:
        rows = list(
            self._support_rows(table, now, ref=ref, require_nonempty=True)
        )
        rows.sort(key=lambda item: item[0])
        return tuple(rows)

    def _selected_rows(
        self,
        table: _GeoParquet,
        ref: str,
        now: datetime,
    ) -> tuple[tuple[tuple[str, int], int], ...]:
        return self._support_rows(table, now, ref=ref, require_nonempty=False)

    def _support_rows(
        self,
        table: _GeoParquet,
        now: datetime,
        *,
        ref: str,
        require_nonempty: bool,
    ) -> tuple[tuple[tuple[str, int], int], ...]:
        """Parse support rows as (identity, count), rejecting bad rows.

        Identity is (identificatie, volgnummer); duplicates, negative or
        non-integral counts, and empty/invalid polygons are rejected.
        """

        required = {"identificatie", "volgnummer", COUNT_COLUMN, "geometry"}
        if not required.issubset(table.column_names) or (
            require_nonempty and table.num_rows == 0
        ):
            self._reject(
                now,
                "result-shape-mismatch",
                f"Output {ref} lacks required identity, count, or geometry rows.",
                ref,
            )
        parsed: list[tuple[tuple[str, int], int]] = []
        seen: set[tuple[str, int]] = set()
        for row in table.rows(
            ("identificatie", "volgnummer", COUNT_COLUMN, "geometry")
        ):
            identity = _support_identity(row)
            count = _integral_count(row[COUNT_COLUMN])
            geometry = _polygon(row["geometry"])
            if (
                count is None
                or count < 0
                or identity is None
                or identity in seen
                or geometry is None
                or geometry.is_empty
                or not geometry.is_valid
            ):
                self._reject(
                    now,
                    "invalid-result-row",
                    f"Output {ref} contains an invalid support row: {identity!r}.",
                    ref,
                )
            seen.add(identity)
            parsed.append((identity, count))
        return tuple(parsed)


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
    unmatched: _GeoParquet | None,
    *,
    record_prefix: str,
) -> tuple[CandidateDiagnostic, ...]:
    """Report ingestion-time data-quality diagnostics from the pinned catalog
    plus the workflow's own unmatched-points output."""

    collected: dict[str, list[str]] = {}
    for diagnostic in catalog_diagnostics:
        refs = collected.setdefault(diagnostic.category, [])
        for record_ref in diagnostic.record_refs:
            _append_unique(refs, record_ref)
    if unmatched is not None:
        refs = collected.setdefault("unmatched", [])
        for index, row in enumerate(unmatched.rows(("id",))):
            identity = row.get("id")
            _append_unique(
                refs,
                f"{record_prefix}.{identity}"
                if identity is not None
                else f"feature:{index}",
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
    *,
    validation_status: ValidationStatus,
    advisory_override: AdvisoryOverride | None,
) -> ReproducibilityEnvelope:
    """Assemble the audit envelope from the job, draft, and pinned catalog."""

    assert job.runtime is not None
    layers = {
        (layer.dataset_id, layer.layer_id): layer for layer in catalog.layers
    }
    inputs = tuple(
        ReproducibilityInput(
            capability_input_ref=binding.capability_input_ref,
            dataset_id=binding.dataset_id,
            layer_id=binding.layer_id,
            dataset_version=binding.dataset_version,
            content_hash=binding.content_hash,
            source_content_hash=layers[
                (binding.dataset_id, binding.layer_id)
            ].raw.provenance.source_content_hash,
            retrieved_at=layers[
                (binding.dataset_id, binding.layer_id)
            ].raw.provenance.retrieved_at,
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
                    layer.enriched.ccd_meaning,
                    *(
                        value
                        for attribute in layer.enriched.attributes
                        for value in (
                            attribute.name_en,
                            attribute.description_en,
                            attribute.semantic_label,
                            attribute.ccd_meaning,
                        )
                    ),
                )
            }
        )
    )
    ontology_versions = {
        "ccd": _file_hash(CCD_ONTOLOGY),
        "abstract-tools": _file_hash(ABSTRACT_TOOL_ONTOLOGY),
        "geopandas-tools": _file_hash(GEOPANDAS_TOOL_ONTOLOGY),
    }
    provenance = draft.provenance
    return ReproducibilityEnvelope(
        execution_job_id=job.job_id,
        draft_id=draft.draft_id,
        validation_id=job.validation_id,
        validation_status=validation_status.value,
        advisory_override=advisory_override,
        catalog_version=catalog.version,
        inputs=inputs,
        annotation_versions=annotation_versions,
        ontology_versions=ontology_versions,
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


def _binding(draft: WorkflowDraft, role: BindingRole):
    for binding in draft.data_bindings.value:
        if binding.role is role:
            return binding
    raise ValueError(f"Draft has no {role.value} binding.")


def _layer(catalog: CatalogVersion, dataset_id: str, layer_id: str) -> CatalogLayer:
    matches = tuple(
        layer
        for layer in catalog.layers
        if (layer.dataset_id, layer.layer_id) == (dataset_id, layer_id)
    )
    if len(matches) != 1:
        raise ValueError(f"Catalog does not contain {dataset_id}/{layer_id}.")
    return matches[0]


def _integral_count(value: object) -> int | None:
    """Accept a count stored as an integral int or float; reject every
    non-integral shape."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _support_identity(
    row: Mapping[str, object],
) -> tuple[str, int] | None:
    identificatie = row.get("identificatie")
    volgnummer = row.get("volgnummer")
    # Exact int check (not isinstance): a bool volgnummer must be rejected.
    if (
        not isinstance(identificatie, str)
        or not identificatie
        or type(volgnummer) is not int
    ):
        return None
    return identificatie, volgnummer


def _geometry(value: object) -> Any | None:
    if value is None:
        return None
    try:
        return from_wkb(cast(str | bytes, value))
    except (TypeError, ValueError):
        return None


def _polygon(value: object) -> Any | None:
    geometry = _geometry(value)
    if geometry is None or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        return None
    return geometry


def _point(value: object) -> Any | None:
    geometry = _geometry(value)
    if geometry is None or geometry.geom_type != "Point" or geometry.is_empty:
        return None
    return geometry


def _nearest_identity(value: object) -> str | None:
    """Canonicalize governed string or integer ids emitted by Arrow."""

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalized = str(value)
    return normalized if normalized else None


def _append_unique(refs: list[str], record_ref: str) -> None:
    if record_ref not in refs:
        refs.append(record_ref)


def _finite_point(point: Any) -> bool:
    return math.isfinite(point.x) and math.isfinite(point.y)


def _file_hash(path: Path) -> str:
    return f"sha256:{sha256(path.read_bytes())}"


def _serializable_parameter(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    return value


