import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiClientError } from '../../api/http'
import {
  operationsApi,
  type OperationEvent,
  type OperationsEvents,
  type OperationsMetrics,
  type OperationsResources,
  type OperationsStatus,
  type OperationsWindow,
  type OperationTrace,
} from '../../api/operations'
import { AsyncState } from '../../components/AsyncState'
import { Icon } from '../../components/Icon'
import { useI18n } from '../../i18n'

interface OperationsFeatureProps {
  authorized: boolean
  authResolved: boolean
}

const PIPELINES = ['upload', 'parse', 'embedding', 'search', 'rag'] as const

function formatNumber(value: number | null | undefined, maximumFractionDigits = 1) {
  return value == null ? '—' : new Intl.NumberFormat(undefined, { maximumFractionDigits }).format(value)
}

function formatDuration(value: number | null | undefined) {
  if (value == null) return '—'
  if (value >= 1000) return `${formatNumber(value / 1000, 2)}s`
  return `${formatNumber(value)}ms`
}

function formatDate(value: string, locale: string) {
  return new Intl.DateTimeFormat(locale === 'ko' ? 'ko-KR' : 'en-US', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(new Date(value))
}

function serviceState(service: { available?: boolean; status?: string; configured?: boolean }) {
  if (service.available === true || service.status === 'healthy') return 'healthy'
  if (service.configured === false) return 'unconfigured'
  return 'warning'
}

function TraceDrawer({ trace, loading, failed, onClose }: {
  trace: OperationTrace | null
  loading: boolean
  failed: boolean
  onClose: () => void
}) {
  const { locale, t } = useI18n()
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [onClose])

  return <div className="drawer-layer operations-drawer-layer">
    <button className="drawer-scrim" aria-label={t('operations.traceClose')} onClick={onClose} />
    <aside className="operations-trace-drawer" aria-label={t('operations.traceTitle')}>
      <header><div><span className="eyebrow">TRACE</span><h2>{trace?.trace_id || t('operations.traceTitle')}</h2></div><div className="trace-header-actions">{trace && <button className="secondary-button" onClick={() => { void navigator.clipboard.writeText(trace.trace_id); setCopied(true) }}><Icon name="check" size={13}/>{copied ? t('operations.copied') : t('operations.copyTrace')}</button>}<button className="icon-button" onClick={onClose} aria-label={t('operations.traceClose')}><Icon name="x" /></button></div></header>
      <div className="operations-trace-scroll">
        {loading && <AsyncState kind="loading" />}
        {failed && <AsyncState kind="error" />}
        {trace && <>
          <p className="trace-log-hint">{trace.log_hint}</p>
          <div className="trace-records">{trace.records.map((record) => <article key={`${record.pipeline}:${record.record_id}`}>
            <div className="trace-record-head"><strong>{t(`operations.pipeline.${record.pipeline}` as Parameters<typeof t>[0])}</strong><span className={`operation-status ${record.status}`}>{record.status}</span></div>
            <dl><div><dt>{t('operations.duration')}</dt><dd>{formatDuration(record.duration_ms)}</dd></div><div><dt>{t('operations.recordedAt')}</dt><dd>{formatDate(record.created_at, locale)}</dd></div></dl>
            {Object.keys(record.metrics).length > 0 && <div className="trace-metrics">{Object.entries(record.metrics).map(([key, value]) => <span key={key}><small>{key}</small><b>{typeof value === 'number' && key.endsWith('_ms') ? formatDuration(value) : String(value)}</b></span>)}</div>}
            {record.error_summary && <p className="operation-error-copy">{record.error_summary}</p>}
          </article>)}</div>
        </>}
      </div>
    </aside>
  </div>
}

export function OperationsFeature({ authorized, authResolved }: OperationsFeatureProps) {
  const { locale, t } = useI18n()
  const [windowKey, setWindowKey] = useState<OperationsWindow>('24h')
  const [status, setStatus] = useState<OperationsStatus | null>(null)
  const [metrics, setMetrics] = useState<OperationsMetrics | null>(null)
  const [events, setEvents] = useState<OperationsEvents | null>(null)
  const [resources, setResources] = useState<OperationsResources | null>(null)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const [forbidden, setForbidden] = useState(false)
  const [collecting, setCollecting] = useState(false)
  const [traceOpen, setTraceOpen] = useState(false)
  const [trace, setTrace] = useState<OperationTrace | null>(null)
  const [traceLoading, setTraceLoading] = useState(false)
  const [traceFailed, setTraceFailed] = useState(false)

  const load = useCallback(async () => {
    if (!authorized) return
    setLoading(true)
    setFailed(false)
    setForbidden(false)
    try {
      const [nextStatus, nextMetrics, nextEvents, nextResources] = await Promise.all([
        operationsApi.getStatus(), operationsApi.getMetrics(windowKey), operationsApi.getEvents(windowKey), operationsApi.getResources(),
      ])
      setStatus(nextStatus)
      setMetrics(nextMetrics)
      setEvents(nextEvents)
      setResources(nextResources)
    } catch (error) {
      setForbidden(error instanceof ApiClientError && (error.status === 401 || error.status === 403))
      setFailed(true)
    } finally {
      setLoading(false)
    }
  }, [authorized, windowKey])

  useEffect(() => { if (authResolved) void load() }, [authResolved, load])

  async function collectResources() {
    setCollecting(true)
    try { setResources(await operationsApi.collectResources()) } finally { setCollecting(false) }
  }

  async function openTrace(event: OperationEvent) {
    if (!event.trace_id) return
    setTraceOpen(true)
    setTrace(null)
    setTraceFailed(false)
    setTraceLoading(true)
    try { setTrace(await operationsApi.getTrace(event.trace_id)) } catch { setTraceFailed(true) } finally { setTraceLoading(false) }
  }

  const serviceEntries = status ? Object.entries(status.services) : []
  const failures = status?.processing.recent_failures ?? []
  const resourceEntries = resources?.snapshots ?? []
  const generatedAt = metrics?.generated_at || status?.generated_at
  const summaries = useMemo(() => metrics ? ([
    ['upload', metrics.summary.upload], ['search', metrics.summary.search], ['rag', metrics.summary.rag],
  ] as const) : [], [metrics])

  if (!authResolved || loading) return <section className="panel operations-panel"><AsyncState kind="loading" /></section>
  if (!authorized || forbidden) return <section className="panel operations-panel"><div className="async-state unauthorized" role="status"><span><Icon name="x" /></span><strong>{t('operations.accessTitle')}</strong><p>{t('operations.accessDescription')}</p></div></section>
  if (failed || !status || !metrics || !events || !resources) return <section className="panel operations-panel"><AsyncState kind="error" onRetry={() => void load()} /></section>

  return <section className="operations-page">
    <div className="operations-heading">
      <div><span className="eyebrow">{t('operations.eyebrow')}</span><h1>{t('operations.title')}</h1><p>{t('operations.description')}</p></div>
      <div className="operations-heading-actions"><div className="operations-window" role="group" aria-label={t('operations.window')}>{(['1h', '24h', '7d'] as OperationsWindow[]).map((key) => <button key={key} className={windowKey === key ? 'active' : ''} onClick={() => setWindowKey(key)}>{key}</button>)}</div><button className="secondary-button" onClick={() => void load()}><Icon name="refresh" size={14}/>{t('operations.refresh')}</button></div>
    </div>
    {generatedAt && <div className="operations-updated">{t('operations.updatedAt', { date: formatDate(generatedAt, locale) })}</div>}

    <div className="operations-service-grid">{serviceEntries.map(([name, service]) => {
      const state = serviceState(service)
      return <article className="panel operation-service" key={name}><div><span className={`service-indicator ${state}`} /><strong>{t(`operations.service.${name}` as Parameters<typeof t>[0])}</strong></div><b>{t(`operations.state.${state}` as Parameters<typeof t>[0])}</b><small>{service.model || service.runtime || (service.latency_ms != null ? `${formatNumber(service.latency_ms)}ms` : service.status || '—')}</small></article>
    })}</div>

    <div className="operations-summary-grid">{summaries.map(([name, summary]) => <article className="panel operation-summary" key={name}><div><span>{t(`operations.pipeline.${name}` as Parameters<typeof t>[0])}</span><small>{summary.total_count} {t('operations.requests')}</small></div><strong>{summary.success_rate == null ? '—' : `${formatNumber(summary.success_rate)}%`}</strong><p>{t('operations.successRate')}</p><dl><div><dt>{t('operations.average')}</dt><dd>{formatDuration(summary.duration.average)}</dd></div><div><dt>{t('operations.maximum')}</dt><dd>{formatDuration(summary.duration.maximum)}</dd></div><div><dt>{t('operations.failures')}</dt><dd>{summary.failure_count}</dd></div><div><dt>{t('operations.timeouts')}</dt><dd>{summary.timeout_count}</dd></div></dl></article>)}</div>

    <div className="operations-grid-two">
      <section className="panel operations-section"><div className="operations-section-head"><div><span className="eyebrow">PIPELINES</span><h2>{t('operations.pipelineMetrics')}</h2></div><small>{t('operations.measurementHint')}</small></div><div className="pipeline-metric-table">{PIPELINES.map((name) => {
        const pipeline = metrics.pipelines.find((item) => item.name === name)
        const primary = pipeline?.metrics[pipeline.primary_duration]
        const measured = Object.entries(pipeline?.metrics ?? {}).filter(([, metric]) => metric.measured_count > 0)
        return <details key={name}><summary><span><i className={`service-indicator ${pipeline?.count ? 'healthy' : 'unconfigured'}`} /><strong>{t(`operations.pipeline.${name}` as Parameters<typeof t>[0])}</strong></span><span>{pipeline?.count ?? 0}</span><span>{formatDuration(primary?.average)}</span><span>{primary ? `${primary.measured_count}/${primary.total_count}` : '—'}</span></summary><div className="pipeline-metric-details">{measured.length === 0 ? <p>{t('operations.noMeasurements')}</p> : measured.map(([key, metric]) => <span key={key}><small>{key}</small><b>{t('operations.average')} {metric.unit === 'ms' ? formatDuration(metric.average) : formatNumber(metric.average)}</b><em>{t('operations.maximum')} {metric.unit === 'ms' ? formatDuration(metric.maximum) : formatNumber(metric.maximum)} · {metric.measured_count}/{metric.total_count}</em></span>)}</div></details>
      })}</div></section>

      <section className="panel operations-section"><div className="operations-section-head"><div><span className="eyebrow">PROCESSING</span><h2>{t('operations.processing')}</h2></div></div><div className="processing-overview"><article><strong>{t('operations.pipeline.parse')}</strong><span>{status.processing.parse.counts.processing || 0} {t('operations.inProgress')}</span><small>{status.processing.parse.stale_count} {t('operations.stale')}</small></article><article><strong>{t('operations.pipeline.embedding')}</strong><span>{status.processing.embedding.counts.processing || 0} {t('operations.inProgress')}</span><small>{status.processing.embedding.stale_count} {t('operations.stale')}</small><small>{status.processing.embedding.contract_mismatch_count || 0} {t('operations.contractMismatch')}</small></article></div><div className="admission-note"><Icon name="clock" size={15}/><p>{status.admission.available ? `${formatNumber(status.admission.active)}/${formatNumber(status.admission.limit)}` : t('operations.admissionUnavailable')}</p></div></section>
    </div>

    <div className="operations-grid-two">
      <section className="panel operations-section"><div className="operations-section-head"><div><span className="eyebrow">EVENTS</span><h2>{t('operations.slowAndFailed')}</h2></div></div>{events.events.length === 0 ? <p className="operations-empty">{t('operations.noEvents')}</p> : <div className="operation-event-list">{events.events.map((event) => <button key={event.key} disabled={!event.trace_id} onClick={() => void openTrace(event)}><span className={`operation-event-icon ${event.is_failure ? 'failed' : ''}`}><Icon name={event.is_failure ? 'x' : 'clock'} size={14}/></span><span><strong>{t(`operations.pipeline.${event.pipeline}` as Parameters<typeof t>[0])} · #{event.record_id}</strong><small>{event.error_summary || (event.dominant_metric ? `${event.dominant_metric.key}: ${formatDuration(event.dominant_metric.value)}` : event.status)}</small></span><b>{formatDuration(event.duration_ms)}</b>{event.trace_id && <Icon name="chevron" size={14}/>}</button>)}</div>}</section>

      <section className="panel operations-section"><div className="operations-section-head"><div><span className="eyebrow">RESOURCES</span><h2>{t('operations.resources')}</h2></div><button className="secondary-button" disabled={collecting} onClick={() => void collectResources()}><Icon name="refresh" size={14}/>{collecting ? t('operations.collecting') : t('operations.collect')}</button></div>{resourceEntries.length === 0 ? <p className="operations-empty">{t('operations.noResources')}</p> : <div className="resource-list">{resourceEntries.map((snapshot) => <div key={snapshot.service}><span><strong>{snapshot.service}</strong><small>{formatDate(snapshot.collected_at, locale)}</small></span><b>{snapshot.disk_free_mb != null ? `${formatNumber(snapshot.disk_free_mb / 1024, 2)} GB ${t('operations.free')}` : snapshot.db_connections != null ? `${snapshot.db_connections} ${t('operations.connections')}` : `${formatNumber(snapshot.mem_mb)} MB`}</b></div>)}</div>}</section>
    </div>

    <section className="panel operations-section"><div className="operations-section-head"><div><span className="eyebrow">SERVER</span><h2>{t('operations.serverContext')}</h2></div><small>{t('operations.readOnly')}</small></div><div className="server-context-grid"><div><small>{t('operations.operationMode')}</small><strong>{status.server.operation_mode}</strong></div><div><small>{t('operations.webWorkers')}</small><strong>{status.server.web_workers}</strong></div><div><small>{t('operations.requestTimeout')}</small><strong>{status.server.request_timeout_seconds}s</strong></div><div><small>{t('operations.parseWorkers')}</small><strong>{status.server.parse_worker_concurrency}</strong></div><div><small>{t('operations.embeddingWorkers')}</small><strong>{status.server.embedding_worker_concurrency}</strong></div><div><small>{t('operations.buildRevision')}</small><strong>{status.server.build_revision || '—'}</strong></div><div><small>{t('operations.hostMetrics')}</small><strong>{t('operations.notCollected')}</strong></div></div></section>

    {failures.length > 0 && <section className="panel operations-section operations-failures"><div className="operations-section-head"><div><span className="eyebrow">FAILURES</span><h2>{t('operations.recentFailures')}</h2></div></div><div className="failure-table">{failures.map((failure) => <div key={`${failure.pipeline}:${failure.record_id}`}><span><strong>{failure.document_name}</strong><small>{failure.pipeline} · {formatDate(failure.failed_at, locale)}</small></span><p>{failure.error_summary || '—'}</p><b>{t('operations.retries', { count: failure.recovery_attempts })}</b></div>)}</div></section>}

    {traceOpen && <TraceDrawer trace={trace} loading={traceLoading} failed={traceFailed} onClose={() => setTraceOpen(false)} />}
  </section>
}
