// SPDX-License-Identifier: GPL-3.0-only

// Shared frontend test fixtures mirroring realistic API payloads.

import type {
  AnswerMapFeatureCollection,
  CandidateAnswer,
  ExecutionJob,
  ExecutionJobStatus,
  QuestionSession,
  SessionDraftVersion,
} from "../types";

export function passDraft(version = 1): SessionDraftVersion {
  return {
    version,
    draft_version_id: `draft-version-${version}`,
    trigger: version === 1 ? "submission" : "edit",
    instruction: version === 1 ? null : "Keep strict within semantics.",
    created_at: "2026-08-25T10:30:00Z",
    draft_id: `plan-${version}`,
    question_phrases: [
      {
        text: "Amsterdam neighborhoods",
        functional_role: "support",
        referenced_phenomenon: "neighborhood",
        candidate_ccd_meaning: "ObjectDS",
        confidence: 0.99,
      },
      {
        text: "registered public sports locations",
        functional_role: "condition",
        referenced_phenomenon: "sports location",
        candidate_ccd_meaning: "ObjectDS",
        confidence: 0.99,
      },
    ],
    task_specification: {
      required_output: "neighborhoods with zero registered public sports locations",
      support: {
        semantic_label: "neighborhood",
        source_state: "active at the selected snapshot",
        identity_fields: ["identificatie", "volgnummer"],
      },
      counted_objects: {
        semantic_label: "sports location",
        distinct_by: "id",
      },
    },
    bindings: [
      {
        role: "supports",
        capability_input_ref: "supports",
        dataset_id: "gebieden",
        layer_id: "buurten",
        content_hash: "sha256:neighborhoods",
        analytical_compatibility: { passed: true, reasons: ["CCD match"] },
        analytical_ccd_meaning: "ObjectDS",
      },
      {
        role: "counted_objects",
        capability_input_ref: "sports_points",
        dataset_id: "sport",
        layer_id: "openbaresportplek",
        content_hash: "sha256:sports",
        analytical_compatibility: { passed: true, reasons: ["CCD match"] },
        analytical_ccd_meaning: "ObjectDS",
      },
    ],
    abstract_workflow: {
      capability_id: "count-zero-sports-locations-by-neighborhood",
      steps: [
        {
          step_id: "select-active-supports",
          abstraction_id: "https://example.test/abstract#SelectLayerByAttributeTessObjects",
          input_refs: ["supports"],
          output_ref: "active_supports",
        },
        {
          step_id: "count-sports-within-supports",
          abstraction_id: "https://example.test/abstract#SpatialJoinCountTess",
          input_refs: ["sports_points", "active_supports"],
          output_ref: "support_counts",
        },
        {
          step_id: "select-zero-count-supports",
          abstraction_id: "https://example.test/abstract#SelectLayerByRatioTessObjects",
          input_refs: ["support_counts"],
          output_ref: "zero_count_supports",
        },
      ],
      final_output_ref: "zero_count_supports",
    },
    concrete_workflow: {
      steps: [
        {
          step_id: "select",
          abstract_step_id: "select-active-supports",
          algorithm_id: "geopandas:filterbyexpression",
          parameters: [
            { name: "input", source: "ref", value: "supports" },
            { name: "expression", source: "template", value: "begin_geldigheid <= '{supports_retrieved_at}' and eind_geldigheid.isnull()" },
          ],
          outputs: [{ name: "output", ref: "active_supports", kind: "sink" }],
        },
        {
          step_id: "count",
          abstract_step_id: "count-sports-within-supports",
          algorithm_id: "geopandas:countpointsinpolygon",
          parameters: [
            { name: "polygons", source: "ref", value: "active_supports" },
            { name: "points", source: "ref", value: "sports_points" },
            { name: "class_field", source: "literal", value: "id" },
            { name: "field", source: "literal", value: "sports_count" },
          ],
          outputs: [{ name: "output", ref: "support_counts", kind: "sink" }],
        },
        {
          step_id: "select-zero",
          abstract_step_id: "select-zero-count-supports",
          algorithm_id: "geopandas:filterbyexpression",
          parameters: [
            { name: "input", source: "ref", value: "support_counts" },
            { name: "expression", source: "template", value: "sports_count == 0" },
          ],
          outputs: [{ name: "output", ref: "zero_count_supports", kind: "sink" }],
        },
      ],
      final_output_ref: "zero_count_supports",
      result_table_ref: "zero_count_supports",
      diagnostic_refs: [],
    },
    assumptions: [],
    unresolved_items: [],
    validation: {
      validation_id: `validation-${version}`,
      draft_id: `plan-${version}`,
      status: "pass",
      diagnostics: [],
    },
    unsupported_result: null,
  };
}

export function failedDraft(): SessionDraftVersion {
  const draft = passDraft();
  return {
    ...draft,
    validation: {
      ...draft.validation,
      status: "fail",
      diagnostics: [
        {
          code: "tool-not-registered",
          message: "The concrete operation is not registered.",
        },
      ],
    },
    unresolved_items: ["Select an executable registered operation."],
    unsupported_result: {
      failed_roles: [
        {
          role: "counted_objects",
          closest_candidates: [
            {
              candidate_id: "sport/openbaresportplek",
              rejection_reasons: ["The requested phenomenon is unavailable."],
            },
          ],
        },
      ],
    },
  };
}

export function sessionWith(
  draftVersions: SessionDraftVersion[] = [passDraft()],
): QuestionSession {
  const current = draftVersions.at(-1)!;
  return {
    session_id: "session-123",
    version: current.version,
    question: "Which Amsterdam neighborhoods have no sports locations?",
    created_at: "2026-08-25T10:30:00Z",
    updated_at: current.created_at,
    expires_at: "2026-09-01T10:30:00Z",
    current_draft_version: current.version,
    draft_versions: draftVersions,
    feedback_history: [],
    execution_authorization: null,
    job_reference: null,
    execution_result: null,
    candidate_answer: null,
    candidate_answer_failure: null,
    result_review_history: [],
    result_decision: null,
  };
}

export function executionJob(status: ExecutionJobStatus): ExecutionJob {
  return {
    job_id: "job-123",
    status,
    created_at: "2026-08-25T10:31:00Z",
    updated_at: "2026-08-25T10:33:00Z",
    effective_steps: [],
    output_locations: {},
    failure:
      status === "failed"
        ? {
            code: "runtime-failure",
            message: "Execution step did not produce the declared output.",
            step_id: "count",
          }
        : null,
  };
}

export function authorizedSession(): QuestionSession {
  return {
    ...sessionWith(),
    version: 2,
    execution_authorization: {
      draft_version: 1,
      draft_version_id: "draft-version-1",
      draft_id: "plan-1",
      validation_id: "validation-1",
      authorized_at: "2026-08-25T10:31:00Z",
    },
    job_reference: { job_id: "job-123", status: "queued" },
  };
}

export function terminalExecutionSession(): QuestionSession {
  const job = executionJob("failed");
  return {
    ...authorizedSession(),
    version: 3,
    updated_at: job.updated_at,
    job_reference: { job_id: job.job_id, status: job.status },
    execution_result: job,
  };
}

export function candidateAnswer(): CandidateAnswer {
  return {
    candidate_answer_id: "sha256:candidate-answer",
    constructed_at: "2026-08-25T10:34:00Z",
    result_table: [
      { identificatie: "A", volgnummer: 1, count: 2 },
      { identificatie: "B", volgnummer: 7, count: 0 },
    ],
    selected_identities: [["B", 7]],
    selected_geometry: {
      location: "azure://execution-jobs/job-123/outputs/zero_count_supports.parquet",
      media_type: "application/vnd.apache.parquet",
      crs: "EPSG:28992",
      feature_identity_fields: ["identificatie", "volgnummer"],
      feature_count: 1,
    },
    answer_map: {
      layer_ref: "zero_count_supports",
      geometry_location:
        "azure://execution-jobs/job-123/outputs/zero_count_supports.parquet",
      feature_count: 1,
      crs: "EPSG:28992",
      title:
        "Amsterdam neighborhoods with zero registered public sports locations",
      count_field: "sports_count",
    },
    diagnostics: [
      { category: "unmatched", count: 2, record_refs: ["sports.10", "sports.11"] },
      { category: "boundary", count: 1, record_refs: ["sports.10"] },
    ],
    summary:
      "In the selected snapshot, 1 of 2 active Amsterdam neighborhoods had zero registered public sports locations. This result does not imply complete provision, outdoor status, or facility footprints.",
    reproducibility: {
      execution_job_id: "job-123",
      draft_id: "plan-1",
      validation_id: "validation-1",
      catalog_version: "catalog-2026-08-24",
      inputs: [
        {
          capability_input_ref: "supports",
          dataset_id: "gebieden",
          layer_id: "buurten",
          dataset_version: "snapshot-2026-08-24",
          content_hash: "sha256:neighborhoods",
          source_content_hash: "sha256:source-neighborhoods",
          retrieved_at: "2026-08-24T12:00:00Z",
        },
        {
          capability_input_ref: "counted_objects",
          dataset_id: "sport",
          layer_id: "openbaresportplek",
          dataset_version: "snapshot-2026-08-24",
          content_hash: "sha256:sports",
          source_content_hash: "sha256:source-sports",
          retrieved_at: "2026-08-24T12:00:00Z",
        },
      ],
      annotation_versions: ["annotation-v1"],
      ontology_versions: { ccd: "sha256:ccd" },
      tool_registry_version: "tool-registry-v1",
      geopandas_version: "1.1.4",
      shapely_version: "2.1.2",
      code_commit: "0123456789abcdef",
      planning_provider: "openai",
      planning_model: "planning-model",
      planning_role_settings: {},
      planning_prompt_version: "planning-v1",
      planning_schema_version: "planning-v1",
      task_schema_version: "task-v1",
      effective_parameters: [
        {
          step_id: "count",
          algorithm_id: "geopandas:countpointsinpolygon",
          parameters: { CLASSFIELD: "id" },
        },
      ],
    },
  };
}

export function answerMap(): AnswerMapFeatureCollection {
  return {
    type: "FeatureCollection",
    candidate_answer_id: "sha256:candidate-answer",
    title:
      "Amsterdam neighborhoods with zero registered public sports locations",
    source_crs: "EPSG:28992",
    display_crs: "EPSG:4326",
    context: "Active Amsterdam neighborhoods in the selected snapshot",
    features: [
      {
        type: "Feature",
        geometry: {
          type: "Polygon",
          coordinates: [[[4.85, 52.35], [4.86, 52.35], [4.86, 52.36], [4.85, 52.35]]],
        },
        properties: {
          identity: { identificatie: "A", volgnummer: 1 },
          count_field: "sports_count",
          count: 2,
          is_selected: false,
        },
      },
      {
        type: "Feature",
        geometry: {
          type: "Polygon",
          coordinates: [[[4.86, 52.35], [4.87, 52.35], [4.87, 52.36], [4.86, 52.35]]],
        },
        properties: {
          identity: { identificatie: "B", volgnummer: 7 },
          count_field: "sports_count",
          count: 0,
          is_selected: true,
        },
      },
    ],
  };
}

export function nearestCandidateAnswer(): CandidateAnswer {
  const count = candidateAnswer();
  return {
    answer_kind: "point-to-point-euclidean-nearest",
    candidate_answer_id: "sha256:nearest-answer",
    constructed_at: count.constructed_at,
    result_table: [
      { source_id: "source-tie", target_id: "target-left", distance_m: 10 },
      { source_id: "source-tie", target_id: "target-right", distance_m: 10 },
      { source_id: "source-zero", target_id: "target-zero", distance_m: 0 },
    ],
    source_geometry: {
      location: "azure://execution-jobs/job-123/outputs/nearest_pairs.parquet",
      media_type: "application/vnd.apache.parquet",
      crs: "EPSG:28992",
      feature_identity_fields: ["source_id"],
      feature_count: 2,
    },
    answer_map: {
      layer_ref: "nearest_pairs",
      geometry_location: "azure://execution-jobs/job-123/outputs/nearest_pairs.parquet",
      feature_count: 2,
      crs: "EPSG:28992",
      title: "Source points styled by nearest-target distance",
      identity_field: "source_id",
      distance_field: "distance_m",
    },
    diagnostics: [],
    summary: "Computed planar Euclidean nearest targets for 2 source points.",
    sanity_checks: ["equidistant targets retain separate result rows"],
    reproducibility: { ...count.reproducibility },
  };
}

export function nearestAnswerMap(): AnswerMapFeatureCollection {
  return {
    type: "FeatureCollection",
    answer_kind: "point-to-point-euclidean-nearest",
    candidate_answer_id: "sha256:nearest-answer",
    title: "Source points styled by nearest-target distance",
    source_crs: "EPSG:28992",
    display_crs: "EPSG:4326",
    context: "Selected-snapshot nearest result",
    features: [
      {
        type: "Feature",
        geometry: { type: "Point", coordinates: [4.85, 52.35] },
        properties: {
          identity: { source_id: "source-tie" },
          distance_field: "distance_m",
          nearest_distance_m: 10,
        },
      },
      {
        type: "Feature",
        geometry: { type: "Point", coordinates: [4.86, 52.36] },
        properties: {
          identity: { source_id: "source-zero" },
          distance_field: "distance_m",
          nearest_distance_m: 0,
        },
      },
    ],
  };
}

export function candidateSession(): QuestionSession {
  const job = executionJob("succeeded");
  return {
    ...authorizedSession(),
    version: 3,
    updated_at: job.updated_at,
    job_reference: { job_id: job.job_id, status: job.status },
    execution_result: job,
    candidate_answer: candidateAnswer(),
  };
}

export function nearestCandidateSession(): QuestionSession {
  return { ...candidateSession(), candidate_answer: nearestCandidateAnswer() };
}

export function sanityCheckFailureSession(): QuestionSession {
  const job = executionJob("succeeded");
  return {
    ...authorizedSession(),
    version: 3,
    updated_at: job.updated_at,
    job_reference: { job_id: job.job_id, status: job.status },
    execution_result: job,
    candidate_answer_failure: {
      status: "rejected",
      phase: "sanity-check",
      evaluated_at: "2026-08-25T10:34:00Z",
      diagnostics: [
        {
          code: "crs-mismatch",
          message: "Output must use EPSG:28992.",
          ref: "zero_count_supports",
        },
      ],
    },
  };
}
