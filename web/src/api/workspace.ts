import { apiError, apiFetch, apiRequest, ApiClientError, uploadForm } from './http'
import type {
  DocumentAiStatus,
  DocumentFolder,
  DocumentListOptions,
  DocumentListResult,
  DocumentMutationResult,
  DocumentReadiness,
  DocumentSummary,
  DocumentUploadResult,
  RagCitation,
  RagConversation,
  RagConversationMessage,
  RagHistoryItem,
  RagRequest,
  RagStreamHandlers,
  SearchRequest,
  SearchResponse,
  SearchResult,
  SearchScopeNode,
} from './models'

export interface WorkspaceApi {
  listDocuments(options: DocumentListOptions): Promise<DocumentListResult>
  getDocument(uid: string): Promise<DocumentSummary>
  getParsedText(uid: string): Promise<string>
  getReadiness(): Promise<DocumentReadiness>
  getDownloadUrl(uid: string): string
  uploadDocument(file: File, options: { parentUid?: string | null; aiProcessingEnabled: boolean; onProgress?: (percent: number) => void }): Promise<DocumentUploadResult>
  createFolder(name: string, parentUid?: string | null): Promise<DocumentSummary>
  listFolders(): Promise<DocumentFolder[]>
  renameDocument(uid: string, name: string): Promise<DocumentSummary>
  moveDocuments(uids: string[], parentUid: string | null): Promise<DocumentMutationResult>
  toggleStar(uid: string): Promise<boolean>
  trashDocuments(uids: string[]): Promise<DocumentMutationResult>
  restoreDocuments(uids: string[]): Promise<DocumentMutationResult>
  permanentlyDeleteDocuments(uids: string[]): Promise<DocumentMutationResult>
  setAiProcessing(uid: string, enabled: boolean): Promise<DocumentSummary>
  retryAiProcessing(uid: string): Promise<void>
  listSearchScopes(query?: string): Promise<SearchScopeNode[]>
  searchDocuments(request: SearchRequest): Promise<SearchResponse>
  listRagHistory(limit?: number): Promise<RagHistoryItem[]>
  listRagConversations(): Promise<RagConversation[]>
  createRagConversation(title: string, defaultNodeIds: string[]): Promise<RagConversation>
  getRagConversation(uid: string): Promise<RagConversation>
  listRagConversationMessages(uid: string): Promise<RagConversationMessage[]>
  streamAnswer(
    request: RagRequest,
    handlers: RagStreamHandlers,
  ): () => void
}

interface ApiAiStatus {
  parse_status?: string
  parse_label?: string
  embedding_status?: string
  embedding_label?: string
  chunk_count?: number
  completed_chunks?: number
  failed_chunks?: number
  embedding_contract_status?: string
  active_embedding_generation_id?: string
  embedded_generation_ids?: string[]
  reembedding_required?: boolean
  searchable?: boolean
}

interface ApiNode {
  uid: string
  name: string
  ext?: string
  node_type: 'file' | 'directory'
  path?: string
  parent_uid?: string | null
  size?: number
  created_at: string
  updated_at: string
  starred?: boolean
  trashed?: boolean
  ai_processing_enabled?: boolean
  ai_status?: ApiAiStatus | null
  summary?: string
  auto_tags?: unknown
}

interface ApiListResponse {
  files: ApiNode[]
  page?: number
  limit?: number
  total?: number
  has_next?: boolean
}

interface ApiDetailResponse { file: ApiNode }
interface ApiFolderListResponse { folders: Array<{ uid: string; name: string; path: string }> }
interface ApiMutationResponse { ok: boolean; count?: number; errors?: unknown }
interface ApiUploadResponse { file: ApiNode; status: 'done' | 'duplicate'; warnings?: unknown }
interface ApiParsedTextResponse { text: string }
interface ApiReadinessResponse {
  total_files: number
  searchable_files: number
  ready_percent: number
  summary: string
  parse: DocumentReadiness['parse']
  embedding: DocumentReadiness['embedding']
  active_embedding_generation_id?: string
}

interface ApiScopeNode {
  uid: string
  name: string
  path: string
  node_type: 'file' | 'directory'
  depth: number
  ext: string
  file_count: number
}

interface ApiScopeResponse { nodes: ApiScopeNode[] }
interface ApiSearchEvidence {
  chunk_id: number
  text: string
  context_text?: string
  section?: string
  pages?: string
  distance?: number
  hybrid_score?: number
}
interface ApiSearchResult {
  node_id: string
  node_name: string
  file_ext?: string
  doc_score: number
  evidences?: ApiSearchEvidence[]
}
interface ApiSearchResponse {
  results: ApiSearchResult[]
  performance_metrics?: Record<string, unknown>
  query_plan: {
    mode: 'basic' | 'advanced'
    source?: string
    retrieval_query?: string
    intent?: string
    confidence?: number | null
    warnings?: Array<{ code?: string; message?: string }>
    filters?: Array<Record<string, unknown>>
    sorts?: Array<Record<string, unknown>>
  }
}

interface ApiRagCitation {
  id: number
  node_id?: string
  node_name?: string
  chunk_id?: number | null
  section?: string
  pages?: string
  text?: string
  doc_score?: number | null
  hybrid_score?: number | null
  dense_score?: number | null
  sparse_score?: number | null
}

interface ApiRagHistoryResponse {
  history: Array<{
    id: number
    question: string
    answer_preview?: string
    answer?: string
    citations?: ApiRagCitation[]
    result_count?: number
    language?: string
    node_ids?: string[]
    llm_model?: string
    performance_metrics?: Record<string, unknown>
    completed_at?: string | null
    created_at: string
  }>
}

interface ApiRagConversation {
  uid: string
  title: string
  default_node_ids?: string[]
  revision: number
  created_by_id?: number | null
  created_at: string
  updated_at: string
}

interface ApiRagMessage {
  uid: string
  sequence: number
  role: 'user' | 'assistant'
  content?: string
  node_ids?: string[]
  reply_to_uid?: string | null
  created_at: string
  rag_job?: null | {
    id: number
    status: string
    answer?: string
    citations?: ApiRagCitation[]
    error_message?: string
    performance_metrics?: Record<string, unknown>
    completed_at?: string | null
  }
}

type ApiRagEvent =
  | { type: 'started'; job_id: number; llm_target?: string; llm_model?: string }
  | { type: 'sources'; citations?: ApiRagCitation[] }
  | { type: 'token'; text?: string }
  | { type: 'completed'; job_id: number; answer?: string; citations?: ApiRagCitation[]; performance_metrics?: Record<string, unknown> }
  | { type: 'canceled'; performance_metrics?: Record<string, unknown> }
  | { type: 'error'; code?: string; message?: string; performance_metrics?: Record<string, unknown> }

function formatSize(bytes?: number): string {
  if (bytes === undefined) return '—'
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const unitIndex = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  return `${(bytes / 1024 ** unitIndex).toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`
}

function formatDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(date)
}

function mapAiStatus(raw?: ApiAiStatus | null): DocumentAiStatus | null {
  if (!raw) return null
  return {
    parseStatus: raw.parse_status ?? 'pending',
    parseLabel: raw.parse_label ?? '',
    embeddingStatus: raw.embedding_status ?? 'pending',
    embeddingLabel: raw.embedding_label ?? '',
    chunkCount: raw.chunk_count ?? 0,
    completedChunks: raw.completed_chunks ?? 0,
    failedChunks: raw.failed_chunks ?? 0,
    embeddingContractStatus: raw.embedding_contract_status ?? 'missing',
    activeEmbeddingGenerationId: raw.active_embedding_generation_id ?? '',
    embeddedGenerationIds: raw.embedded_generation_ids ?? [],
    reembeddingRequired: Boolean(raw.reembedding_required),
    searchable: Boolean(raw.searchable),
  }
}

function mapStatus(node: ApiNode): DocumentSummary['status'] {
  if (!node.ai_processing_enabled && node.node_type === 'file') return 'disabled'
  if (node.node_type === 'directory') return 'ready'
  const aiStatus = node.ai_status
  if (aiStatus?.embedding_contract_status === 'stale_contract') return 'stale'
  if (aiStatus?.parse_status === 'failed' || aiStatus?.embedding_status === 'failed') return 'failed'
  if (aiStatus?.parse_status === 'completed' && aiStatus?.embedding_status === 'completed') return 'ready'
  return 'processing'
}

function mapNode(node: ApiNode): DocumentSummary {
  const aiStatus = mapAiStatus(node.ai_status)
  const tags = Array.isArray(node.auto_tags)
    ? node.auto_tags.filter((tag): tag is string => typeof tag === 'string')
    : []
  const type = node.node_type === 'directory'
    ? 'FOLDER'
    : (node.ext || node.name.split('.').pop() || 'FILE').replace('.', '').toUpperCase()
  return {
    uid: node.uid,
    name: node.name,
    nodeType: node.node_type,
    type,
    path: node.path ?? `/${node.name}`,
    parentUid: node.parent_uid ?? null,
    size: node.size ?? null,
    sizeLabel: formatSize(node.size),
    chunks: aiStatus?.chunkCount ?? 0,
    updatedAt: node.updated_at,
    updatedLabel: formatDate(node.updated_at),
    createdAt: node.created_at,
    createdLabel: formatDate(node.created_at),
    status: mapStatus(node),
    starred: Boolean(node.starred),
    trashed: Boolean(node.trashed),
    aiProcessingEnabled: node.ai_processing_enabled !== false,
    aiStatus,
    summary: node.summary ?? '',
    tags,
  }
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function mutationResult(payload: ApiMutationResponse, fallbackCount = 0): DocumentMutationResult {
  const errors = stringList(payload.errors)
  return { ok: payload.ok && errors.length === 0, count: payload.count ?? fallbackCount, errors }
}

function mapSearchResult(result: ApiSearchResult): SearchResult {
  const evidences = (result.evidences ?? []).map((evidence) => ({
    chunkId: evidence.chunk_id,
    text: evidence.text,
    contextText: evidence.context_text ?? evidence.text,
    section: evidence.section ?? '',
    pages: evidence.pages ?? '',
    score: typeof evidence.hybrid_score === 'number'
      ? evidence.hybrid_score
      : (typeof evidence.distance === 'number' ? -evidence.distance : null),
  }))
  const primary = evidences[0]
  return {
    uid: result.node_id,
    title: result.node_name,
    fileType: (result.file_ext || result.node_name.split('.').pop() || 'FILE').replace('.', '').toUpperCase(),
    score: result.doc_score,
    text: primary?.contextText || primary?.text || '',
    page: primary?.pages || '',
    section: primary?.section || '',
    evidences,
  }
}

function mapRagCitation(citation: ApiRagCitation): RagCitation {
  return {
    id: citation.id,
    nodeId: citation.node_id ?? '',
    nodeName: citation.node_name ?? '',
    chunkId: citation.chunk_id ?? null,
    section: citation.section ?? '',
    pages: citation.pages ?? '',
    text: citation.text ?? '',
    docScore: citation.doc_score ?? null,
    hybridScore: citation.hybrid_score ?? null,
    denseScore: citation.dense_score ?? null,
    sparseScore: citation.sparse_score ?? null,
  }
}

function mapRagConversation(value: ApiRagConversation): RagConversation {
  return {
    uid: value.uid,
    title: value.title,
    defaultNodeIds: value.default_node_ids ?? [],
    revision: value.revision,
    createdById: value.created_by_id ?? null,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
  }
}

function mapRagMessage(value: ApiRagMessage): RagConversationMessage {
  return {
    uid: value.uid,
    sequence: value.sequence,
    role: value.role,
    content: value.content ?? '',
    nodeIds: value.node_ids ?? [],
    replyToUid: value.reply_to_uid ?? null,
    createdAt: value.created_at,
    ragJob: value.rag_job ? {
      id: value.rag_job.id,
      status: value.rag_job.status,
      answer: value.rag_job.answer ?? '',
      citations: (value.rag_job.citations ?? []).map(mapRagCitation),
      errorMessage: value.rag_job.error_message ?? '',
      performanceMetrics: value.rag_job.performance_metrics ?? {},
      completedAt: value.rag_job.completed_at ?? null,
    } : null,
  }
}

function searchDuration(metrics: Record<string, unknown>): number | null {
  for (const key of ['request_search_ms', 'search_total_ms', 'total_ms']) {
    if (typeof metrics[key] === 'number') return metrics[key]
  }
  return null
}

function buildDocumentListUrl(options: DocumentListOptions): string {
  if (options.view !== 'files') return `/files/api/v1/${options.view}/`
  const params = new URLSearchParams()
  if (options.query) params.set('q', options.query)
  if (options.parentUid) params.set('parent_id', options.parentUid)
  params.set('page', String(options.page ?? 1))
  params.set('limit', String(options.limit ?? 50))
  return `/files/api/v1/files/?${params.toString()}`
}

const djangoDocumentApi: WorkspaceApi = {
  async listDocuments(options) {
    const payload = await apiRequest<ApiListResponse>(buildDocumentListUrl(options))
    const query = options.query?.trim().toLocaleLowerCase() ?? ''
    const documents = payload.files
      .map(mapNode)
      .filter((document) => !query || document.name.toLocaleLowerCase().includes(query) || document.tags.some((tag) => tag.toLocaleLowerCase().includes(query)))
    const locallyFiltered = options.view !== 'files' && Boolean(query)
    return {
      documents,
      page: payload.page ?? 1,
      limit: payload.limit ?? documents.length,
      total: locallyFiltered ? documents.length : (payload.total ?? documents.length),
      hasNext: Boolean(payload.has_next),
    }
  },

  async getDocument(uid) {
    const payload = await apiRequest<ApiDetailResponse>(`/files/api/v1/${encodeURIComponent(uid)}/`)
    return mapNode(payload.file)
  },

  async getParsedText(uid) {
    const payload = await apiRequest<ApiParsedTextResponse>(`/files/api/v1/${encodeURIComponent(uid)}/parsed_text/`)
    return payload.text
  },

  async getReadiness() {
    const payload = await apiRequest<ApiReadinessResponse>('/files/api/v1/ai/readiness/')
    return {
      totalFiles: payload.total_files,
      searchableFiles: payload.searchable_files,
      readyPercent: payload.ready_percent,
      summary: payload.summary,
      parse: payload.parse,
      embedding: payload.embedding,
      activeEmbeddingGenerationId: payload.active_embedding_generation_id ?? '',
    }
  },

  getDownloadUrl(uid) {
    return `/files/api/v1/${encodeURIComponent(uid)}/download/`
  },

  async uploadDocument(file, options) {
    const formData = new FormData()
    formData.set('file', file)
    if (options.parentUid) formData.set('parent_id', options.parentUid)
    formData.set('ai_processing_enabled', options.aiProcessingEnabled ? '1' : '0')
    const payload = await uploadForm<ApiUploadResponse>('/files/api/v1/upload/', formData, options.onProgress)
    return {
      document: mapNode(payload.file),
      status: payload.status,
      warnings: stringList(payload.warnings),
    }
  },

  async createFolder(name, parentUid) {
    const formData = new FormData()
    formData.set('name', name)
    if (parentUid) formData.set('parent_id', parentUid)
    const payload = await apiRequest<{ folder: ApiNode }>('/files/api/v1/create_folder/', { method: 'POST', body: formData })
    return mapNode(payload.folder)
  },

  async listFolders() {
    const payload = await apiRequest<ApiFolderListResponse>('/files/api/v1/folders/')
    return payload.folders
  },

  async renameDocument(uid, name) {
    const payload = await apiRequest<ApiDetailResponse>(`/files/api/v1/${encodeURIComponent(uid)}/rename/`, { method: 'POST', json: { name } })
    return mapNode(payload.file)
  },

  async moveDocuments(uids, parentUid) {
    const payload = await apiRequest<ApiMutationResponse>('/files/api/v1/bulk/move/', { method: 'POST', json: { uids, parent_id: parentUid ?? 'root' } })
    return mutationResult(payload, uids.length)
  },

  async toggleStar(uid) {
    const payload = await apiRequest<{ starred: boolean }>(`/files/api/v1/toggle_star/${encodeURIComponent(uid)}/`, { method: 'POST', json: {} })
    return payload.starred
  },

  async trashDocuments(uids) {
    const payload = await apiRequest<ApiMutationResponse>('/files/api/v1/bulk/delete/', { method: 'POST', json: { uids } })
    return mutationResult(payload, uids.length)
  },

  async restoreDocuments(uids) {
    const payload = await apiRequest<ApiMutationResponse>('/files/api/v1/bulk/restore/', { method: 'POST', json: { uids } })
    return mutationResult(payload, uids.length)
  },

  async permanentlyDeleteDocuments(uids) {
    const results = await Promise.allSettled(uids.map((uid) => apiRequest(`/files/api/v1/${encodeURIComponent(uid)}/permanent_delete/`, { method: 'DELETE' })))
    const errors = results.flatMap((result, index) => result.status === 'rejected' ? [`${uids[index]}: ${result.reason instanceof Error ? result.reason.message : String(result.reason)}`] : [])
    return { ok: errors.length === 0, count: results.length - errors.length, errors }
  },

  async setAiProcessing(uid, enabled) {
    const payload = await apiRequest<ApiDetailResponse>(`/files/api/v1/${encodeURIComponent(uid)}/ai/enabled/`, { method: 'POST', json: { enabled } })
    return mapNode(payload.file)
  },

  async retryAiProcessing(uid) {
    await apiRequest(`/files/api/v1/${encodeURIComponent(uid)}/ai/retry/`, { method: 'POST', json: {} })
  },

  async listSearchScopes(query = '') {
    const params = new URLSearchParams()
    if (query.trim()) params.set('q', query.trim())
    const suffix = params.size ? `?${params.toString()}` : ''
    const payload = await apiRequest<ApiScopeResponse>(`/files/api/v1/rag/scope-nodes/${suffix}`)
    return payload.nodes.map((node) => ({
      uid: node.uid,
      name: node.name,
      path: node.path,
      nodeType: node.node_type,
      depth: node.depth,
      ext: node.ext,
      fileCount: node.file_count,
    }))
  },

  async searchDocuments(request) {
    const payload = await apiRequest<ApiSearchResponse>('/api/document-ai/v1/search/', {
      method: 'POST',
      json: {
        mode: request.mode,
        query: request.query,
        ...(request.topK === undefined ? {} : { top_k: request.topK }),
        ...(request.threshold === undefined ? {} : { threshold: request.threshold }),
        ...(request.nodeIds.length ? { node_ids: request.nodeIds } : {}),
      },
    })
    const metrics = payload.performance_metrics ?? {}
    return {
      results: payload.results.map(mapSearchResult),
      queryPlan: {
        mode: payload.query_plan.mode,
        source: payload.query_plan.source ?? '',
        retrievalQuery: payload.query_plan.retrieval_query ?? request.query,
        intent: payload.query_plan.intent ?? '',
        confidence: payload.query_plan.confidence ?? null,
        warnings: (payload.query_plan.warnings ?? []).map((warning) => ({ code: warning.code ?? '', message: warning.message ?? '' })),
        filters: payload.query_plan.filters ?? [],
        sorts: payload.query_plan.sorts ?? [],
      },
      performanceMetrics: metrics,
      durationMs: searchDuration(metrics),
    }
  },

  async listRagHistory(limit = 10) {
    const payload = await apiRequest<ApiRagHistoryResponse>(`/files/api/v1/ai/search-history/?limit=${Math.max(1, Math.min(limit, 20))}`)
    return payload.history.map((item) => ({
      id: item.id,
      question: item.question,
      answerPreview: item.answer_preview ?? '',
      answer: item.answer ?? '',
      citations: (item.citations ?? []).map(mapRagCitation),
      resultCount: item.result_count ?? 0,
      language: item.language === 'en' ? 'en' : 'ko',
      nodeIds: item.node_ids ?? [],
      llmModel: item.llm_model ?? '',
      performanceMetrics: item.performance_metrics ?? {},
      completedAt: item.completed_at ?? null,
      createdAt: item.created_at,
    }))
  },

  async listRagConversations() {
    const payload = await apiRequest<{ conversations: ApiRagConversation[] }>('/api/document-ai/v1/rag/conversations/')
    return payload.conversations.map(mapRagConversation)
  },

  async createRagConversation(title, defaultNodeIds) {
    const payload = await apiRequest<{ conversation: ApiRagConversation }>('/api/document-ai/v1/rag/conversations/', {
      method: 'POST',
      json: { title, default_node_ids: defaultNodeIds },
    })
    return mapRagConversation(payload.conversation)
  },

  async getRagConversation(uid) {
    const payload = await apiRequest<{ conversation: ApiRagConversation }>(`/api/document-ai/v1/rag/conversations/${encodeURIComponent(uid)}/`)
    return mapRagConversation(payload.conversation)
  },

  async listRagConversationMessages(uid) {
    const payload = await apiRequest<{ messages: ApiRagMessage[] }>(`/api/document-ai/v1/rag/conversations/${encodeURIComponent(uid)}/messages/`)
    return payload.messages.map(mapRagMessage)
  },

  streamAnswer(request, handlers) {
    const controller = new AbortController()
    handlers.onPhase('searching')

    void (async () => {
      let terminal = false

      const consumeLine = (line: string) => {
        let event: ApiRagEvent
        try {
          event = JSON.parse(line) as ApiRagEvent
        } catch {
          throw new ApiClientError(0, 'INVALID_RAG_STREAM', 'RAG stream contained an invalid event.')
        }

        switch (event.type) {
          case 'started':
            handlers.onStarted({
              jobId: event.job_id,
              llmTarget: event.llm_target ?? '',
              llmModel: event.llm_model ?? '',
            })
            handlers.onPhase('preparing')
            break
          case 'sources':
            handlers.onSources((event.citations ?? []).map(mapRagCitation))
            break
          case 'token':
            handlers.onPhase('streaming')
            if (event.text) handlers.onToken(event.text)
            break
          case 'completed': {
            terminal = true
            const citations = (event.citations ?? []).map(mapRagCitation)
            handlers.onCompleted({
              jobId: event.job_id,
              answer: event.answer ?? '',
              citations,
              performanceMetrics: event.performance_metrics ?? {},
            })
            handlers.onPhase('done')
            break
          }
          case 'canceled':
            terminal = true
            handlers.onCanceled(event.performance_metrics ?? {})
            handlers.onPhase('canceled')
            break
          case 'error':
            terminal = true
            throw new ApiClientError(
              500,
              event.code ?? 'RAG_GENERATION_FAILED',
              event.message ?? 'RAG generation failed.',
              event.performance_metrics ?? {},
            )
          default:
            throw new ApiClientError(0, 'INVALID_RAG_STREAM', 'RAG stream contained an unknown event.')
        }
      }

      try {
        const streamUrl = request.conversationUid
          ? `/api/document-ai/v1/rag/conversations/${encodeURIComponent(request.conversationUid)}/messages/stream/`
          : '/api/document-ai/v1/rag/stream/'
        const response = await apiFetch(streamUrl, {
          method: 'POST',
          signal: controller.signal,
          json: {
            question: request.question,
            ...(request.topK === undefined ? {} : { top_k: request.topK }),
            ...(request.threshold === undefined ? {} : { threshold: request.threshold }),
            language: request.language,
            ...(request.clientRequestId ? { client_request_id: request.clientRequestId } : {}),
            ...(request.nodeIds.length ? { node_ids: request.nodeIds } : {}),
          },
        })
        if (!response.ok) throw await apiError(response)
        if (!response.body) throw new ApiClientError(0, 'RAG_STREAM_UNAVAILABLE', 'This browser cannot read the RAG response stream.')

        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        while (true) {
          const { value, done } = await reader.read()
          buffer += decoder.decode(value, { stream: !done })
          const lines = buffer.split('\n')
          buffer = lines.pop() ?? ''
          lines.map((line) => line.trim()).filter(Boolean).forEach(consumeLine)
          if (done) break
        }
        if (buffer.trim()) consumeLine(buffer.trim())
        if (!terminal) throw new ApiClientError(0, 'RAG_STREAM_INCOMPLETE', 'RAG stream ended before a terminal event.')
      } catch (error) {
        if (controller.signal.aborted) return
        handlers.onPhase('error')
        handlers.onError(error instanceof Error ? error : new Error(String(error)))
      }
    })()

    return () => controller.abort()
  },
}

const mockDocuments: DocumentSummary[] = [
  { uid: 'strategy-2026', name: '2026 제품 전략 보고서.pdf', nodeType: 'file', type: 'PDF', path: '/2026 제품 전략 보고서.pdf', parentUid: null, size: 5033165, sizeLabel: '4.8 MB', chunks: 48, updatedAt: '2026-08-12T09:42:00+09:00', updatedLabel: '2026. 8. 12.', createdAt: '2026-08-12T09:42:00+09:00', createdLabel: '2026. 8. 12.', status: 'ready', starred: true, trashed: false, aiProcessingEnabled: true, aiStatus: null, summary: '제품 검색과 RAG 경험을 단순화하고 운영 복잡도를 줄이기 위한 핵심 전략을 정리한 문서입니다.', tags: ['제품 전략', 'RAG', '운영'] },
  { uid: 'service-policy', name: '서비스 운영 정책.hwp', nodeType: 'file', type: 'HWP', path: '/서비스 운영 정책.hwp', parentUid: null, size: 856064, sizeLabel: '836 KB', chunks: 19, updatedAt: '2026-08-10T11:05:00+09:00', updatedLabel: '2026. 8. 10.', createdAt: '2026-08-10T11:05:00+09:00', createdLabel: '2026. 8. 10.', status: 'processing', starred: false, trashed: false, aiProcessingEnabled: true, aiStatus: null, summary: '단일 운영자 서버를 기준으로 한 런타임과 장애 대응 정책입니다.', tags: ['운영', '정책'] },
]

const searchResults: SearchResult[] = [
  { uid: 'strategy-2026', title: '2026 제품 전략 보고서.pdf', fileType: 'PDF', score: 0.92, text: '문서 검색과 답변 생성을 하나의 흐름으로 단순화하고, 오래 걸리는 파싱과 임베딩만 비동기로 처리한다.', page: '3', section: '아키텍처', evidences: [{ chunkId: 1, text: '문서 검색과 답변 생성을 하나의 흐름으로 단순화한다.', contextText: '문서 검색과 답변 생성을 하나의 흐름으로 단순화하고, 오래 걸리는 파싱과 임베딩만 비동기로 처리한다.', section: '아키텍처', pages: '3', score: 0.92 }] },
  { uid: 'service-policy', title: '서비스 운영 정책.hwp', fileType: 'HWP', score: 0.87, text: '운영자가 관리하는 단일 서버를 기준으로 런타임과 동시성을 자동 결정한다.', page: '4', section: '운영 정책', evidences: [{ chunkId: 2, text: '단일 서버 기준 정책', contextText: '운영자가 관리하는 단일 서버를 기준으로 런타임과 동시성을 자동 결정한다.', section: '운영 정책', pages: '4', score: 0.87 }] },
]

const answerChunks = [
  '핵심 전략은 문서 검색과 답변 생성을 하나의 작업 흐름으로 단순화하는 것입니다. ',
  '문서 파싱과 임베딩처럼 오래 걸리는 작업만 비동기로 처리하고, ',
  '사용자 검색은 즉시 반환하며 RAG 답변은 근거와 함께 스트리밍합니다. [1][2]',
]

const wait = (milliseconds: number) => new Promise((resolve) => window.setTimeout(resolve, milliseconds))

const mockConversations: RagConversation[] = []
const mockConversationMessages = new Map<string, RagConversationMessage[]>()

const mockWorkspaceApi: WorkspaceApi = {
  async listDocuments(options) {
    await wait(120)
    const query = options.query?.trim().toLocaleLowerCase() ?? ''
    let documents = mockDocuments.filter((document) => !document.trashed)
    if (options.view === 'starred') documents = documents.filter((document) => document.starred)
    if (options.view === 'trash') documents = mockDocuments.filter((document) => document.trashed)
    if (query) documents = documents.filter((document) => document.name.toLocaleLowerCase().includes(query))
    return { documents, page: 1, limit: 50, hasNext: false, total: documents.length }
  },
  async getDocument(uid) {
    await wait(80)
    const document = mockDocuments.find((item) => item.uid === uid)
    if (!document) throw new Error('Document not found')
    return document
  },
  async getParsedText() {
    await wait(80)
    return '이 문서는 Dotori SPA 읽기 흐름 검증을 위한 예시 본문입니다.'
  },
  async getReadiness() {
    await wait(80)
    return {
      totalFiles: 2,
      searchableFiles: 1,
      readyPercent: 50,
      summary: '1개 문서가 준비되었습니다.',
      parse: { completed: 1, pending: 0, processing: 1, failed: 0 },
      embedding: { completed: 1, pending: 1, processing: 0, failed: 0, stale: 0 },
      activeEmbeddingGenerationId: 'mock-active-generation',
    }
  },
  getDownloadUrl(uid) { return `/files/api/v1/${encodeURIComponent(uid)}/download/` },
  async uploadDocument(file, options) {
    options.onProgress?.(100)
    const now = new Date().toISOString()
    const document: DocumentSummary = { uid: `mock-${Date.now()}`, name: file.name, nodeType: 'file', type: file.name.split('.').pop()?.toUpperCase() || 'FILE', path: `/${file.name}`, parentUid: options.parentUid ?? null, size: file.size, sizeLabel: formatSize(file.size), chunks: 0, updatedAt: now, updatedLabel: formatDate(now), createdAt: now, createdLabel: formatDate(now), status: options.aiProcessingEnabled ? 'processing' : 'disabled', starred: false, trashed: false, aiProcessingEnabled: options.aiProcessingEnabled, aiStatus: null, summary: '', tags: [] }
    mockDocuments.unshift(document)
    return { document, status: 'done', warnings: [] }
  },
  async createFolder(name, parentUid) {
    const now = new Date().toISOString()
    const folder: DocumentSummary = { uid: `mock-folder-${Date.now()}`, name, nodeType: 'directory', type: 'FOLDER', path: `/${name}`, parentUid: parentUid ?? null, size: null, sizeLabel: '—', chunks: 0, updatedAt: now, updatedLabel: formatDate(now), createdAt: now, createdLabel: formatDate(now), status: 'ready', starred: false, trashed: false, aiProcessingEnabled: false, aiStatus: null, summary: '', tags: [] }
    mockDocuments.unshift(folder)
    return folder
  },
  async listFolders() { return mockDocuments.filter((item) => item.nodeType === 'directory').map(({ uid, name, path }) => ({ uid, name, path })) },
  async renameDocument(uid, name) { const item = mockDocuments.find((document) => document.uid === uid); if (!item) throw new Error('Document not found'); item.name = name; return item },
  async moveDocuments(uids, parentUid) { mockDocuments.filter((item) => uids.includes(item.uid)).forEach((item) => { item.parentUid = parentUid }); return { ok: true, count: uids.length, errors: [] } },
  async toggleStar(uid) { const item = mockDocuments.find((document) => document.uid === uid); if (!item) throw new Error('Document not found'); item.starred = !item.starred; return item.starred },
  async trashDocuments(uids) { mockDocuments.filter((item) => uids.includes(item.uid)).forEach((item) => { item.trashed = true }); return { ok: true, count: uids.length, errors: [] } },
  async restoreDocuments(uids) { mockDocuments.filter((item) => uids.includes(item.uid)).forEach((item) => { item.trashed = false }); return { ok: true, count: uids.length, errors: [] } },
  async permanentlyDeleteDocuments(uids) { uids.forEach((uid) => { const index = mockDocuments.findIndex((item) => item.uid === uid); if (index >= 0) mockDocuments.splice(index, 1) }); return { ok: true, count: uids.length, errors: [] } },
  async setAiProcessing(uid, enabled) { const item = mockDocuments.find((document) => document.uid === uid); if (!item) throw new Error('Document not found'); item.aiProcessingEnabled = enabled; item.status = enabled ? 'processing' : 'disabled'; return item },
  async retryAiProcessing(uid) { const item = mockDocuments.find((document) => document.uid === uid); if (!item) throw new Error('Document not found'); item.status = 'processing' },
  async listSearchScopes() { await wait(80); return mockDocuments.map((document) => ({ uid: document.uid, name: document.name, path: document.path, nodeType: document.nodeType, depth: 0, ext: document.type, fileCount: document.nodeType === 'directory' ? 1 : 0 })) },
  async searchDocuments(request) {
    await wait(550)
    return {
      results: searchResults,
      queryPlan: { mode: request.mode, source: request.mode === 'basic' ? 'direct' : 'mock_query_understanding', retrievalQuery: request.query, intent: request.mode === 'advanced' ? 'question' : '', confidence: request.mode === 'advanced' ? 0.91 : null, warnings: [], filters: [], sorts: [] },
      performanceMetrics: { request_search_ms: 32 },
      durationMs: 32,
    }
  },
  async listRagHistory() { return [] },
  async listRagConversations() { return [...mockConversations] },
  async createRagConversation(title, defaultNodeIds) {
    const now = new Date().toISOString()
    const conversation: RagConversation = {
      uid: crypto.randomUUID(), title, defaultNodeIds, revision: 1,
      createdById: 1, createdAt: now, updatedAt: now,
    }
    mockConversations.unshift(conversation)
    mockConversationMessages.set(conversation.uid, [])
    return conversation
  },
  async getRagConversation(uid) {
    const conversation = mockConversations.find((item) => item.uid === uid)
    if (!conversation) throw new Error('Conversation not found')
    return conversation
  },
  async listRagConversationMessages(uid) {
    return [...(mockConversationMessages.get(uid) ?? [])]
  },
  streamAnswer(_request, handlers) {
    const timers: number[] = []
    const schedule = (callback: () => void, delay: number) => timers.push(window.setTimeout(callback, delay))
    handlers.onPhase('searching')
    const citations: RagCitation[] = searchResults.map((result, index) => ({
      id: index + 1,
      nodeId: result.uid,
      nodeName: result.title,
      chunkId: result.evidences[0]?.chunkId ?? null,
      section: result.section,
      pages: result.page,
      text: result.text,
      docScore: result.score,
      hybridScore: result.evidences[0]?.score ?? null,
      denseScore: null,
      sparseScore: null,
    }))
    schedule(() => {
      handlers.onStarted({ jobId: 1, llmTarget: 'mock', llmModel: 'mock-model' })
      handlers.onSources(citations)
      handlers.onPhase('preparing')
    }, 700)
    schedule(() => {
      handlers.onPhase('streaming')
      answerChunks.forEach((chunk, index) => schedule(() => {
        handlers.onToken(chunk)
        if (index === answerChunks.length - 1) {
          handlers.onCompleted({ jobId: 1, answer: answerChunks.join(''), citations, performanceMetrics: { total_ms: 2390 } })
          handlers.onPhase('done')
        }
      }, index * 520))
    }, 1350)
    return () => timers.forEach(window.clearTimeout)
  },
}

const documentApi = import.meta.env.VITE_USE_MOCK_API === 'true'
  ? mockWorkspaceApi
  : djangoDocumentApi

export const workspaceApi: WorkspaceApi = {
  ...mockWorkspaceApi,
  ...documentApi,
}
