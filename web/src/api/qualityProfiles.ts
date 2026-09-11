import { apiRequest } from './http'

export type QualityAxis = 'retrieval' | 'generation' | 'prompt_policy'
export type ProfileStatus = 'draft' | 'active' | 'archived'
export type ValidationState = 'verified' | 'unverified' | 'stale' | 'rollback' | 'not_run'

export interface QualityFieldSchema {
  type: 'number' | 'integer' | 'boolean' | 'enum' | 'string'
  tier: 'core' | 'advanced'
  minimum?: number
  maximum?: number
  step?: number
  choices?: string[]
  effects?: string[]
  recommended_minimum?: number
  recommended_maximum?: number
}

export interface RetrievalConfig extends Record<string, unknown> {
  dense_weight: number
  sparse_weight: number
  search_top_k: number
  rag_search_top_k: number
  retrieval_threshold: number | null
  evidence_top_k: number
  evidence_context_window: number
  candidate_multiplier: number
  per_node_candidate_cap: number
  query_sparse_top_n: number
  pooling_method: 'normalized_logsumexp' | 'normalized_softmax' | 'max'
  pool_top_k: number
  pool_tau: number
  doc_length_penalty_alpha: number
  contextual_compression: { enabled: boolean }
}

export interface GenerationConfig extends Record<string, unknown> {
  max_output_tokens: number
  temperature: number
  top_p: number
}

export type PromptRoute = 'document_rag' | 'no_retrieval'
export interface PromptRoutePolicy {
  mode: 'inherit' | 'replace'
  instruction: string | null
  sha256?: string
  character_count?: number
  server_prompt_contract_version?: number
}

export interface PromptPolicy extends Record<string, unknown> {
  document_rag: PromptRoutePolicy
  no_retrieval: PromptRoutePolicy
}

export interface AxisSection<T> {
  overrides: Partial<T>
  effective: T
  changed_fields: string[]
}

export interface QualityProfileRow {
  uid: string
  version: number
  revision: number
  status: ProfileStatus
  changed_axes: QualityAxis[]
  based_on_uid: string | null
  retrieval: AxisSection<RetrievalConfig>
  generation: AxisSection<GenerationConfig>
  prompt_policy: AxisSection<PromptPolicy>
  validation: {
    state: ValidationState
    last_run_uid: string | null
    warnings: string[]
  }
  note: string
  created_at: string
  updated_at: string
  applied_at: string | null
}

export interface ProfilePermissions {
  can_read: boolean
  can_edit: boolean
  can_apply: boolean
}

export interface QualityProfileEnvelope {
  ok: true
  workspace_uid: string
  active: QualityProfileRow
  draft: QualityProfileRow | null
  defaults: {
    retrieval: RetrievalConfig
    generation: GenerationConfig
    prompt_policy: Record<PromptRoute, PromptRoutePolicy>
  }
  schema: {
    retrieval: Record<string, QualityFieldSchema>
    generation: Record<string, QualityFieldSchema>
  }
  capabilities: {
    schema_version: number
    server_prompt_contract_version: number
    max_instruction_chars: number
    import_extensions: string[]
  }
  permissions: ProfilePermissions
  fixed_contract: string
  provider_disclosure: string | null
}

export interface PromptPreview {
  ok: true
  route: PromptRoute
  assembled_prompt: string
  sha256: string
  character_count: number
  server_prompt_contract_version: number
}

interface DraftMutation {
  ok: true
  draft: QualityProfileRow | null
}

const BASE = '/api/workspaces/v1/current'

export interface SaveQualityProfileDraftInput {
  retrieval?: { overrides: Partial<RetrievalConfig>; reset_fields: string[] }
  generation?: { overrides: Partial<GenerationConfig>; reset_fields: string[] }
  prompt_policy?: Partial<PromptPolicy>
}

export const qualityProfileApi = {
  get: () => apiRequest<QualityProfileEnvelope>(`${BASE}/quality-profile/`),
  saveDraft: (expectedRevision: number, sections: SaveQualityProfileDraftInput, note: string) => apiRequest<DraftMutation>(`${BASE}/quality-profile/draft/`, {
    method: 'PATCH',
    json: { expected_revision: expectedRevision, note, ...sections },
  }),
  discardDraft: (expectedRevision: number) => apiRequest<DraftMutation>(`${BASE}/quality-profile/draft/discard/`, {
    method: 'POST',
    json: { expected_revision: expectedRevision },
  }),
  previewPrompt: (expectedRevision: number, route: PromptRoute) => apiRequest<PromptPreview>(`${BASE}/quality-profile/draft/preview-prompt/`, {
    method: 'POST',
    json: { expected_revision: expectedRevision, route },
  }),
  apply: (expectedRevision: number, evaluationRunUid: string | null, allowUnverified: boolean, note: string) => apiRequest<QualityProfileEnvelope>(`${BASE}/quality-profile/apply/`, {
    method: 'POST',
    json: {
      expected_revision: expectedRevision,
      evaluation_run_uid: evaluationRunUid,
      allow_unverified: allowUnverified,
      note,
    },
  }),
  listVersions: () => apiRequest<{ ok: true; results: QualityProfileVersionSummary[] }>(`${BASE}/quality-profile/versions/?page=1&limit=20`),
}

export interface QualityProfileVersionSummary {
  uid: string
  version: number
  changed_axes: QualityAxis[]
  validation: { state: ValidationState }
  note: string
  applied_at: string | null
  created_by: { id: number; display_name: string } | null
}

export interface EvaluationDatasetItem {
  query: string
  expected_node_ids: string[]
}

export interface EvaluationDataset {
  uid: string
  axis: QualityAxis
  name: string
  item_count: number
  created_at: string
}

export type EvaluationRunStatus = 'pending' | 'running' | 'succeeded' | 'failed'

export interface RetrievalEvaluationMetrics {
  queries: number
  chunk_count: number
  top_k: number
  dense_weight: number
  sparse_weight: number
  hit_rate_at_1: number
  hit_rate_at_k: number
  mrr_at_k: number
  per_query: { query: string; hit_at_1: boolean; matched_rank: number | null }[]
}

export interface EvaluationRun {
  uid: string
  axis: QualityAxis
  dataset_uid: string
  status: EvaluationRunStatus
  metrics: Partial<RetrievalEvaluationMetrics>
  error_message: string
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export const evaluationDatasetApi = {
  list: (axis: QualityAxis) => apiRequest<{ ok: true; axis: QualityAxis; datasets: EvaluationDataset[] }>(`${BASE}/evaluation-datasets/?axis=${axis}`),
  create: (axis: QualityAxis, name: string, items: EvaluationDatasetItem[]) => apiRequest<{ ok: true; dataset: EvaluationDataset }>(`${BASE}/evaluation-datasets/`, {
    method: 'POST',
    json: { axis, name, items },
  }),
}

export const evaluationRunApi = {
  startRetrieval: (expectedRevision: number, datasetUid: string) => apiRequest<{ ok: true; run: EvaluationRun }>(`${BASE}/quality-profile/evaluate/`, {
    method: 'POST',
    json: { expected_revision: expectedRevision, dataset_uid: datasetUid },
  }),
  get: (runUid: string) => apiRequest<{ ok: true; run: EvaluationRun }>(`${BASE}/evaluation-runs/${runUid}/`),
}
