export type DocumentView = 'files' | 'recent' | 'starred' | 'trash'
export type DocumentProcessingStatus = 'ready' | 'processing' | 'failed' | 'stale' | 'disabled'
export type DocumentNodeType = 'file' | 'directory'

export interface DocumentAiStatus {
  parseStatus: string
  parseLabel: string
  embeddingStatus: string
  embeddingLabel: string
  chunkCount: number
  completedChunks: number
  failedChunks: number
  embeddingContractStatus: string
  activeEmbeddingGenerationId: string
  embeddedGenerationIds: string[]
  reembeddingRequired: boolean
  searchable: boolean
}

export interface DocumentSummary {
  uid: string
  name: string
  nodeType: DocumentNodeType
  type: string
  path: string
  parentUid: string | null
  size: number | null
  sizeLabel: string
  chunks: number
  updatedAt: string
  updatedLabel: string
  createdAt: string
  createdLabel: string
  status: DocumentProcessingStatus
  starred: boolean
  trashed: boolean
  aiProcessingEnabled: boolean
  aiStatus: DocumentAiStatus | null
  summary: string
  tags: string[]
}

export interface DocumentListOptions {
  view: DocumentView
  query?: string
  parentUid?: string | null
  page?: number
  limit?: number
}

export interface DocumentListResult {
  documents: DocumentSummary[]
  page: number
  limit: number
  hasNext: boolean
  total: number
}

export interface DocumentFolder {
  uid: string
  name: string
  path: string
}

export interface DocumentMutationResult {
  ok: boolean
  count: number
  errors: string[]
}

export interface DocumentUploadResult {
  document: DocumentSummary
  status: 'done' | 'duplicate'
  warnings: string[]
}

export interface DocumentReadiness {
  totalFiles: number
  searchableFiles: number
  readyPercent: number
  summary: string
  parse: { completed: number; pending: number; processing: number; failed: number }
  embedding: { completed: number; pending: number; processing: number; failed: number; stale: number }
  activeEmbeddingGenerationId: string
}

export type SearchMode = 'basic' | 'advanced'

export interface SearchScopeNode {
  uid: string
  name: string
  path: string
  nodeType: DocumentNodeType
  depth: number
  ext: string
  fileCount: number
}

export interface SearchEvidence {
  chunkId: number
  text: string
  contextText: string
  section: string
  pages: string
  score: number | null
}

export interface SearchResult {
  uid: string
  title: string
  fileType: string
  score: number
  text: string
  page: string
  section: string
  evidences: SearchEvidence[]
}

export interface SearchRequest {
  mode: SearchMode
  query: string
  topK?: number
  threshold?: number
  nodeIds: string[]
}

export interface SearchQueryPlan {
  mode: SearchMode
  source: string
  retrievalQuery: string
  intent: string
  confidence: number | null
  warnings: Array<{ code: string; message: string }>
  filters: Array<Record<string, unknown>>
  sorts: Array<Record<string, unknown>>
}

export interface SearchResponse {
  results: SearchResult[]
  queryPlan: SearchQueryPlan
  performanceMetrics: Record<string, unknown>
  durationMs: number | null
}

export type ChatPhase = 'idle' | 'searching' | 'preparing' | 'streaming' | 'done' | 'canceled' | 'error'

export interface RagRequest {
  question: string
  topK?: number
  threshold?: number
  language: 'ko' | 'en'
  nodeIds: string[]
  conversationUid?: string
  clientRequestId?: string
}

export interface RagCitation {
  id: number
  nodeId: string
  nodeName: string
  chunkId: number | null
  section: string
  pages: string
  text: string
  docScore: number | null
  hybridScore: number | null
  denseScore: number | null
  sparseScore: number | null
}

export interface RagStarted {
  jobId: number
  llmTarget: string
  llmModel: string
}

export interface RagCompleted {
  jobId: number
  answer: string
  citations: RagCitation[]
  performanceMetrics: Record<string, unknown>
}

export interface RagHistoryItem {
  id: number
  question: string
  answerPreview: string
  answer: string
  citations: RagCitation[]
  resultCount: number
  language: 'ko' | 'en'
  nodeIds: string[]
  llmModel: string
  performanceMetrics: Record<string, unknown>
  completedAt: string | null
  createdAt: string
}

export interface RagConversation {
  uid: string
  title: string
  defaultNodeIds: string[]
  revision: number
  createdById: number | null
  createdAt: string
  updatedAt: string
}

export interface RagConversationMessage {
  uid: string
  sequence: number
  role: 'user' | 'assistant'
  content: string
  nodeIds: string[]
  replyToUid: string | null
  createdAt: string
  ragJob: null | {
    id: number
    status: string
    answer: string
    citations: RagCitation[]
    errorMessage: string
    performanceMetrics: Record<string, unknown>
    completedAt: string | null
  }
}

export interface ServerPolicySummary {
  operationMode: string
  policy: {
    searchStrategy: string
    searchTopK: number
    retrievalThreshold: number | null
  }
  rag: {
    configured: boolean
    available: boolean
    status: string
    reasonCode: string
    updatedAt: string | null
    model: string
    runtime: string
    priorityPreset: string
    selectionMode: string
    servingConcurrency: number
  }
  embedding: {
    enabled: boolean
    configured: boolean
    status: string
    model: string
    provider: string
    dimension: number
    sparseEnabled: boolean
    distanceStrategy: string
  }
}

export interface RagStreamHandlers {
  onPhase: (phase: ChatPhase) => void
  onStarted: (started: RagStarted) => void
  onSources: (citations: RagCitation[]) => void
  onToken: (text: string) => void
  onCompleted: (completed: RagCompleted) => void
  onCanceled: (performanceMetrics: Record<string, unknown>) => void
  onError: (error: Error) => void
}
