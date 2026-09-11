import { apiRequest } from './http'

export interface LLMEndpointSummary {
  id: number
  name: string
  endpoint_type: string
  endpoint_type_label: string
  default_model: string
}

export interface LLMActiveTarget {
  source: 'server' | 'external'
  label: string
  base_url: string
  model: string
  endpoint_type: string
}

export interface LLMSettings {
  ok: true
  endpoints: LLMEndpointSummary[]
  active: LLMActiveTarget
  selected_endpoint_id: number | null
  server_default_model: string
  can_edit: boolean
}

const BASE = '/api/workspaces/v1/current'

export const llmSettingsApi = {
  get: () => apiRequest<LLMSettings>(`${BASE}/llm-settings/`),
  select: (endpointId: number | null) => apiRequest<LLMSettings>(`${BASE}/llm-settings/select/`, {
    method: 'POST',
    json: { endpoint_id: endpointId },
  }),
}
