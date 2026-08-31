# SPDX-License-Identifier: GPL-3.0-only

"""Immutable workflow drafts and deterministic validation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from itertools import combinations
from string import Formatter
from typing import Mapping, cast

from pydantic import TypeAdapter
from rdflib.term import URIRef
from transforge.type import TypeInstance

from data_pipeline.catalog import CatalogReader
from data_pipeline.models import AnnotationStatus, CatalogLayer
from data_pipeline.serialization import canonical_json, sha256
from data_pipeline.storage import ObjectStore
from data_pipeline.geoparquet import CANONICAL_CRS
from geoqa_agent.governance import (
    BindingRole,
    CapabilityTaskSpecification,
    NearestTaskSpecification,
    data_binding_attribute_is_resolved,
    role_identity_fields,
    task_family,
    task_roles,
)
from geoqa_agent.question_interpretation import (
    DataBinding,
    MatchAssessment,
    SupportedInterpretation,
    task_specification_document,
)
from geoqa_agent.ccd import ccd
from geoqa_agent.cct import cct
from geoqa_agent.namespace import CCD
from geoqa_agent.polytype import Polytype
from geoqa_agent.semantic_types import LayerCCDMeaning
from geoqa_agent.semantic_validation import (
    AbstractionCatalog,
    AbstractionSignature,
)
from geoqa_agent.structured_artifacts import (
    ArtifactContract,
    ArtifactProvenance,
    ArtifactRequest,
    StructuredArtifactClient,
)
from geoqa_agent.tool_registry import (
    CapabilityNotExecutableError,
    OutputBinding,
    ParameterBinding,
    OperationContract,
    ToolRegistry,
)


@dataclass(frozen=True)
class AbstractWorkflowStep:
    """One typed, tool-independent operation in a proposed topology."""

    step_id: str
    abstraction_id: str
    input_refs: tuple[str, ...]
    output_ref: str


@dataclass(frozen=True)
class AbstractWorkflow:
    """The proposed abstract topology, composed freely by the planner."""

    steps: tuple[AbstractWorkflowStep, ...]
    final_output_ref: str


class ArtifactKind(StrEnum):
    """Persisted planning artifacts kept distinct for review."""

    TASK_SPECIFICATION = "task-specification"
    DATA_BINDINGS = "data-bindings"
    ABSTRACT_WORKFLOW = "abstract-workflow"
    CONCRETE_WORKFLOW = "concrete-workflow"


@dataclass(frozen=True)
class ConcreteWorkflowStep:
    """One proposed allow-listed operation invocation."""

    step_id: str
    abstract_step_id: str
    algorithm_id: str
    parameters: tuple[ParameterBinding, ...]
    outputs: tuple[OutputBinding, ...]


@dataclass(frozen=True)
class ConcreteWorkflow:
    """A complete proposed executable dataflow."""

    steps: tuple[ConcreteWorkflowStep, ...]
    final_output_ref: str
    result_table_ref: str
    diagnostic_refs: tuple[str, ...]


# Each *Artifact below pairs one planning payload with the LLM provenance
# that produced it, so interpretation and planning stay separately auditable.
@dataclass(frozen=True)
class TaskSpecificationArtifact:
    value: CapabilityTaskSpecification
    provenance: ArtifactProvenance


@dataclass(frozen=True)
class DataBindingsArtifact:
    value: tuple[DataBinding, ...]
    provenance: ArtifactProvenance


@dataclass(frozen=True)
class AbstractWorkflowArtifact:
    value: AbstractWorkflow
    provenance: ArtifactProvenance


@dataclass(frozen=True)
class ConcreteWorkflowArtifact:
    value: ConcreteWorkflow
    provenance: ArtifactProvenance


@dataclass(frozen=True)
class WorkflowDraft:
    """Immutable review draft; validation alone may mark it executable."""

    draft_id: str
    catalog_version: str
    tool_registry_version: str
    provenance: ArtifactProvenance
    task_specification: TaskSpecificationArtifact
    data_bindings: DataBindingsArtifact
    abstract_workflow: AbstractWorkflowArtifact
    concrete_workflow: ConcreteWorkflowArtifact


class ValidationStatus(StrEnum):
    PASS = "pass"
    PASS_WITH_WARNINGS = "pass_with_warnings"
    FAIL = "fail"


class ValidationSeverity(StrEnum):
    BLOCKING = "blocking"
    ADVISORY = "advisory"


VALIDATION_SCHEMA_VERSION = "workflow-validation-v2"


class DiagnosticCode(StrEnum):
    STRUCTURAL_ERROR = "structural-error"
    INVENTED_DATA = "invented-data"
    UNRESOLVED_ANNOTATION = "unresolved-annotation"
    INCOMPATIBLE_TYPE = "incompatible-type"
    MISSING_PARAMETER = "missing-parameter"
    INVALID_PARAMETER = "invalid-parameter"
    CRS_CONFLICT = "crs-conflict"
    COVERAGE_CONFLICT = "coverage-conflict"
    UNAVAILABLE_ALGORITHM = "unavailable-algorithm"
    DISCONNECTED_REFERENCE = "disconnected-reference"


@dataclass(frozen=True)
class ValidationDiagnostic:
    """Stable machine-readable validation failure."""

    code: DiagnosticCode
    message: str
    artifact: ArtifactKind
    severity: ValidationSeverity = ValidationSeverity.BLOCKING
    step_id: str | None = None
    ref: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    """Deterministic gate result with no override field."""

    validation_id: str
    draft_id: str
    status: ValidationStatus
    diagnostics: tuple[ValidationDiagnostic, ...]
    schema_version: str = VALIDATION_SCHEMA_VERSION

    @property
    def is_executable(self) -> bool:
        return self.status is ValidationStatus.PASS

    @property
    def requires_advisory_acknowledgement(self) -> bool:
        return self.status is ValidationStatus.PASS_WITH_WARNINGS

    @property
    def advisory_diagnostic_codes(self) -> tuple[DiagnosticCode, ...]:
        """Return advisory codes in first-emission order without duplicates."""

        return tuple(
            dict.fromkeys(
                diagnostic.code
                for diagnostic in self.diagnostics
                if diagnostic.severity is ValidationSeverity.ADVISORY
            )
        )


_DRAFT: TypeAdapter[WorkflowDraft] = TypeAdapter(WorkflowDraft)
_VALIDATION: TypeAdapter[ValidationResult] = TypeAdapter(ValidationResult)


def sealed_draft(provisional: WorkflowDraft) -> WorkflowDraft:
    """Stamp a complete draft with its content-addressed identity."""

    return replace(
        provisional,
        draft_id=f"sha256:{sha256(_DRAFT.dump_json(provisional))}",
    )
# Boundary parsers for the planner's untrusted artifact JSON.
_ABSTRACT: TypeAdapter[AbstractWorkflow] = TypeAdapter(AbstractWorkflow)
_CONCRETE: TypeAdapter[ConcreteWorkflow] = TypeAdapter(ConcreteWorkflow)


class WorkflowDraftRepository:
    """Persist each draft and its validation as one JSON blob apiece."""

    def __init__(self, storage: ObjectStore) -> None:
        self._storage = storage

    def save(self, draft: WorkflowDraft) -> None:
        self._storage.put_immutable(
            self._draft_key(draft.draft_id),
            _DRAFT.dump_json(draft),
        )

    def get(self, draft_id: str) -> WorkflowDraft:
        stored = self._storage.read(self._draft_key(draft_id))
        if stored is None:
            raise LookupError(f"Workflow draft does not exist: {draft_id}")
        return _DRAFT.validate_json(stored.data)

    def retain_until(self, draft_id: str, expires_at: datetime) -> None:
        """Keep the draft and its validation through a session deadline."""

        self._storage.set_expiry(self._draft_key(draft_id), expires_at)
        self._storage.set_expiry(self._validation_key(draft_id), expires_at)

    def save_validation(self, result: ValidationResult) -> None:
        self._storage.put_immutable(
            self._validation_key(result.draft_id),
            _VALIDATION.dump_json(result),
        )

    def get_validation(self, draft_id: str) -> ValidationResult:
        stored = self._storage.read(self._validation_key(draft_id))
        if stored is None:
            raise LookupError(
                f"Workflow validation does not exist: {draft_id}"
            )
        return _VALIDATION.validate_json(stored.data)

    def get_validation_version(
        self,
        draft_id: str,
        validation_id: str,
    ) -> ValidationResult:
        """Load the draft's validation and require the pinned identity."""

        result = self.get_validation(draft_id)
        if result.validation_id != validation_id:
            raise LookupError(
                f"Validation {validation_id} is not current for {draft_id}."
            )
        return result

    @staticmethod
    def _draft_key(draft_id: str) -> str:
        return f"workflow-drafts/{draft_id}.json"

    @staticmethod
    def _validation_key(draft_id: str) -> str:
        return f"workflow-drafts/{draft_id}.validation.json"

# The presentation layer reads each family's result tables through this
# documented interface; the planner may compose any workflow that fulfils it.
COUNT_COLUMN = "object_count"
FAMILY_OUTPUT_CONTRACTS: Mapping[str, Mapping[str, str]] = {
    "count": {
        "result_table_ref": (
            "one row per support unit with the support identity fields, an "
            f"integral count column named '{COUNT_COLUMN}', and geometry"
        ),
        "final_output_ref": (
            "the subset of result-table rows answering the required output "
            "(for example the zero-count supports), with the same columns"
        ),
        "diagnostic_refs": (
            "retained intermediate outputs explaining excluded records; when "
            "point-in-support joining can drop records, retain them under the "
            "ref 'unmatched_points'"
        ),
    },
    "nearest": {
        "result_table_ref": (
            "one row per (source, equally nearest target) pair with columns "
            "source_id, target_id, distance_m, and the source point geometry"
        ),
        "final_output_ref": (
            "the geometry-bearing nearest-pairs output used for the answer map"
        ),
        "diagnostic_refs": (
            "retain sources without any target match under the ref "
            "'unmatched_sources'"
        ),
    },
}


class WorkflowPlanningService:
    """Ask the planning model to compose one complete workflow proposal."""

    def __init__(
        self,
        *,
        storage: ObjectStore,
        client: StructuredArtifactClient,
        tool_registry: ToolRegistry,
        abstraction_catalog: AbstractionCatalog | None = None,
    ) -> None:
        self._storage = storage
        self._client = client
        self._tool_registry = tool_registry
        self._abstractions = abstraction_catalog or AbstractionCatalog()
        self._repository = WorkflowDraftRepository(storage)

    def propose(
        self,
        interpretation: SupportedInterpretation,
        *,
        review_context: Mapping[str, object] | None = None,
        skeletons: tuple[AbstractWorkflow, ...] | None = None,
        case_examples: tuple[Mapping[str, object], ...] | None = None,
    ) -> WorkflowDraft:
        """Ask the planning model for one draft pinned to catalog and registry.

        The model composes the abstract workflow from the abstraction
        vocabulary and maps it onto allow-listed operations; nothing in
        the input pre-composes the workflow. ``review_context`` carries the
        previous failed draft plus its diagnostics on repair retries.
        ``skeletons`` (escalation only) adds enumerated well-typed abstract
        chains as candidates the model may adopt, adapt, or ignore.
        ``case_examples`` adds accepted workflows from structurally similar
        past tasks as worked examples.
        """

        catalog = CatalogReader(self._storage).get(interpretation.catalog_version)
        layers = {
            (layer.dataset_id, layer.layer_id): layer
            for layer in catalog.layers
        }
        family = task_family(interpretation.task_specification)
        input_document: dict[str, object] = {
            "catalog_version": catalog.version,
            "tool_registry_version": self._tool_registry.version,
            "task_family": family,
            "task_specification": task_specification_document(
                interpretation.task_specification
            ),
            "output_contract": dict(FAMILY_OUTPUT_CONTRACTS[family]),
            "data_bindings": [
                {
                    **_binding_document(item),
                    "layer": _planning_layer_document(
                        layers[(item.dataset_id, item.layer_id)]
                    ),
                }
                for item in interpretation.bindings
            ],
            "template_placeholders": [
                f"{item.capability_input_ref}_retrieved_at"
                for item in interpretation.bindings
            ],
            "abstractions": [
                _signature_document(signature)
                for signature in self._abstractions.vector_signatures()
            ],
            "operations": [
                _algorithm_document(algorithm)
                for algorithm in self._tool_registry.algorithms
            ],
        }
        if skeletons is not None:
            input_document["composition_candidates"] = [
                _abstract_document(skeleton) for skeleton in skeletons
            ]
        if case_examples is not None:
            input_document["case_examples"] = list(case_examples)
        if review_context is not None:
            input_document["review_context"] = review_context
        artifact = self._client.generate(
            ArtifactRequest(
                contract=ArtifactContract.WORKFLOW_PLANNING,
                input_text=canonical_json(input_document).decode(),
            )
        )
        abstract = _ABSTRACT.validate_python(artifact.data["abstract_workflow"])
        concrete = _CONCRETE.validate_python(artifact.data["concrete_workflow"])
        # The draft id is the hash of its content, so build the draft once
        # with a placeholder id and then stamp the computed identity.
        provisional = WorkflowDraft(
            draft_id="",
            catalog_version=catalog.version,
            tool_registry_version=self._tool_registry.version,
            provenance=artifact.provenance,
            task_specification=TaskSpecificationArtifact(
                value=interpretation.task_specification,
                provenance=interpretation.provenance,
            ),
            data_bindings=DataBindingsArtifact(
                value=interpretation.bindings,
                provenance=interpretation.provenance,
            ),
            abstract_workflow=AbstractWorkflowArtifact(
                value=abstract,
                provenance=artifact.provenance,
            ),
            concrete_workflow=ConcreteWorkflowArtifact(
                value=concrete,
                provenance=artifact.provenance,
            ),
        )
        draft = sealed_draft(provisional)
        self._repository.save(draft)
        return draft


class WorkflowValidator:
    """Deterministic Catalog, semantic-type, registry, and dataflow checks.

    Every check is blocking: a proposal passes only when its bindings pin
    real catalog data, its abstract composition type-checks against the
    CCD/CCT vocabulary, and its concrete steps satisfy the operation contracts.
    """

    def __init__(
        self,
        *,
        storage: ObjectStore,
        tool_registry: ToolRegistry,
        abstraction_catalog: AbstractionCatalog | None = None,
    ) -> None:
        self._catalog_reader = CatalogReader(storage)
        self._tool_registry = tool_registry
        self._abstractions = abstraction_catalog or AbstractionCatalog()
        self._repository = WorkflowDraftRepository(storage)

    def validate(self, draft: WorkflowDraft) -> ValidationResult:
        """Run every deterministic check, persist and return the result."""

        diagnostics: list[ValidationDiagnostic] = []
        if draft.tool_registry_version != self._tool_registry.version:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.STRUCTURAL_ERROR,
                    "Draft Tool Registry version does not match the validator.",
                    ArtifactKind.CONCRETE_WORKFLOW,
                )
            )
        try:
            catalog = self._catalog_reader.get(draft.catalog_version)
        except LookupError:
            catalog = None
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.INVENTED_DATA,
                    f"Pinned Catalog version is unavailable: {draft.catalog_version}.",
                    ArtifactKind.DATA_BINDINGS,
                )
            )
        layers: dict[str, CatalogLayer] = {}
        if catalog is not None:
            layers = self._validate_bindings(draft, catalog.layers, diagnostics)
        self._validate_abstract(draft, diagnostics)
        self._validate_concrete(draft, diagnostics)
        self._validate_task_constraints(draft, diagnostics)
        self._validate_dataflow_grouping(draft, diagnostics)
        self._validate_crs_and_coverage(layers, diagnostics)
        self._validate_semantic_composition(draft, layers, diagnostics)

        if any(
            item.severity is ValidationSeverity.BLOCKING
            for item in diagnostics
        ):
            status = ValidationStatus.FAIL
        elif diagnostics:
            status = ValidationStatus.PASS_WITH_WARNINGS
        else:
            status = ValidationStatus.PASS
        provisional = ValidationResult(
            validation_id="",
            draft_id=draft.draft_id,
            status=status,
            diagnostics=tuple(diagnostics),
        )
        result = replace(
            provisional,
            validation_id=f"sha256:{sha256(_VALIDATION.dump_json(provisional))}",
        )
        self._repository.save_validation(result)
        return result

    @staticmethod
    def _validate_bindings(
        draft: WorkflowDraft,
        catalog_layers: tuple[CatalogLayer, ...],
        diagnostics: list[ValidationDiagnostic],
    ) -> dict[str, CatalogLayer]:
        """Check bindings against the pinned Catalog; return resolved layers by ref."""

        indexed = {
            (layer.dataset_id, layer.layer_id): layer for layer in catalog_layers
        }
        task = draft.task_specification.value
        expected_roles = task_roles(task)
        resolved: dict[str, CatalogLayer] = {}
        for binding in draft.data_bindings.value:
            if not (
                binding.topical_relevance.passed
                and binding.analytical_compatibility.passed
            ):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.INCOMPATIBLE_TYPE,
                        "Data binding has a failed matching assessment.",
                        ArtifactKind.DATA_BINDINGS,
                        ref=binding.capability_input_ref,
                    )
                )
            # A binding must pin the exact layer bytes: any version or hash
            # drift means the model referenced data the catalog cannot vouch for.
            layer = indexed.get((binding.dataset_id, binding.layer_id))
            if (
                layer is None
                or layer.dataset_version != binding.dataset_version
                or layer.content_hash != binding.content_hash
                or binding.catalog_version != draft.catalog_version
            ):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.INVENTED_DATA,
                        "Data binding does not resolve to its pinned Catalog Layer: "
                        f"{binding.dataset_id}/{binding.layer_id}.",
                        ArtifactKind.DATA_BINDINGS,
                        ref=binding.capability_input_ref,
                    )
                )
                continue
            resolved[binding.capability_input_ref] = layer
            # The layer plus the task's identity attributes must carry
            # resolved semantic annotations before it may be executed on.
            identity_fields = role_identity_fields(task, binding.role)
            attributes = (
                {}
                if layer.enriched is None
                else {
                    attribute.name: attribute
                    for attribute in layer.enriched.attributes
                }
            )
            layer_annotations_resolved = (
                layer.enriched is not None
                and layer.enriched.semantic_label.status
                is AnnotationStatus.RESOLVED
                and layer.enriched.ccd_meaning.status
                is AnnotationStatus.RESOLVED
            )
            missing_attributes = set(identity_fields) - set(attributes)
            attribute_annotations_resolved = all(
                data_binding_attribute_is_resolved(attributes[name])
                for name in identity_fields
                if name in attributes
            )
            if (
                not layer_annotations_resolved
                or missing_attributes
                or not attribute_annotations_resolved
            ):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.UNRESOLVED_ANNOTATION,
                        "Bound Catalog Layer has unresolved required annotations: "
                        f"{binding.dataset_id}/{binding.layer_id}.",
                        ArtifactKind.DATA_BINDINGS,
                        ref=binding.capability_input_ref,
                    )
                )
            if not set(identity_fields).issubset(layer.source_identity_fields):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.INCOMPATIBLE_TYPE,
                        "Catalog source identity fields do not cover the Task "
                        "Specification.",
                        ArtifactKind.DATA_BINDINGS,
                        ref=binding.capability_input_ref,
                    )
                )
        expected_refs = {role.value for role in expected_roles}
        role_refs = {
            item.role: item.capability_input_ref
            for item in draft.data_bindings.value
        }
        if set(role_refs.values()) != expected_refs or set(role_refs) != set(
            expected_roles
        ):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.STRUCTURAL_ERROR,
                    f"Data bindings must provide refs {sorted(expected_refs)}.",
                    ArtifactKind.DATA_BINDINGS,
                )
            )
        return resolved

    def _validate_abstract(
        self,
        draft: WorkflowDraft,
        diagnostics: list[ValidationDiagnostic],
    ) -> None:
        """Check the abstract workflow's structure and vocabulary."""

        workflow = draft.abstract_workflow.value
        available = {
            binding.capability_input_ref
            for binding in draft.data_bindings.value
        }
        step_ids: set[str] = set()
        output_refs: set[str] = set()
        for step in workflow.steps:
            if step.step_id in step_ids:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.STRUCTURAL_ERROR,
                        f"Abstract step id is duplicated: {step.step_id}.",
                        ArtifactKind.ABSTRACT_WORKFLOW,
                        step_id=step.step_id,
                    )
                )
            step_ids.add(step.step_id)
            if not self._abstractions.has_abstraction(URIRef(step.abstraction_id)):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.UNAVAILABLE_ALGORITHM,
                        "Abstract step uses an abstraction outside the "
                        f"vocabulary: {step.abstraction_id}.",
                        ArtifactKind.ABSTRACT_WORKFLOW,
                        step_id=step.step_id,
                    )
                )
            for ref in step.input_refs:
                if ref not in available:
                    diagnostics.append(
                        _diagnostic(
                            DiagnosticCode.DISCONNECTED_REFERENCE,
                            f"Abstract input ref is unavailable: {ref}.",
                            ArtifactKind.ABSTRACT_WORKFLOW,
                            step_id=step.step_id,
                            ref=ref,
                        )
                    )
            if step.output_ref in output_refs or step.output_ref in {
                binding.capability_input_ref
                for binding in draft.data_bindings.value
            }:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.STRUCTURAL_ERROR,
                        f"Abstract output ref is duplicated: {step.output_ref}.",
                        ArtifactKind.ABSTRACT_WORKFLOW,
                        step_id=step.step_id,
                        ref=step.output_ref,
                    )
                )
            output_refs.add(step.output_ref)
            available.add(step.output_ref)
        if not workflow.steps or workflow.final_output_ref not in output_refs:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.DISCONNECTED_REFERENCE,
                    "Abstract final output ref is not produced by any step.",
                    ArtifactKind.ABSTRACT_WORKFLOW,
                    ref=workflow.final_output_ref,
                )
            )

    def _validate_concrete(
        self,
        draft: WorkflowDraft,
        diagnostics: list[ValidationDiagnostic],
    ) -> None:
        """Check each concrete step against its allow-listed operation contract."""

        workflow = draft.concrete_workflow.value
        available = {
            binding.capability_input_ref
            for binding in draft.data_bindings.value
        }
        placeholders = {
            f"{binding.capability_input_ref}_retrieved_at"
            for binding in draft.data_bindings.value
        }
        abstract_ids = {step.step_id for step in draft.abstract_workflow.value.steps}
        step_ids: set[str] = set()
        produced: set[str] = set()
        sink_refs: set[str] = set()
        for step in workflow.steps:
            if step.step_id in step_ids:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.STRUCTURAL_ERROR,
                        f"Concrete step id is duplicated: {step.step_id}.",
                        ArtifactKind.CONCRETE_WORKFLOW,
                        step_id=step.step_id,
                    )
                )
            step_ids.add(step.step_id)
            if step.abstract_step_id not in abstract_ids:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.DISCONNECTED_REFERENCE,
                        f"Concrete step references unknown abstract step "
                        f"{step.abstract_step_id}.",
                        ArtifactKind.CONCRETE_WORKFLOW,
                        step_id=step.step_id,
                        ref=step.abstract_step_id,
                    )
                )
            try:
                contract = self._tool_registry.algorithm(step.algorithm_id)
            except CapabilityNotExecutableError:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.UNAVAILABLE_ALGORITHM,
                        f"Algorithm is unavailable: {step.algorithm_id}.",
                        ArtifactKind.CONCRETE_WORKFLOW,
                        step_id=step.step_id,
                    )
                )
                continue
            self._validate_step_parameters(
                step,
                contract,
                available,
                placeholders,
                diagnostics,
            )
            self._validate_step_outputs(step, contract, produced, diagnostics)
            available.update(output.ref for output in step.outputs)
            sink_refs.update(
                output.ref for output in step.outputs if output.kind == "sink"
            )
        required_outputs = {
            workflow.final_output_ref,
            workflow.result_table_ref,
            *workflow.diagnostic_refs,
        }
        for ref in sorted(required_outputs - produced):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.DISCONNECTED_REFERENCE,
                    f"Required concrete output ref is not produced: {ref}.",
                    ArtifactKind.CONCRETE_WORKFLOW,
                    ref=ref,
                )
            )
        # Only sink outputs are files that can be retained and published;
        # declaring a result-kind ref here would fail at execution time.
        for ref in sorted((required_outputs & produced) - sink_refs):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.DISCONNECTED_REFERENCE,
                    f"Retained output ref must be a sink output: {ref}.",
                    ArtifactKind.CONCRETE_WORKFLOW,
                    ref=ref,
                )
            )

    @staticmethod
    def _validate_step_parameters(
        step: ConcreteWorkflowStep,
        contract: OperationContract,
        available: set[str],
        placeholders: set[str],
        diagnostics: list[ValidationDiagnostic],
    ) -> None:
        declared = {parameter.name: parameter for parameter in contract.parameters}
        bound = {parameter.name: parameter for parameter in step.parameters}
        if len(bound) != len(step.parameters):
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.INVALID_PARAMETER,
                    f"{step.step_id} binds a parameter more than once.",
                    ArtifactKind.CONCRETE_WORKFLOW,
                    step_id=step.step_id,
                )
            )
        for parameter in contract.parameters:
            if parameter.required and parameter.name not in bound:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.MISSING_PARAMETER,
                        f"Required parameter is missing: {parameter.name}.",
                        ArtifactKind.CONCRETE_WORKFLOW,
                        step_id=step.step_id,
                    )
                )
        for name, binding in bound.items():
            declared_parameter = declared.get(name)
            if declared_parameter is None:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.INVALID_PARAMETER,
                        f"Parameter is not in the algorithm contract: {name}.",
                        ArtifactKind.CONCRETE_WORKFLOW,
                        step_id=step.step_id,
                    )
                )
                continue
            if declared_parameter.role == "data_binding":
                if binding.source != "ref" or binding.value not in available:
                    diagnostics.append(
                        _diagnostic(
                            DiagnosticCode.DISCONNECTED_REFERENCE,
                            f"{name} must bind an available data ref; got "
                            f"{binding.value!r}.",
                            ArtifactKind.CONCRETE_WORKFLOW,
                            step_id=step.step_id,
                            ref=str(binding.value),
                        )
                    )
                continue
            if binding.source == "ref":
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.INVALID_PARAMETER,
                        f"{name} is scalar configuration and cannot bind a ref.",
                        ArtifactKind.CONCRETE_WORKFLOW,
                        step_id=step.step_id,
                    )
                )
            elif (
                binding.source == "literal"
                and declared_parameter.allowed_values is not None
                and binding.value not in declared_parameter.allowed_values
            ):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.INVALID_PARAMETER,
                        f"{name} has disallowed value {binding.value!r}.",
                        ArtifactKind.CONCRETE_WORKFLOW,
                        step_id=step.step_id,
                    )
                )
            elif binding.source == "template":
                keys = _template_keys(binding.value)
                if keys is None or not keys or not keys.issubset(placeholders):
                    diagnostics.append(
                        _diagnostic(
                            DiagnosticCode.INVALID_PARAMETER,
                            f"{name} uses unknown template placeholders; "
                            f"available: {sorted(placeholders)}.",
                            ArtifactKind.CONCRETE_WORKFLOW,
                            step_id=step.step_id,
                        )
                    )

    @staticmethod
    def _validate_step_outputs(
        step: ConcreteWorkflowStep,
        contract: OperationContract,
        produced: set[str],
        diagnostics: list[ValidationDiagnostic],
    ) -> None:
        declared = {output.name: output for output in contract.outputs}
        for output in step.outputs:
            expected = declared.get(output.name)
            if expected is None:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.STRUCTURAL_ERROR,
                        f"Output is not in the algorithm contract: {output.name}.",
                        ArtifactKind.CONCRETE_WORKFLOW,
                        step_id=step.step_id,
                    )
                )
                continue
            expected_kind = "sink" if expected.value_type == "sink" else "result"
            if output.kind != expected_kind:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.STRUCTURAL_ERROR,
                        f"{output.name} must be a {expected_kind} output.",
                        ArtifactKind.CONCRETE_WORKFLOW,
                        step_id=step.step_id,
                    )
                )
            if output.ref in produced:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.STRUCTURAL_ERROR,
                        f"Concrete output ref is duplicated: {output.ref}.",
                        ArtifactKind.CONCRETE_WORKFLOW,
                        step_id=step.step_id,
                        ref=output.ref,
                    )
                )
            produced.add(output.ref)

    @staticmethod
    def _validate_task_constraints(
        draft: WorkflowDraft,
        diagnostics: list[ValidationDiagnostic],
    ) -> None:
        """Declared task constraints must surface in the concrete plan.

        A nearest task with a maximum-distance cutoff whose plan never
        mentions the cutoff value silently answers a different question
        (the benchmark's dominant cutoff failure), so it blocks here where
        the repair loop can fix it.
        """

        task = draft.task_specification.value
        if not isinstance(task, NearestTaskSpecification):
            return
        cutoff = task.distance.maximum_distance_m
        if cutoff is None:
            return
        rendered = f"{cutoff:g}"
        for step in draft.concrete_workflow.value.steps:
            for parameter in step.parameters:
                value = parameter.value
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and float(value) == cutoff
                ):
                    return
                if isinstance(value, str) and rendered in value:
                    return
        diagnostics.append(
            _diagnostic(
                DiagnosticCode.MISSING_PARAMETER,
                f"Task Specification declares maximum_distance_m={rendered} "
                "but no concrete step applies it; bind max_distance on "
                "geopandas:sjoinnearest or filter the distance column.",
                ArtifactKind.CONCRETE_WORKFLOW,
            )
        )

    @staticmethod
    def _validate_dataflow_grouping(
        draft: WorkflowDraft,
        diagnostics: list[ValidationDiagnostic],
    ) -> None:
        """Check that each abstract step's dataflow matches its concrete group.

        Concrete steps are grouped by the abstract step they implement; the
        groups must appear in abstract order, a group's external ref inputs
        must cover exactly the abstract input refs (as a set — the order in
        which a concretization first touches its inputs is not semantic;
        positional typing is checked at the abstract level), and the
        group's last sink output is its result.
        """

        abstract_steps = draft.abstract_workflow.value.steps
        concrete_steps = draft.concrete_workflow.value.steps
        group_sequence = tuple(
            dict.fromkeys(step.abstract_step_id for step in concrete_steps)
        )
        expected_sequence = tuple(
            step.step_id
            for step in abstract_steps
            if any(c.abstract_step_id == step.step_id for c in concrete_steps)
        )
        if group_sequence != expected_sequence:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.STRUCTURAL_ERROR,
                    "Concrete steps are not grouped in abstract step order.",
                    ArtifactKind.CONCRETE_WORKFLOW,
                )
            )
        for abstract_step in abstract_steps:
            group = tuple(
                step
                for step in concrete_steps
                if step.abstract_step_id == abstract_step.step_id
            )
            if not group:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.STRUCTURAL_ERROR,
                        "Abstract step has no concrete implementation: "
                        f"{abstract_step.step_id}.",
                        ArtifactKind.CONCRETE_WORKFLOW,
                        step_id=abstract_step.step_id,
                    )
                )
                continue
            produced: set[str] = set()
            boundary_inputs: list[str] = []
            for concrete_step in group:
                for parameter in concrete_step.parameters:
                    if (
                        parameter.source == "ref"
                        and isinstance(parameter.value, str)
                        and parameter.value not in produced
                        and parameter.value not in boundary_inputs
                    ):
                        boundary_inputs.append(parameter.value)
                produced.update(output.ref for output in concrete_step.outputs)
            sink_outputs = [
                output.ref
                for output in group[-1].outputs
                if output.kind == "sink"
            ]
            if (
                set(abstract_step.input_refs) != set(boundary_inputs)
                or not sink_outputs
                or abstract_step.output_ref not in sink_outputs
            ):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.DISCONNECTED_REFERENCE,
                        "Abstract step dataflow does not match its concrete "
                        "step group.",
                        ArtifactKind.ABSTRACT_WORKFLOW,
                        step_id=abstract_step.step_id,
                        ref=abstract_step.output_ref,
                    )
                )

    @staticmethod
    def _validate_crs_and_coverage(
        layers: dict[str, CatalogLayer],
        diagnostics: list[ValidationDiagnostic],
    ) -> None:
        """Check canonical CRS and spatial/temporal coverage of the inputs."""

        for ref, layer in layers.items():
            if layer.crs != CANONICAL_CRS:
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.CRS_CONFLICT,
                        f"{ref} uses {layer.crs}; the pipeline requires "
                        f"{CANONICAL_CRS}.",
                        ArtifactKind.DATA_BINDINGS,
                        ref=ref,
                    )
                )
        bound_layers = tuple(layers.values())
        for first, second in combinations(bound_layers, 2):
            if (
                first.spatial_extent is None
                or second.spatial_extent is None
                or not _bbox_intersects(
                    first.spatial_extent,
                    second.spatial_extent,
                )
            ):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.COVERAGE_CONFLICT,
                        "Bound Catalog Layers do not have overlapping spatial coverage.",
                        ArtifactKind.DATA_BINDINGS,
                    )
                )
            if not _temporal_extents_overlap(first, second):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.COVERAGE_CONFLICT,
                        "Bound Catalog Layers do not have overlapping temporal coverage.",
                        ArtifactKind.DATA_BINDINGS,
                    )
                )

    def _validate_semantic_composition(
        self,
        draft: WorkflowDraft,
        layers: dict[str, CatalogLayer],
        diagnostics: list[ValidationDiagnostic],
    ) -> None:
        """Type-check the proposed composition against the CCD/CCT vocabulary.

        CCD types of each input must be subtypes of the abstraction's
        declared inputs, with declared output types propagating to
        intermediate refs; CCT types are inferred step by step through the
        DAG. Both checks are blocking: a workflow that does not type-check
        is not executable.
        """

        ccd_ref_types: dict[str, frozenset[URIRef]] = {}
        cct_ref_types: dict[str, TypeInstance] = {}
        for ref, layer in layers.items():
            ccd_ref_types[ref] = bound_ccd_types(layer)
            cct_type = _bound_cct_type(layer)
            if cct_type is not None:
                cct_ref_types[ref] = cct_type

        for step in draft.abstract_workflow.value.steps:
            abstraction = URIRef(step.abstraction_id)
            if not self._abstractions.has_abstraction(abstraction):
                continue  # already a blocking vocabulary diagnostic
            declared_inputs = self._abstractions.declared_input_types(abstraction)
            if len(step.input_refs) != len(declared_inputs):
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.INCOMPATIBLE_TYPE,
                        f"{step.abstraction_id} takes {len(declared_inputs)} "
                        f"input(s); the step wires {len(step.input_refs)}.",
                        ArtifactKind.ABSTRACT_WORKFLOW,
                        step_id=step.step_id,
                    )
                )
                continue
            for input_index, (ref, expected_types) in enumerate(
                zip(step.input_refs, declared_inputs, strict=True),
                start=1,
            ):
                actual_types = ccd_ref_types.get(ref)
                if actual_types is None:
                    continue
                actual = Polytype.project(ccd.dimensions, actual_types).root_empty()
                expected = Polytype.project(
                    ccd.dimensions,
                    expected_types,
                ).root_empty()
                # Bidirectional compatibility: a subtype satisfies the input
                # directly; a supertype (e.g. a plain region feeding a
                # tessellation input) is a conservative upcast and passes.
                # Only dimensionally incomparable types block the workflow.
                if not actual.subtype(expected) and not expected.subtype(actual):
                    diagnostics.append(
                        _diagnostic(
                            DiagnosticCode.INCOMPATIBLE_TYPE,
                            f"Input {input_index} of {step.step_id} has CCD types "
                            f"{sorted(_ccd_names(actual_types))}, but "
                            f"{step.abstraction_id} requires "
                            f"{sorted(_ccd_names(expected_types))}.",
                            ArtifactKind.ABSTRACT_WORKFLOW,
                            step_id=step.step_id,
                            ref=ref,
                        )
                    )
            ccd_ref_types[step.output_ref] = (
                self._abstractions.declared_output_types(abstraction)
            )

            # CCT inference: check inputs we have evidence for; fall back to
            # the abstraction's declared defaults when evidence is missing.
            actual_cct_inputs = [cct_ref_types.get(ref) for ref in step.input_refs]
            if any(value is None for value in actual_cct_inputs):
                inferred = self._abstractions.infer(abstraction)
            else:
                inferred = self._abstractions.infer(
                    abstraction,
                    *cast(list[TypeInstance], actual_cct_inputs),
                )
            if not inferred.passed or inferred.inferred_type is None:
                message = (
                    inferred.diagnostics[0].message
                    if inferred.diagnostics
                    else "CCT inference failed for the proposed workflow step."
                )
                diagnostics.append(
                    _diagnostic(
                        DiagnosticCode.INCOMPATIBLE_TYPE,
                        message,
                        ArtifactKind.ABSTRACT_WORKFLOW,
                        step_id=step.step_id,
                    )
                )
                continue
            cct_ref_types[step.output_ref] = cct.parse_type(inferred.inferred_type)


def _template_keys(value: object) -> set[str] | None:
    """Extract the placeholder names a template parameter references."""

    if not isinstance(value, str):
        return None
    try:
        return {
            name
            for _, name, _, _ in Formatter().parse(value)
            if name is not None
        }
    except ValueError:
        return None


def _ccd_names(types: frozenset[URIRef]) -> tuple[str, ...]:
    return tuple(str(item).rsplit("#", 1)[-1] for item in types)


def _binding_document(binding: DataBinding) -> dict[str, object]:
    return {
        "role": binding.role.value,
        "capability_input_ref": binding.capability_input_ref,
        "catalog_version": binding.catalog_version,
        "dataset_id": binding.dataset_id,
        "layer_id": binding.layer_id,
        "dataset_version": binding.dataset_version,
        "content_hash": binding.content_hash,
        "topical_relevance": _assessment_document(binding.topical_relevance),
        "analytical_compatibility": _assessment_document(
            binding.analytical_compatibility
        ),
        "analytical_ccd_meaning": binding.analytical_ccd_meaning,
    }


def _assessment_document(value: MatchAssessment) -> dict[str, object]:
    return {"passed": value.passed, "reasons": list(value.reasons)}


def _abstract_document(workflow: AbstractWorkflow) -> dict[str, object]:
    return {
        "steps": [
            {
                "step_id": step.step_id,
                "abstraction_id": step.abstraction_id,
                "input_refs": list(step.input_refs),
                "output_ref": step.output_ref,
            }
            for step in workflow.steps
        ],
        "final_output_ref": workflow.final_output_ref,
    }


def _concrete_document(workflow: ConcreteWorkflow) -> dict[str, object]:
    return {
        "steps": [
            {
                "step_id": step.step_id,
                "abstract_step_id": step.abstract_step_id,
                "algorithm_id": step.algorithm_id,
                "parameters": [_parameter_document(item) for item in step.parameters],
                "outputs": [_output_document(item) for item in step.outputs],
            }
            for step in workflow.steps
        ],
        "final_output_ref": workflow.final_output_ref,
        "result_table_ref": workflow.result_table_ref,
        "diagnostic_refs": list(workflow.diagnostic_refs),
    }


def _parameter_document(value: ParameterBinding) -> dict[str, object]:
    return {"name": value.name, "source": value.source, "value": value.value}


def _output_document(value: OutputBinding) -> dict[str, object]:
    return {"name": value.name, "ref": value.ref, "kind": value.kind}


def _diagnostic_document(value: ValidationDiagnostic) -> dict[str, object]:
    return {
        "code": value.code.value,
        "message": value.message,
        "artifact": value.artifact.value,
        "step_id": value.step_id,
        "ref": value.ref,
        "severity": value.severity.value,
    }


def planning_repair_context_document(
    draft: WorkflowDraft,
    validation: ValidationResult,
    *,
    attempt: int,
) -> dict[str, object]:
    """Serialize one failed proposal for the next planning attempt."""

    return {
        "attempt": attempt,
        "failed_draft": _DRAFT.dump_python(draft, mode="json"),
        "diagnostics": [
            _diagnostic_document(item) for item in validation.diagnostics
        ],
    }


def _diagnostic(
    code: DiagnosticCode,
    message: str,
    artifact: ArtifactKind,
    *,
    severity: ValidationSeverity = ValidationSeverity.BLOCKING,
    step_id: str | None = None,
    ref: str | None = None,
) -> ValidationDiagnostic:
    return ValidationDiagnostic(
        code=code,
        message=message,
        artifact=artifact,
        severity=severity,
        step_id=step_id,
        ref=ref,
    )


def _planning_layer_document(layer: CatalogLayer) -> dict[str, object]:
    """Describe one bound layer for the planner: schema, semantics, timing."""

    enriched = layer.enriched
    return {
        "dataset_id": layer.dataset_id,
        "layer_id": layer.layer_id,
        "crs": layer.crs,
        "geometry_types": list(layer.vector.geometry_types),
        "feature_count": layer.vector.feature_count,
        "source_identity_fields": list(layer.source_identity_fields),
        "retrieved_at": layer.raw.provenance.retrieved_at.isoformat(),
        "ccd_meaning": (
            None if enriched is None else enriched.ccd_meaning.value
        ),
        "semantic_label": (
            None if enriched is None else enriched.semantic_label.value
        ),
        "attributes": [
            {
                "name": attribute.name,
                "storage_type": attribute.storage_type,
                "sample_values": list(attribute.sample_values[:3]),
            }
            for attribute in layer.vector.attributes
        ],
    }


def _signature_document(signature: AbstractionSignature) -> dict[str, object]:
    return {
        "abstraction_id": signature.abstraction_id,
        "name": signature.name,
        "description": signature.description,
        "input_ccd_types": [list(types) for types in signature.input_ccd_types],
        "output_ccd_types": list(signature.output_ccd_types),
        "cct_expression": signature.cct_expression,
    }


def _algorithm_document(algorithm: OperationContract) -> dict[str, object]:
    return {
        "algorithm_id": algorithm.algorithm_id,
        "parameters": [
            {
                "name": parameter.name,
                "value_type": parameter.value_type,
                "required": parameter.required,
                "role": parameter.role,
                "default": parameter.default,
                "allowed_values": (
                    None
                    if parameter.allowed_values is None
                    else list(parameter.allowed_values)
                ),
            }
            for parameter in algorithm.parameters
        ],
        "outputs": [
            {
                "name": output.name,
                "value_type": output.value_type,
                "required": output.required,
                "effect": output.effect,
            }
            for output in algorithm.outputs
        ],
    }


def bound_ccd_types(layer: CatalogLayer) -> frozenset[URIRef]:
    """Project a bound layer's annotations onto CCD types for subtype checks."""

    if layer.enriched is None or layer.enriched.ccd_meaning.value is None:
        return frozenset()
    # Catalog dataset-semantics vocabulary -> CCD core concept of the layer.
    layer_meanings = {
        "ObjectDS": CCD.ObjectQ,
        "EventDS": CCD.EventQ,
        "NetworkDS": CCD.NetworkQ,
        "PointMeasuresDS": CCD.FieldQ,
        "CoverageDS": CCD.FieldQ,
        "LatticeDS": CCD.ObjectQ,
        "PatchDS": CCD.FieldQ,
        "ContourDS": CCD.FieldQ,
    }
    layer_type = layer_meanings.get(layer.enriched.ccd_meaning.value)
    if layer_type is None:
        return frozenset()
    types = {layer_type}
    geometry_type = _geometry_representation(layer)
    if geometry_type is not None:
        types.add(geometry_type)
    attributes = {
        attribute.name: attribute for attribute in layer.enriched.attributes
    }
    for name in layer.source_identity_fields:
        attribute = attributes.get(name)
        if attribute is not None and attribute.ccd_meaning.value is not None:
            types.add(CCD[attribute.ccd_meaning.value])
    return frozenset(types)


def _geometry_representation(layer: CatalogLayer) -> URIRef | None:
    """Derive the CCD geometry representation from the layer itself."""

    if layer.enriched is not None and layer.enriched.ccd_meaning.value == "LatticeDS":
        return CCD.VectorTessellationA
    geometries = set(layer.vector.geometry_types)
    if geometries <= {"Point", "MultiPoint"}:
        return CCD.PointA
    if geometries <= {"LineString", "MultiLineString"}:
        return CCD.LineA
    if geometries <= {"Polygon", "MultiPolygon"}:
        return CCD.VectorRegionA
    return None


def _bound_cct_type(layer: CatalogLayer) -> TypeInstance | None:
    """Derive the CCT input type ObjectInfo(<scale>) from identity-field scales.

    Returns None (no evidence, advisory diagnostic upstream) unless the
    identity fields agree on exactly one measurement scale.
    """

    if (
        layer.enriched is None
        or layer.enriched.ccd_meaning.value
        not in {LayerCCDMeaning.OBJECT, LayerCCDMeaning.LATTICE}
    ):
        return None
    attributes = {
        attribute.name: attribute for attribute in layer.enriched.attributes
    }
    scales = {
        attribute.ccd_meaning.value
        for name in layer.source_identity_fields
        if (attribute := attributes.get(name)) is not None
        and attribute.ccd_meaning.value is not None
    }
    scale_names = {
        "BooleanA": "Bool",
        "NominalA": "Nom",
        "OrdinalA": "Ord",
        "IntervalA": "Itv",
        "RatioA": "Ratio",
        "CountA": "Count",
        "ERA": "Ratio",
        "IRA": "Ratio",
    }
    if len(scales) != 1:
        return None
    scale = scale_names.get(next(iter(scales)))
    if scale is None:
        return None
    return cct.parse_type(f"ObjectInfo({scale})")


def _bbox_intersects(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    # Extents are (min_x, min_y, max_x, max_y) in a shared CRS.
    return not (
        left[2] < right[0]
        or right[2] < left[0]
        or left[3] < right[1]
        or right[3] < left[1]
    )


def _temporal_extents_overlap(left: CatalogLayer, right: CatalogLayer) -> bool:
    left_extent = left.temporal_extent
    right_extent = right.temporal_extent
    if left_extent.end is not None and left_extent.end < right_extent.start:
        return False
    if right_extent.end is not None and right_extent.end < left_extent.start:
        return False
    return True
