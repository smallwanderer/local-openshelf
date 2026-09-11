import { apiRequest } from './http'
import type { ServerPolicySummary } from './models'
import type { SessionBootstrapResponse } from './types'

interface ApiServerPolicyResponse {
  operation_mode: string
  policy: {
    search_strategy: string
    search_top_k: number
    retrieval_threshold: number | null
  }
  rag: {
    configured: boolean
    available: boolean
    status: string
    reason_code: string
    updated_at: string | null
    model: string
    runtime: string
    priority_preset: string
    selection_mode: string
    serving_concurrency: number
  }
  embedding: {
    enabled: boolean
    configured: boolean
    status: string
    model: string
    provider: string
    dimension: number
    sparse_enabled: boolean
    distance_strategy: string
  }
}

export const systemApi = {
  getSession() {
    return apiRequest<SessionBootstrapResponse>('/api/accounts/v1/session/')
  },

  async getServerPolicy(): Promise<ServerPolicySummary> {
    const payload = await apiRequest<ApiServerPolicyResponse>('/api/document-ai/v1/server-policy/')
    return {
      operationMode: payload.operation_mode,
      policy: {
        searchStrategy: payload.policy.search_strategy,
        searchTopK: payload.policy.search_top_k,
        retrievalThreshold: payload.policy.retrieval_threshold,
      },
      rag: {
        configured: payload.rag.configured,
        available: payload.rag.available,
        status: payload.rag.status,
        reasonCode: payload.rag.reason_code,
        updatedAt: payload.rag.updated_at,
        model: payload.rag.model,
        runtime: payload.rag.runtime,
        priorityPreset: payload.rag.priority_preset,
        selectionMode: payload.rag.selection_mode,
        servingConcurrency: payload.rag.serving_concurrency,
      },
      embedding: {
        enabled: payload.embedding.enabled,
        configured: payload.embedding.configured,
        status: payload.embedding.status,
        model: payload.embedding.model,
        provider: payload.embedding.provider,
        dimension: payload.embedding.dimension,
        sparseEnabled: payload.embedding.sparse_enabled,
        distanceStrategy: payload.embedding.distance_strategy,
      },
    }
  },
}
