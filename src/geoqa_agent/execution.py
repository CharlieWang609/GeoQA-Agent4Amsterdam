# SPDX-License-Identifier: GPL-3.0-only

"""Execution authorization records and in-process deterministic execution."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import logging
from pathlib import Path
import tempfile
from typing import Callable, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field

from data_pipeline.errors import ConcurrentPublicationError
from data_pipeline.serialization import canonical_json, sha256
from data_pipeline.storage import ObjectStore
from data_pipeline.catalog import CatalogReader
from data_pipeline.models import CatalogLayer
from geoqa_agent.tool_registry import CapabilityNotExecutableError, ToolRegistry
from geoqa_agent.workflow_planning import (
    DiagnosticCode,
    ValidationResult,
    ValidationStatus,
    WorkflowDraft,
    WorkflowDraftRepository,
)


LOGGER = logging.getLogger(__name__)


class ImmutableExecutionModel(BaseModel):
    """Base for execution records: frozen and rejecting unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AdvisoryOverride(ImmutableExecutionModel):
    """Attributed human acknowledgement of the advisory diagnostics."""

    actor_principal_id: str
    acknowledged_at: datetime
    diagnostic_codes: tuple[DiagnosticCode, ...]


class ExecutionAuthorization(ImmutableExecutionModel):
    """Pinned human decision to execute one exact draft + validation pair."""

    session_id: str
    actor_principal_id: str
    authorized_at: datetime
    expires_at: datetime
    draft_version: int
    draft_version_id: str
    draft_id: str
    validation_id: str
    advisory_override: AdvisoryOverride | None = None


class ExecutionJobReference(ImmutableExecutionModel):
    job_id: str
    status: ExecutionJobStatus


class EffectiveExecutionStep(ImmutableExecutionModel):
    step_id: str
    algorithm_id: str
    parameters: Mapping[str, object]
    stdout: str
    stderr: str
    elapsed_seconds: float


class ExecutionFailure(ImmutableExecutionModel):
    code: str
    message: str
    step_id: str | None = None


class ExecutionRuntimeProvenance(ImmutableExecutionModel):
    """Version identity of the deterministic runtime that executed the plan."""

    geopandas: str
    shapely: str
    code_commit: str


class ExecutionJob(ImmutableExecutionModel):
    """Durable job record embedding its authorizing human decision."""

    job_id: str
    session_id: str
    owner_principal_id: str
    authorization: ExecutionAuthorization
    draft_id: str
    validation_id: str
    status: ExecutionJobStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    effective_steps: tuple[EffectiveExecutionStep, ...] = ()
    output_locations: Mapping[str, str] = Field(default_factory=dict)
    runtime: ExecutionRuntimeProvenance | None = None
    failure: ExecutionFailure | None = None


class ExecutionAuthorizationError(ValueError):
    """The current session state cannot authorize execution."""


def validation_is_authorized(
    validation: ValidationResult,
    authorization: ExecutionAuthorization,
) -> bool:
    """Verify the pinned human decision against the exact Validation result."""

    if validation.status is ValidationStatus.PASS:
        return True
    if validation.status is not ValidationStatus.PASS_WITH_WARNINGS:
        return False
    # PASS_WITH_WARNINGS runs only with an override covering exactly the
    # advisory codes the validation reported.
    override = authorization.advisory_override
    return (
        override is not None
        and override.diagnostic_codes == validation.advisory_diagnostic_codes
    )


class PinnedInputMaterializer(Protocol):
    def materialize(self, layer: CatalogLayer, destination: Path) -> None: ...


class ExecutionOutputPublisher(Protocol):
    def publish(
        self,
        job_id: str,
        ref: str,
        source: Path,
        *,
        expires_at: datetime,
    ) -> str: ...


class ExecutionSessionSink(Protocol):
    def record(self, job: ExecutionJob) -> None: ...


class NullExecutionSessionSink:
    def record(self, job: ExecutionJob) -> None:
        del job


class OperationRunResult(ImmutableExecutionModel):
    stdout: str
    stderr: str
    elapsed_seconds: float = 0.0


class OperationRunner(Protocol):
    def run(
        self,
        algorithm_id: str,
        parameters: Mapping[str, object],
    ) -> OperationRunResult: ...


class ExecutionRepository:
    """Persist each Execution Job as one ETag-guarded JSON document."""

    def __init__(self, storage: ObjectStore) -> None:
        self._storage = storage

    def create_job(self, job: ExecutionJob) -> None:
        key = _job_key(job.job_id)
        self._storage.compare_and_swap(
            key,
            canonical_json(job.model_dump(mode="json")),
            None,
        )
        self._storage.set_expiry(key, job.expires_at)
        LOGGER.info(
            "execution_job.queued job=%s session=%s",
            job.job_id,
            job.session_id,
        )

    def get_job(self, job_id: str) -> ExecutionJob:
        stored = self._storage.read(_job_key(job_id))
        if stored is None:
            raise LookupError(f"Execution Job does not exist: {job_id}")
        return ExecutionJob.model_validate_json(stored.data)

    def transition(
        self,
        job: ExecutionJob,
        status: ExecutionJobStatus,
        *,
        at: datetime,
        effective_steps: tuple[EffectiveExecutionStep, ...] | None = None,
        output_locations: Mapping[str, str] | None = None,
        runtime: ExecutionRuntimeProvenance | None = None,
        failure: ExecutionFailure | None = None,
    ) -> ExecutionJob:
        """Advance the job; the caller must hold the latest persisted state."""

        key = _job_key(job.job_id)
        current = self._storage.read(key)
        if current is None or ExecutionJob.model_validate_json(current.data) != job:
            raise ConcurrentPublicationError("Execution Job changed concurrently.")
        updated = job.model_copy(
            update={
                "status": status,
                "updated_at": at,
                "effective_steps": (
                    job.effective_steps
                    if effective_steps is None
                    else effective_steps
                ),
                "output_locations": (
                    job.output_locations
                    if output_locations is None
                    else output_locations
                ),
                "runtime": job.runtime if runtime is None else runtime,
                "failure": failure,
            }
        )
        self._storage.compare_and_swap(
            key,
            canonical_json(updated.model_dump(mode="json")),
            current.etag,
        )
        self._storage.set_expiry(key, job.expires_at)
        LOGGER.info(
            "execution_job.%s job=%s session=%s failure=%s",
            updated.status.value,
            updated.job_id,
            updated.session_id,
            None if updated.failure is None else updated.failure.code,
        )
        return updated


def _job_key(job_id: str) -> str:
    return f"execution-jobs/{job_id}.json"


class ObjectStorePinnedInputMaterializer:
    """Materialize a bounded content-addressed object from the ObjectStore."""

    def __init__(self, storage: ObjectStore, *, max_input_bytes: int) -> None:
        self._storage = storage
        self._max_input_bytes = max_input_bytes

    def materialize(self, layer: CatalogLayer, destination: Path) -> None:
        digest = layer.content_hash.removeprefix("sha256:")
        key = f"datasets/{layer.dataset_id}/{layer.layer_id}/{digest}.parquet"
        stored = self._storage.read(key)
        if stored is None or f"sha256:{sha256(stored.data)}" != layer.content_hash:
            raise ValueError("Pinned Catalog input is missing or corrupt.")
        if len(stored.data) > self._max_input_bytes:
            raise ValueError("Pinned Catalog input exceeds the execution size limit.")
        destination.write_bytes(stored.data)


class ObjectStoreExecutionOutputPublisher:
    """Publish bounded outputs through the ObjectStore."""

    def __init__(self, storage: ObjectStore, *, max_output_bytes: int) -> None:
        self._storage = storage
        self._max_output_bytes = max_output_bytes

    def publish(
        self,
        job_id: str,
        ref: str,
        source: Path,
        *,
        expires_at: datetime,
    ) -> str:
        if source.stat().st_size > self._max_output_bytes:
            raise ValueError("Execution output exceeds the persistence size limit.")
        key = f"execution-jobs/{job_id}/outputs/{ref}.parquet"
        self._storage.put_immutable(key, source.read_bytes())
        self._storage.set_expiry(key, expires_at)
        return self._storage.uri(key)


class _ExecutionBlockedError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        step_id: str | None = None,
        effective_steps: tuple[EffectiveExecutionStep, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.step_id = step_id
        self.effective_steps = effective_steps


class ExecutionWorker:
    """Mechanically replay one exact authorized plan without runtime planning."""

    def __init__(
        self,
        *,
        storage: ObjectStore,
        tool_registry: ToolRegistry,
        input_materializer: PinnedInputMaterializer,
        runner: OperationRunner,
        clock: Callable[[], datetime],
        max_output_bytes: int = 100 * 1024 * 1024,
        output_publisher: ExecutionOutputPublisher | None = None,
        session_sink: ExecutionSessionSink | None = None,
        runtime_provenance: ExecutionRuntimeProvenance | None = None,
    ) -> None:
        self._storage = storage
        self._tool_registry = tool_registry
        self._input_materializer = input_materializer
        self._runner = runner
        self._clock = clock
        self._repository = ExecutionRepository(storage)
        self._output_publisher = (
            output_publisher
            or ObjectStoreExecutionOutputPublisher(
                storage,
                max_output_bytes=max_output_bytes,
            )
        )
        self._session_sink = session_sink or NullExecutionSessionSink()
        self._runtime_provenance = runtime_provenance

    def execute(self, job_id: str) -> ExecutionJob:
        """Execute one queued job in process; a terminal job returns as-is."""

        job = self._repository.get_job(job_id)
        if job.status is not ExecutionJobStatus.QUEUED:
            return job
        effective_steps: tuple[EffectiveExecutionStep, ...] = ()
        try:
            draft = self._resolve_authorized_draft(job)
            layers = self._resolve_pinned_layers(draft)
            for step in draft.concrete_workflow.value.steps:
                try:
                    self._tool_registry.algorithm(step.algorithm_id)
                except CapabilityNotExecutableError as error:
                    raise _ExecutionBlockedError(
                        "unavailable-operation",
                        f"Operation is not registered for execution: {step.algorithm_id}",
                        step.step_id,
                    ) from error
            job = self._repository.transition(
                job,
                ExecutionJobStatus.RUNNING,
                at=self._clock(),
            )
            with tempfile.TemporaryDirectory(prefix=f"geoqa-{job.job_id}-") as directory:
                output_locations, effective_steps = self._run_draft(
                    job,
                    draft,
                    layers,
                    Path(directory),
                )
            job = self._repository.transition(
                job,
                ExecutionJobStatus.SUCCEEDED,
                at=self._clock(),
                effective_steps=effective_steps,
                output_locations=output_locations,
                runtime=self._runtime_provenance,
            )
        except Exception as error:
            blocked = error if isinstance(error, _ExecutionBlockedError) else None
            if blocked is not None and blocked.effective_steps:
                effective_steps = blocked.effective_steps
            failure = ExecutionFailure(
                code=blocked.code if blocked is not None else "runtime-failure",
                message=str(error),
                step_id=blocked.step_id if blocked is not None else None,
            )
            job = self._repository.transition(
                self._repository.get_job(job.job_id),
                ExecutionJobStatus.FAILED,
                at=self._clock(),
                effective_steps=effective_steps,
                failure=failure,
            )
        finally:
            if job.status in {
                ExecutionJobStatus.SUCCEEDED,
                ExecutionJobStatus.FAILED,
            }:
                self._session_sink.record(job)
        return job

    def _resolve_authorized_draft(self, job: ExecutionJob) -> WorkflowDraft:
        """Load the exact authorized draft, or raise a blocked-execution error."""

        authorization = job.authorization
        if self._clock() >= min(job.expires_at, authorization.expires_at):
            raise _ExecutionBlockedError(
                "execution-expired",
                "Execution Authorization expired before execution started.",
            )
        drafts = WorkflowDraftRepository(self._storage)
        try:
            draft = drafts.get(job.draft_id)
            validation = drafts.get_validation_version(
                job.draft_id,
                job.validation_id,
            )
        except (LookupError, ValueError) as error:
            raise _ExecutionBlockedError(
                "authorized-plan-unavailable",
                "The exact authorized draft or Validation result is unavailable.",
            ) from error
        if not validation_is_authorized(validation, authorization):
            raise _ExecutionBlockedError(
                "validation-failed",
                "The exact authorized Validation result did not pass.",
            )
        if draft.tool_registry_version != self._tool_registry.version:
            raise _ExecutionBlockedError(
                "tool-registry-version-unavailable",
                "The authorized Tool Registry version is unavailable.",
            )
        return draft

    def _resolve_pinned_layers(
        self,
        draft: WorkflowDraft,
    ) -> Mapping[str, CatalogLayer]:
        try:
            catalog = CatalogReader(self._storage).get(draft.catalog_version)
        except LookupError as error:
            raise _ExecutionBlockedError(
                "pinned-input-unavailable",
                "The pinned Catalog version is unavailable.",
            ) from error
        indexed = {
            (layer.dataset_id, layer.layer_id): layer for layer in catalog.layers
        }
        resolved: dict[str, CatalogLayer] = {}
        for binding in draft.data_bindings.value:
            layer = indexed.get((binding.dataset_id, binding.layer_id))
            if (
                layer is None
                or binding.catalog_version != draft.catalog_version
                or layer.dataset_version != binding.dataset_version
                or layer.content_hash != binding.content_hash
            ):
                raise _ExecutionBlockedError(
                    "pinned-input-unavailable",
                    f"Pinned input cannot be resolved: {binding.capability_input_ref}",
                )
            resolved[binding.capability_input_ref] = layer
        return resolved

    def _run_draft(
        self,
        job: ExecutionJob,
        draft: WorkflowDraft,
        layers: Mapping[str, CatalogLayer],
        directory: Path,
    ) -> tuple[Mapping[str, str], tuple[EffectiveExecutionStep, ...]]:
        """Materialize inputs, replay each step, and publish retained outputs.

        ``refs`` maps workflow refs to local paths as steps produce them; only
        the declared final/result/diagnostic refs are published and returned.
        """

        refs: dict[str, object] = {}
        input_directory = directory / "inputs"
        output_directory = directory / "outputs"
        input_directory.mkdir()
        output_directory.mkdir()
        for ref, layer in layers.items():
            destination = input_directory / f"{ref}.parquet"
            self._input_materializer.materialize(layer, destination)
            refs[ref] = destination
        # Deterministic placeholder values a step's "template" parameters
        # may reference, one per bound input layer.
        template_values: dict[str, object] = {
            f"{ref}_retrieved_at": (
                layer.raw.provenance.retrieved_at.isoformat().replace(
                    "+00:00",
                    "Z",
                )
            )
            for ref, layer in layers.items()
        }
        effective: list[EffectiveExecutionStep] = []
        for index, step in enumerate(draft.concrete_workflow.value.steps):
            parameters: dict[str, object] = {}
            for binding in step.parameters:
                if binding.source == "ref":
                    if not isinstance(binding.value, str) or binding.value not in refs:
                        raise _ExecutionBlockedError(
                            "unresolved-reference",
                            f"Step input ref is unavailable: {binding.value}",
                            step.step_id,
                        )
                    value = refs[binding.value]
                elif binding.source == "template":
                    if not isinstance(binding.value, str):
                        raise _ExecutionBlockedError(
                            "invalid-parameter",
                            "Template parameter must be a string.",
                            step.step_id,
                        )
                    value = binding.value.format(**template_values)
                else:
                    value = binding.value
                parameters[binding.name] = value
            # "sink" outputs are files the operation writes; assign each a
            # run-local path and expose it to later steps under its ref.
            for output in step.outputs:
                if output.kind != "sink":
                    continue
                destination = output_directory / f"{index:02d}-{output.ref}.parquet"
                parameters[output.name] = destination
                refs[output.ref] = destination
            try:
                run = self._runner.run(step.algorithm_id, parameters)
            except Exception as error:
                failed_step = EffectiveExecutionStep(
                    step_id=step.step_id,
                    algorithm_id=step.algorithm_id,
                    parameters=parameters,
                    stdout=str(getattr(error, "stdout", "") or ""),
                    stderr=str(getattr(error, "stderr", "") or error),
                    elapsed_seconds=0.0,
                )
                raise _ExecutionBlockedError(
                    "runtime-failure",
                    f"Execution step failed: {error}",
                    step.step_id,
                    (*effective, failed_step),
                ) from error
            effective.append(
                EffectiveExecutionStep(
                    step_id=step.step_id,
                    algorithm_id=step.algorithm_id,
                    parameters=parameters,
                    stdout=run.stdout,
                    stderr=run.stderr,
                    elapsed_seconds=run.elapsed_seconds,
                )
            )
            for output in step.outputs:
                if output.kind == "sink":
                    path = refs[output.ref]
                    if not isinstance(path, Path) or not path.is_file():
                        raise _ExecutionBlockedError(
                            "runtime-failure",
                            f"Execution step did not create output {output.ref}.",
                            step.step_id,
                            tuple(effective),
                        )
        workflow = draft.concrete_workflow.value
        retained_refs = {
            workflow.final_output_ref,
            workflow.result_table_ref,
            *workflow.diagnostic_refs,
        }
        locations: dict[str, str] = {}
        for ref in sorted(retained_refs):
            source = refs.get(ref)
            if not isinstance(source, Path) or not source.is_file():
                raise _ExecutionBlockedError(
                    "runtime-failure",
                    f"Declared execution output is unavailable: {ref}",
                    effective_steps=tuple(effective),
                )
            try:
                location = self._output_publisher.publish(
                    job.job_id,
                    ref,
                    source,
                    expires_at=job.expires_at,
                )
            except Exception as error:
                raise _ExecutionBlockedError(
                    "runtime-failure",
                    f"Execution output persistence failed: {error}",
                    effective_steps=tuple(effective),
                ) from error
            locations[ref] = location
        return locations, tuple(effective)
