import { apiRequest } from './http'

export type OperationsWindow = '1h' | '24h' | '7d'

export interface MetricStat {
  average: number | null
  maximum: number | null
  measured_count: number
  total_count: number
  unit: 'ms' | 'count' | 'tokens_per_second'
}

export interface StatusSummary {
  total_count: number
  terminal_count: number
  success_count: number
  failure_count: number
  canceled_count: number
  in_progress_count: number
  timeout_count: number
  success_rate: number | null
  duration: MetricStat
}

export interface OperationsMetrics {
  ok: true
  generated_at: string
  window: { key: OperationsWindow; from: string; to: string; timezone: string }
  summary: Record<'upload' | 'search' | 'rag', StatusSummary>
  pipelines: Array<{
    name: 'upload' | 'parse' | 'embedding' | 'search' | 'rag'
    count: number
    primary_duration: string
    metrics: Record<string, MetricStat>
  }>
}

export interface ServiceStatus {
  available?: boolean
  status?: string
  latency_ms?: number
  configured?: boolean
  runtime?: string
  model?: string
  error?: string
}

export interface OperationsStatus {
  ok: true
  generated_at: string
  services: Record<'app' | 'database' | 'embedding' | 'rag', ServiceStatus>
  processing: {
    parse: { counts: Record<string, number>; stale_count: number; unit: string }
    embedding: {
      counts: Record<string, number>
      stale_count: number
      contract_mismatch_count: number
      active_generation_id: string
      unit: string
    }
    recent_failures: Array<{
      pipeline: string
      record_id: number
      document_uid: string
      document_name: string
      failed_at: string
      recovery_attempts: number
      error_summary: string
    }>
  }
  admission: { available: boolean; active: number | null; limit: number | null; rejected_count: number | null }
  server: {
    operation_mode: string
    web_workers: number
    request_timeout_seconds: number
    parse_worker_concurrency: number
    embedding_worker_concurrency: number
    build_revision: string
  }
}

export interface OperationEvent {
  key: string
  pipeline: string
  operation: string
  record_id: number
  status: string
  created_at: string
  duration_ms: number | null
  duration_metric: string | null
  dominant_metric: { key: string; value: number; unit: string } | null
  trace_id: string
  error_summary: string
  is_failure: boolean
}

export interface OperationsEvents {
  ok: true
  generated_at: string
  window: { key: OperationsWindow; from: string; to: string; timezone: string }
  events: OperationEvent[]
}

export interface ResourceSnapshot {
  service: string
  cpu_percent: number | null
  mem_mb: number | null
  gpu_mem_mb: number | null
  db_connections: number | null
  disk_free_mb: number | null
  collected_at: string
}

export interface OperationsResources {
  ok: true
  snapshots: ResourceSnapshot[]
  skipped?: Array<{ service: string; reason: string }>
}

export interface TraceRecord {
  pipeline: string
  operation: string
  record_id: number
  status: string
  created_at: string
  started_at: string | null
  completed_at: string | null
  duration_ms: number | null
  duration_metric: string | null
  metrics: Record<string, number | boolean | string>
  metadata: Record<string, number | boolean | string | null>
  error_summary: string
}

export interface OperationTrace {
  ok: true
  trace_id: string
  records: TraceRecord[]
  log_hint: string
}

function query(window: OperationsWindow) {
  return new URLSearchParams({ window }).toString()
}

export const operationsApi = {
  getStatus: () => apiRequest<OperationsStatus>('/api/document-ai/v1/operations/status/'),
  getMetrics: (window: OperationsWindow) => apiRequest<OperationsMetrics>(`/api/document-ai/v1/operations/metrics/?${query(window)}`),
  getEvents: (window: OperationsWindow) => apiRequest<OperationsEvents>(`/api/document-ai/v1/operations/events/?${query(window)}`),
  getResources: () => apiRequest<OperationsResources>('/api/document-ai/v1/operations/resources/'),
  collectResources: () => apiRequest<OperationsResources>('/api/document-ai/v1/operations/resources/collect/', { method: 'POST' }),
  getTrace: (traceId: string) => apiRequest<OperationTrace>(`/api/document-ai/v1/operations/traces/${encodeURIComponent(traceId)}/`),
}
