// SPDX-License-Identifier: GPL-3.0-only

// Browser-side mirrors of the API's persisted Pydantic models. Nested
// payloads the UI only displays are kept as loose JsonValue records rather
// than fully typed shapes.

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface CurrentIdentity {
  principal_id: string;
  display_name: string;
}

export interface ValidationReview {
  schema_version?: "workflow-validation-v2" | null;
  validation_id: string | null;
  draft_id: string | null;
  status: "pass" | "pass_with_warnings" | "fail";
  diagnostics: Array<Record<string, JsonValue>>;
}

export interface AdvisoryOverride {
  actor_principal_id: string;
  acknowledged_at: string;
  diagnostic_codes: string[];
}

export interface SessionDraftVersion {
  version: number;
  draft_version_id: string;
  trigger: "submission" | "edit" | "regeneration" | "result_rejection" | "auto_repair";
  instruction: string | null;
  created_at: string;
  draft_id: string | null;
  question_phrases: Array<Record<string, JsonValue>>;
  task_specification: Record<string, JsonValue>;
  bindings: Array<Record<string, JsonValue>>;
  abstract_workflow: Record<string, JsonValue> | null;
  concrete_workflow: Record<string, JsonValue> | null;
  assumptions: string[];
  unresolved_items: string[];
  validation: ValidationReview;
  unsupported_result: { failed_roles: Array<Record<string, JsonValue>> } | null;
  interpretation_repair?: {
    attempt_count: number;
    failed_attempts: Array<{ diagnostic_codes: string[] }>;
  };
}

export interface FeedbackRecord {
  action: "edit" | "regeneration" | "result_rejection" | "auto_repair";
  instruction: string | null;
  actor_principal_id: string;
  submitted_at: string;
  from_draft_version: number;
  to_draft_version: number;
}

export interface ExecutionAuthorization {
  draft_version: number;
  draft_version_id: string;
  draft_id: string;
  validation_id: string;
  authorized_at: string;
  advisory_override?: AdvisoryOverride | null;
}

export type ExecutionJobStatus = "queued" | "running" | "succeeded" | "failed";

export interface ExecutionFailure {
  code: string;
  message: string;
  step_id: string | null;
}

export interface ExecutionJob {
  job_id: string;
  status: ExecutionJobStatus;
  created_at: string;
  updated_at: string;
  effective_steps: Array<{
    step_id: string;
    algorithm_id: string;
    parameters: Record<string, JsonValue>;
    stdout: string;
    stderr: string;
    elapsed_seconds: number;
  }>;
  output_locations: Record<string, string>;
  failure: ExecutionFailure | null;
}

export interface CandidateResultRow {
  identificatie: string;
  volgnummer: number;
  count: number;
}

export interface CandidateDiagnostic {
  category: string;
  count: number;
  record_refs: string[];
}

export interface CandidateAnswerFailure {
  status: "rejected";
  phase: "sanity-check";
  evaluated_at: string;
  diagnostics: Array<{ code: string; message: string; ref: string | null }>;
}

export interface CountCandidateAnswer {
  candidate_answer_id: string;
  constructed_at: string;
  result_table: CandidateResultRow[];
  selected_identities: Array<[string, number]>;
  selected_geometry: {
    location: string;
    media_type: string;
    crs: string;
    feature_identity_fields: string[];
    feature_count: number;
  };
  answer_map: {
    layer_ref: string;
    geometry_location: string;
    feature_count: number;
    crs: string;
    title: string;
    count_field: string;
  };
  diagnostics: CandidateDiagnostic[];
  summary: string;
  reproducibility: {
    execution_job_id: string;
    draft_id: string;
    validation_id: string;
    catalog_version: string;
    inputs: Array<{
      capability_input_ref: string;
      dataset_id: string;
      layer_id: string;
      dataset_version: string;
      content_hash: string;
      source_content_hash: string;
      retrieved_at: string;
    }>;
    annotation_versions: string[];
    ontology_versions: Record<string, string>;
    tool_registry_version: string;
    geopandas_version: string;
    shapely_version: string;
    code_commit: string;
    planning_provider: string;
    planning_model: string;
    planning_role_settings: Record<string, JsonValue>;
    planning_prompt_version: string;
    planning_schema_version: string;
    task_schema_version: string;
    effective_parameters: Array<{
      step_id: string;
      algorithm_id: string;
      parameters: Record<string, JsonValue>;
    }>;
    validation_status?: ValidationReview["status"];
    advisory_override?: AdvisoryOverride | null;
  };
}

export interface NearestCandidateAnswer {
  answer_kind: "point-to-point-euclidean-nearest";
  candidate_answer_id: string;
  constructed_at: string;
  result_table: Array<{
    source_id: string;
    target_id: string;
    distance_m: number;
  }>;
  source_geometry: {
    location: string;
    media_type: string;
    crs: string;
    feature_identity_fields: string[];
    feature_count: number;
  };
  answer_map: {
    layer_ref: string;
    geometry_location: string;
    feature_count: number;
    crs: string;
    title: string;
    identity_field: string;
    distance_field: string;
  };
  diagnostics: CandidateDiagnostic[];
  summary: string;
  sanity_checks: string[];
  reproducibility: CountCandidateAnswer["reproducibility"];
}

export type CandidateAnswer = CountCandidateAnswer | NearestCandidateAnswer;

export interface ResultDecision {
  decision: "accepted" | "rejected";
  candidate_answer_id: string;
  actor_principal_id: string;
  decided_at: string;
  feedback: string | null;
  workflow_id: string | null;
  answer_artifact_ref: string | null;
  workflow_record_ref: string | null;
}

export type AnswerMapGeometry =
  | { type: "Point"; coordinates: number[] }
  | { type: "Polygon"; coordinates: number[][][] }
  | { type: "MultiPolygon"; coordinates: number[][][][] };

export interface CountAnswerMapFeatureCollection {
  type: "FeatureCollection";
  candidate_answer_id: string;
  title: string;
  source_crs: string;
  display_crs: "EPSG:4326";
  context: string;
  features: Array<{
    type: "Feature";
    geometry: AnswerMapGeometry;
    properties: {
      identity: Record<string, string | number>;
      count_field: string;
      count: number;
      is_selected: boolean;
    };
  }>;
}

export interface NearestAnswerMapFeatureCollection {
  type: "FeatureCollection";
  answer_kind: "point-to-point-euclidean-nearest";
  candidate_answer_id: string;
  title: string;
  source_crs: string;
  display_crs: "EPSG:4326";
  context: string;
  features: Array<{
    type: "Feature";
    geometry: { type: "Point"; coordinates: number[] };
    properties: {
      identity: Record<string, string | number>;
      distance_field: string;
      nearest_distance_m: number;
    };
  }>;
}

export type AnswerMapFeatureCollection =
  | CountAnswerMapFeatureCollection
  | NearestAnswerMapFeatureCollection;

export interface QuestionSessionSummary {
  session_id: string;
  question: string;
  created_at: string;
  expires_at: string;
  current_draft_version: number;
  latest_validation_status: ValidationReview["status"];
  has_execution_job: boolean;
  has_candidate_answer: boolean;
  has_result_decision: boolean;
}

export interface CatalogLayer {
  dataset: string;
  feature_type: string;
  name: string;
  name_language: "en" | "nl";
  description: string;
  description_language: "en" | "nl";
  semantic_label: string | null;
  geometry_types: string[];
  feature_count: number;
  dataset_version: string;
  crs: string;
  original_crs: string;
  temporal_extent: { start: string; end: string | null };
  spatial_extent: JsonValue;
}

export interface CatalogLayerListing {
  catalog_version: string | null;
  layers: CatalogLayer[];
}

export interface GeoJsonFeatureCollection {
  type: "FeatureCollection";
  features: Array<{
    type: "Feature";
    geometry: { type: string; coordinates: JsonValue } | null;
    properties: Record<string, JsonValue>;
  }>;
}

export interface QuestionSession {
  session_id: string;
  version: number;
  question: string;
  created_at: string;
  updated_at: string;
  expires_at: string;
  current_draft_version: number;
  draft_versions: SessionDraftVersion[];
  feedback_history: FeedbackRecord[];
  execution_authorization: ExecutionAuthorization | null;
  job_reference: { job_id: string; status: ExecutionJobStatus } | null;
  execution_result: ExecutionJob | null;
  candidate_answer: CandidateAnswer | null;
  candidate_answer_failure: CandidateAnswerFailure | null;
  result_review_history: ResultDecision[];
  result_decision: ResultDecision | null;
}
