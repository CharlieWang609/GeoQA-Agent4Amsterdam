# SPDX-License-Identifier: GPL-3.0-only

"""Ask the planning model to compose a workflow over the operation contracts."""

from __future__ import annotations


from dataclasses import asdict
from typing import Mapping


from data_pipeline.catalog import CatalogReader
from data_pipeline.models import CatalogLayer
from data_pipeline.serialization import canonical_json
from data_pipeline.storage import ObjectStore
from geoqa_agent.governance import result_contract
from geoqa_agent.question_interpretation import SupportedInterpretation
from geoqa_agent.structured_artifacts import (
    ArtifactContract,
    ArtifactRequest,
    StructuredArtifactClient,
)
from geoqa_agent.tool_registry import (
    OperationContract,
    ToolRegistry,
)


from geoqa_agent.workflow_models import (
    CONCRETE_ADAPTER,
    ConcreteWorkflowArtifact,
    DRAFT_ADAPTER,
    DataBindingsArtifact,
    TaskSpecificationArtifact,
    ValidationResult,
    WorkflowDraft,
    WorkflowDraftRepository,
    sealed_draft,
)


class WorkflowPlanningService:
    """Ask the planning model to compose one complete workflow proposal."""

    def __init__(
        self,
        *,
        storage: ObjectStore,
        client: StructuredArtifactClient,
        tool_registry: ToolRegistry,
    ) -> None:
        self._storage = storage
        self._client = client
        self._tool_registry = tool_registry
        self._repository = WorkflowDraftRepository(storage)

    def propose(
        self,
        interpretation: SupportedInterpretation,
        *,
        review_context: Mapping[str, object] | None = None,
        case_examples: tuple[Mapping[str, object], ...] | None = None,
    ) -> WorkflowDraft:
        """Ask the planning model for one draft pinned to catalog and registry.

        The model composes the workflow from the allow-listed operation
        contracts; nothing in the input pre-composes it. ``review_context``
        carries the previous failed draft plus its diagnostics on repair
        retries. ``case_examples`` adds accepted workflows from
        structurally similar past tasks as worked examples.
        """

        catalog = CatalogReader(self._storage).get(interpretation.catalog_version)
        task = interpretation.task_specification
        input_document: dict[str, object] = {
            "catalog_version": catalog.version,
            "tool_registry_version": self._tool_registry.version,
            "task_specification": asdict(task),
            "output_contract": result_contract(task).document(task),
            "data_bindings": [
                {
                    **asdict(item),
                    "layer": _planning_layer_document(
                        catalog.layer(item.dataset_id, item.layer_id)
                    ),
                }
                for item in interpretation.bindings
            ],
            "template_placeholders": [
                f"{item.capability_input_ref}_retrieved_at"
                for item in interpretation.bindings
            ],
            "operations": [
                _algorithm_document(algorithm)
                for algorithm in self._tool_registry.algorithms
            ],
        }
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
        concrete = CONCRETE_ADAPTER.validate_python(artifact.data["concrete_workflow"])
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
            concrete_workflow=ConcreteWorkflowArtifact(
                value=concrete,
                provenance=artifact.provenance,
            ),
        )
        draft = sealed_draft(provisional)
        self._repository.save(draft)
        return draft


def planning_repair_context_document(
    draft: WorkflowDraft,
    validation: ValidationResult,
    *,
    attempt: int,
) -> dict[str, object]:
    """Serialize one failed proposal for the next planning attempt."""

    return {
        "attempt": attempt,
        "failed_draft": DRAFT_ADAPTER.dump_python(draft, mode="json"),
        "diagnostics": [asdict(item) for item in validation.diagnostics],
    }


def _planning_layer_document(layer: CatalogLayer) -> dict[str, object]:
    """Describe one bound layer for the planner: schema, semantics, timing."""

    enriched = layer.enriched
    annotated = (
        {}
        if enriched is None
        else {item.name: item.measurement_scale.value for item in enriched.attributes}
    )
    return {
        "dataset_id": layer.dataset_id,
        "layer_id": layer.layer_id,
        "crs": layer.crs,
        "data_kind": layer.kind.value,
        "geometry_types": list(layer.geometry_types),
        "feature_count": None if layer.vector is None else layer.vector.feature_count,
        "pixel_size": None if layer.raster is None else layer.raster.pixel_size,
        "source_identity_fields": list(layer.source_identity_fields),
        "retrieved_at": layer.raw.provenance.retrieved_at.isoformat(),
        "semantic_label": (
            None if enriched is None else enriched.semantic_label.value
        ),
        "attributes": [
            {
                "name": attribute.name,
                "storage_type": attribute.storage_type,
                "sample_values": list(attribute.sample_values[:3]),
                "measurement_scale": annotated.get(attribute.name),
            }
            for attribute in layer.attributes
        ],
    }


def _algorithm_document(algorithm: OperationContract) -> dict[str, object]:
    return {
        "algorithm_id": algorithm.algorithm_id,
        "description": algorithm.description,
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
                "geometry": (
                    None if parameter.geometry is None else list(parameter.geometry)
                ),
                "data_kind": parameter.data_kind,
            }
            for parameter in algorithm.parameters
        ],
        "outputs": [
            {
                "name": output.name,
                "value_type": output.value_type,
                "required": output.required,
                "effect": output.effect,
                "data_kind": output.data_kind,
            }
            for output in algorithm.outputs
        ],
    }
