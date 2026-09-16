# SPDX-License-Identifier: GPL-3.0-only

"""Deterministic Catalog, registry, dataflow and column-flow validation of drafts."""

from __future__ import annotations


from dataclasses import replace
from itertools import combinations
from string import Formatter


from data_pipeline.catalog import CatalogReader
from data_pipeline.models import AnnotationStatus, CatalogLayer
from data_pipeline.serialization import sha256
from data_pipeline.storage import ObjectStore
from data_pipeline.geoparquet import CANONICAL_CRS
from geoqa_agent.governance import (
    data_binding_attribute_is_resolved,
    result_contract,
)
from geoqa_agent import column_flow
from geoqa_agent.tool_registry import (
    CapabilityNotExecutableError,
    OperationContract,
    ToolRegistry,
)


from geoqa_agent.workflow_models import (
    ArtifactKind,
    ConcreteWorkflowStep,
    DiagnosticCode,
    VALIDATION_ADAPTER,
    ValidationDiagnostic,
    ValidationResult,
    ValidationStatus,
    WorkflowDraft,
    WorkflowDraftRepository,
)


class WorkflowValidator:
    """Deterministic Catalog, registry, dataflow and column-flow checks.

    Every check is blocking: a proposal passes only when its bindings pin
    real catalog data, its steps satisfy the operation contracts, its refs
    connect, and the columns it names exist with the scales it needs.
    """

    def __init__(
        self,
        *,
        storage: ObjectStore,
        tool_registry: ToolRegistry,
    ) -> None:
        self._catalog_reader = CatalogReader(storage)
        self._tool_registry = tool_registry
        self._repository = WorkflowDraftRepository(storage)

    def validate(self, draft: WorkflowDraft) -> ValidationResult:
        """Run every deterministic check, persist and return the result."""

        diagnostics: list[ValidationDiagnostic] = []
        if draft.tool_registry_version != self._tool_registry.version:
            diagnostics.append(
                ValidationDiagnostic(
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
                ValidationDiagnostic(
                    DiagnosticCode.INVENTED_DATA,
                    f"Pinned Catalog version is unavailable: {draft.catalog_version}.",
                    ArtifactKind.DATA_BINDINGS,
                )
            )
        layers: dict[str, CatalogLayer] = {}
        if catalog is not None:
            layers = self._validate_bindings(draft, catalog.layers, diagnostics)
        self._validate_concrete(draft, diagnostics)
        self._validate_task_constraints(draft, diagnostics)
        self._validate_aggregation(draft, layers, diagnostics)
        self._validate_column_flow(draft, layers, diagnostics)
        self._validate_crs_and_coverage(layers, diagnostics)

        status = ValidationStatus.FAIL if diagnostics else ValidationStatus.PASS
        provisional = ValidationResult(
            validation_id="",
            draft_id=draft.draft_id,
            status=status,
            diagnostics=tuple(diagnostics),
        )
        result = replace(
            provisional,
            validation_id=f"sha256:{sha256(VALIDATION_ADAPTER.dump_json(provisional))}",
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
        expected_roles = tuple(role.role for role in task.roles)
        resolved: dict[str, CatalogLayer] = {}
        for binding in draft.data_bindings.value:
            if not (
                binding.topical_relevance.passed
                and binding.analytical_compatibility.passed
            ):
                diagnostics.append(
                    ValidationDiagnostic(
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
                    ValidationDiagnostic(
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
            identity_fields = task.role(binding.role).identity_fields
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
                    ValidationDiagnostic(
                        DiagnosticCode.UNRESOLVED_ANNOTATION,
                        "Bound Catalog Layer has unresolved required annotations: "
                        f"{binding.dataset_id}/{binding.layer_id}.",
                        ArtifactKind.DATA_BINDINGS,
                        ref=binding.capability_input_ref,
                    )
                )
            if not set(identity_fields).issubset(layer.source_identity_fields):
                diagnostics.append(
                    ValidationDiagnostic(
                        DiagnosticCode.INCOMPATIBLE_TYPE,
                        "Catalog source identity fields do not cover the Task "
                        "Specification.",
                        ArtifactKind.DATA_BINDINGS,
                        ref=binding.capability_input_ref,
                    )
                )
        expected_refs = set(expected_roles)
        role_refs = {
            item.role: item.capability_input_ref
            for item in draft.data_bindings.value
        }
        if set(role_refs.values()) != expected_refs or set(role_refs) != set(
            expected_roles
        ):
            diagnostics.append(
                ValidationDiagnostic(
                    DiagnosticCode.STRUCTURAL_ERROR,
                    f"Data bindings must provide refs {sorted(expected_refs)}.",
                    ArtifactKind.DATA_BINDINGS,
                )
            )
        return resolved

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
        step_ids: set[str] = set()
        produced: set[str] = set()
        sink_refs: set[str] = set()
        for step in workflow.steps:
            if step.step_id in step_ids:
                diagnostics.append(
                    ValidationDiagnostic(
                        DiagnosticCode.STRUCTURAL_ERROR,
                        f"Concrete step id is duplicated: {step.step_id}.",
                        ArtifactKind.CONCRETE_WORKFLOW,
                        step_id=step.step_id,
                    )
                )
            step_ids.add(step.step_id)
            try:
                contract = self._tool_registry.algorithm(step.algorithm_id)
            except CapabilityNotExecutableError:
                diagnostics.append(
                    ValidationDiagnostic(
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
                ValidationDiagnostic(
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
                ValidationDiagnostic(
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
                ValidationDiagnostic(
                    DiagnosticCode.INVALID_PARAMETER,
                    f"{step.step_id} binds a parameter more than once.",
                    ArtifactKind.CONCRETE_WORKFLOW,
                    step_id=step.step_id,
                )
            )
        for parameter in contract.parameters:
            if parameter.required and parameter.name not in bound:
                diagnostics.append(
                    ValidationDiagnostic(
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
                    ValidationDiagnostic(
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
                        ValidationDiagnostic(
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
                    ValidationDiagnostic(
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
                    ValidationDiagnostic(
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
                        ValidationDiagnostic(
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
                    ValidationDiagnostic(
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
                    ValidationDiagnostic(
                        DiagnosticCode.STRUCTURAL_ERROR,
                        f"{output.name} must be a {expected_kind} output.",
                        ArtifactKind.CONCRETE_WORKFLOW,
                        step_id=step.step_id,
                    )
                )
            if output.ref in produced:
                diagnostics.append(
                    ValidationDiagnostic(
                        DiagnosticCode.STRUCTURAL_ERROR,
                        f"Concrete output ref is duplicated: {output.ref}.",
                        ArtifactKind.CONCRETE_WORKFLOW,
                        step_id=step.step_id,
                        ref=output.ref,
                    )
                )
            produced.add(output.ref)

    @staticmethod
    def _validate_aggregation(
        draft: WorkflowDraft,
        layers: dict[str, CatalogLayer],
        diagnostics: list[ValidationDiagnostic],
    ) -> None:
        """A sum, mean, min or max of a bound vector attribute must be taken
        by some step: a statistic literal naming that aggregation over that
        attribute. Counting records where the question sums an attribute
        answers a different question with a plausible table."""

        goal = draft.task_specification.value.goal
        attribute = goal.value_attribute
        if goal.aggregation not in {"sum", "mean", "min", "max"} or attribute is None:
            return
        if not any(
            layer.vector is not None
            and attribute in {item.name for item in layer.vector.attributes}
            for layer in layers.values()
        ):
            return  # a raster band or a derived value: the column flow decides
        for step in draft.concrete_workflow.value.steps:
            literals = {
                parameter.name: parameter.value
                for parameter in step.parameters
                if parameter.source == "literal"
            }
            if literals.get("statistic") == goal.aggregation and attribute in {
                literals.get("value_field"),
                literals.get("field"),
            }:
                return
        diagnostics.append(
            ValidationDiagnostic(
                DiagnosticCode.MISSING_PARAMETER,
                f"The goal is the {goal.aggregation} of {attribute!r} per unit, but "
                f"no step takes statistic={goal.aggregation} over that attribute "
                "(geopandas:countpointsinpolygon with value_field, or "
                "geopandas:aggregate with field); counting records is not it.",
                ArtifactKind.CONCRETE_WORKFLOW,
            )
        )

    @staticmethod
    def _validate_task_constraints(
        draft: WorkflowDraft,
        diagnostics: list[ValidationDiagnostic],
    ) -> None:
        """Declared task semantics must surface in the concrete plan.

        A goal declared as a selection must not hand the whole result table
        back as the final output, or every unit becomes the answer; every
        quantity the question states (a distance cutoff, a threshold) and
        every period (an imagery season) must appear as literals in some
        concrete parameter, or the plan silently answers a different
        question.
        """

        task = draft.task_specification.value
        concrete = draft.concrete_workflow.value
        if (
            task.goal.selection
            and concrete.final_output_ref == concrete.result_table_ref
        ):
            diagnostics.append(
                ValidationDiagnostic(
                    DiagnosticCode.STRUCTURAL_ERROR,
                    "The goal is a selection "
                    f"({', '.join(task.target_transformation)}) but "
                    "final_output_ref is the whole result table; add the "
                    "selection step (for example geopandas:filterbyexpression "
                    f"on {task.goal.value_name}) and point final_output_ref at "
                    "its output.",
                    ArtifactKind.CONCRETE_WORKFLOW,
                    ref=concrete.final_output_ref,
                )
            )
        literals = [
            parameter.value
            for step in concrete.steps
            for parameter in step.parameters
            if parameter.source != "ref"
        ]
        for quantity in task.quantities:
            rendered = f"{quantity.value:g}"
            if any(
                (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and float(value) == quantity.value
                )
                or (isinstance(value, str) and rendered in value)
                for value in literals
            ):
                continue
            diagnostics.append(
                ValidationDiagnostic(
                    DiagnosticCode.MISSING_PARAMETER,
                    f"Task Specification declares {quantity.name}={rendered}"
                    f"{'' if quantity.unit is None else ' ' + quantity.unit} but "
                    "no concrete step applies it; bind it as a parameter "
                    "literal or use it in a filter expression.",
                    ArtifactKind.CONCRETE_WORKFLOW,
                )
            )
        for period in task.periods:
            if all(
                any(isinstance(value, str) and date in value for value in literals)
                for date in (period.start, period.end)
            ):
                continue
            diagnostics.append(
                ValidationDiagnostic(
                    DiagnosticCode.MISSING_PARAMETER,
                    f"Task Specification declares the period {period.name} "
                    f"({period.start} to {period.end}) but no concrete step "
                    "applies both dates; bind them as start_date and end_date "
                    "of gee:filter or use them in a filter expression.",
                    ArtifactKind.CONCRETE_WORKFLOW,
                )
            )

    def _validate_column_flow(
        self,
        draft: WorkflowDraft,
        layers: dict[str, CatalogLayer],
        diagnostics: list[ValidationDiagnostic],
    ) -> None:
        """Check bound data kinds and geometries against what each operation
        accepts, field
        parameters and expressions against the columns that will exist when
        the step runs, numeric statistics against the scale of the column
        they aggregate, and the result table and final output against the
        goal's result contract.

        Column schemas flow ref by ref through the concrete steps exactly as
        the runner transforms frames; a step whose inputs or algorithm are
        already diagnosed is skipped, and so are the steps downstream of it.
        """

        schemas: dict[str, column_flow.Schema] = {
            ref: column_flow.layer_schema(layer) for ref, layer in layers.items()
        }
        for step in draft.concrete_workflow.value.steps:
            try:
                contract = self._tool_registry.algorithm(step.algorithm_id)
            except CapabilityNotExecutableError:
                continue
            bound = {parameter.name: parameter for parameter in step.parameters}
            sources: dict[str, column_flow.Schema] = {}
            literals: dict[str, object] = {
                parameter.name: parameter.default
                for parameter in contract.parameters
                if parameter.default is not None
            }
            complete = True
            for parameter in contract.parameters:
                binding = bound.get(parameter.name)
                if parameter.role == "data_binding":
                    if binding is None:
                        complete = complete and not parameter.required
                        continue
                    schema = schemas.get(str(binding.value))
                    if schema is None:
                        complete = False
                    else:
                        sources[parameter.name] = schema
                elif binding is not None and binding.value is not None:
                    literals[parameter.name] = binding.value
                elif parameter.required:
                    complete = False
            if not complete:
                continue

            for parameter in contract.parameters:
                if parameter.name not in sources:
                    continue
                bound_kind = column_flow.data_kind(sources[parameter.name])
                if bound_kind != parameter.data_kind:
                    diagnostics.append(
                        ValidationDiagnostic(
                            DiagnosticCode.INCOMPATIBLE_TYPE,
                            f"{parameter.name} binds {bound[parameter.name].value}, "
                            f"which is {bound_kind} data; {step.algorithm_id} needs "
                            f"{parameter.data_kind} data.",
                            ArtifactKind.CONCRETE_WORKFLOW,
                            step_id=step.step_id,
                            ref=str(bound[parameter.name].value),
                        )
                    )
                    continue
                kind = sources[parameter.name].get(column_flow.GEOMETRY)
                if (
                    bound_kind == "vector"
                    and parameter.geometry is not None
                    and kind is not None
                    and kind not in parameter.geometry
                ):
                    diagnostics.append(
                        ValidationDiagnostic(
                            DiagnosticCode.INCOMPATIBLE_TYPE,
                            f"{parameter.name} binds {bound[parameter.name].value}, "
                            f"whose geometry is {kind}; {step.algorithm_id} needs "
                            f"{' or '.join(parameter.geometry)}.",
                            ArtifactKind.CONCRETE_WORKFLOW,
                            step_id=step.step_id,
                            ref=str(bound[parameter.name].value),
                        )
                    )

            for (algorithm_id, name), source in column_flow.FIELD_SOURCES.items():
                if algorithm_id != step.algorithm_id or name not in literals:
                    continue
                for column in column_flow.fields(literals[name]):
                    if column not in sources[source]:
                        diagnostics.append(
                            ValidationDiagnostic(
                                DiagnosticCode.INVALID_PARAMETER,
                                f"{name}={column!r} is not a column of "
                                f"{bound[source].value}; it has "
                                f"{sorted(sources[source])}.",
                                ArtifactKind.CONCRETE_WORKFLOW,
                                step_id=step.step_id,
                                ref=str(bound[source].value),
                            )
                        )
            expression_source = column_flow.EXPRESSION_SOURCES.get(step.algorithm_id)
            if expression_source is not None and "expression" in literals:
                source, pseudo = expression_source
                columns = column_flow.expression_columns(str(literals["expression"]))
                known = set(sources[source]) | set(pseudo)
                for column in sorted((columns or set()) - known):
                    diagnostics.append(
                        ValidationDiagnostic(
                            DiagnosticCode.INVALID_PARAMETER,
                            f"expression references {column!r}, which is not a "
                            f"column of {bound[source].value}; it has "
                            f"{sorted(sources[source])}.",
                            ArtifactKind.CONCRETE_WORKFLOW,
                            step_id=step.step_id,
                            ref=str(bound[source].value),
                        )
                    )
            statistic_field = column_flow.STATISTIC_FIELDS.get(step.algorithm_id)
            if statistic_field is not None and "statistic" in literals:
                source, name = statistic_field
                required = column_flow.STATISTIC_SCALES.get(str(literals["statistic"]))
                value_column = (
                    None if literals.get(name) is None else str(literals[name])
                )
                if value_column is None and column_flow.data_kind(sources[source]) != "vector":
                    # A raster statistic defaults to the first band.
                    value_column = next(
                        (column for column in sources[source] if column != column_flow.GEOMETRY),
                        None,
                    )
                if required is not None and value_column is None:
                    diagnostics.append(
                        ValidationDiagnostic(
                            DiagnosticCode.INVALID_PARAMETER,
                            f"statistic={literals['statistic']} needs {name}.",
                            ArtifactKind.CONCRETE_WORKFLOW,
                            step_id=step.step_id,
                        )
                    )
                scale = None if value_column is None else sources[source].get(value_column)
                if required is not None and scale is not None and scale not in required:
                    diagnostics.append(
                        ValidationDiagnostic(
                            DiagnosticCode.INCOMPATIBLE_TYPE,
                            f"statistic={literals['statistic']} needs a column "
                            f"with scale in {sorted(required)}, but "
                            f"{value_column!r} is {scale}.",
                            ArtifactKind.CONCRETE_WORKFLOW,
                            step_id=step.step_id,
                        )
                    )

            effects = column_flow.apply(step.algorithm_id, sources, literals)
            for output in step.outputs:
                if output.kind == "sink" and output.name in effects:
                    schemas[output.ref] = effects[output.name]

        # The result table and the final output must carry the columns the
        # goal's result contract names; a plan that renames or copies
        # differently answers with a table nobody can read.
        workflow = draft.concrete_workflow.value
        expected = result_contract(draft.task_specification.value)
        for ref in dict.fromkeys((workflow.result_table_ref, workflow.final_output_ref)):
            schema = schemas.get(ref)
            if schema is None:
                continue
            missing = [column for column in expected.columns if column not in schema]
            if missing:
                collided = [
                    f"{column} was suffixed to {column}_left/_right because both "
                    "joined layers carry it; set prefix on the joined side"
                    for column in missing
                    if f"{column}_left" in schema
                ]
                diagnostics.append(
                    ValidationDiagnostic(
                        DiagnosticCode.STRUCTURAL_ERROR,
                        f"{ref} lacks the result-contract columns {missing}; it "
                        f"carries {sorted(schema)}. Name the key columns exactly "
                        f"{[item.column for item in expected.key_columns]} (rename "
                        "with geopandas:renamefield, or set prefix / "
                        "distance_field / field so the operation names them) and "
                        f"the value column {expected.value_column!r}."
                        + (" " + "; ".join(collided) + "." if collided else ""),
                        ArtifactKind.CONCRETE_WORKFLOW,
                        ref=ref,
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
                    ValidationDiagnostic(
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
                    ValidationDiagnostic(
                        DiagnosticCode.COVERAGE_CONFLICT,
                        "Bound Catalog Layers do not have overlapping spatial coverage.",
                        ArtifactKind.DATA_BINDINGS,
                    )
                )
            if not _temporal_extents_overlap(first, second):
                diagnostics.append(
                    ValidationDiagnostic(
                        DiagnosticCode.COVERAGE_CONFLICT,
                        "Bound Catalog Layers do not have overlapping temporal coverage.",
                        ArtifactKind.DATA_BINDINGS,
                    )
                )

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
