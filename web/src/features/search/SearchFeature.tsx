import { useEffect, useRef, useState } from 'react'
import type { SearchMode, SearchQueryPlan, SearchResult, SearchScopeNode } from '../../api/models'
import { ApiClientError } from '../../api/http'
import { workspaceApi } from '../../api/workspace'
import { AsyncState, type AsyncStateKind } from '../../components/AsyncState'
import { Icon } from '../../components/Icon'
import { ResultCard } from '../../components/ResultCard'
import { useI18n } from '../../i18n'

type SearchState = AsyncStateKind | 'ready'

const COMPOSE_MAX_HEIGHT = 120

function autoGrow(el: HTMLTextAreaElement) {
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, COMPOSE_MAX_HEIGHT)}px`
}

function filterLabel(filter: Record<string, unknown>): string {
  if (typeof filter.source_text === 'string' && filter.source_text) return filter.source_text
  return [filter.field, filter.operator, filter.value]
    .filter((value) => value !== undefined && value !== '')
    .map(String)
    .join(' ')
}

export function SearchFeature({ onOpenDocument }: { onOpenDocument: (uid: string) => void }) {
  const { t } = useI18n()
  const [mode, setMode] = useState<SearchMode>('basic')
  const [query, setQuery] = useState('')
  const [scopes, setScopes] = useState<SearchScopeNode[]>([])
  const [selectedNodeIds, setSelectedNodeIds] = useState<string[]>([])
  const [scopeLoading, setScopeLoading] = useState(true)
  const [scopeError, setScopeError] = useState(false)
  const [scopeOpen, setScopeOpen] = useState(false)
  const [results, setResults] = useState<SearchResult[]>([])
  const [queryPlan, setQueryPlan] = useState<SearchQueryPlan | null>(null)
  const [durationMs, setDurationMs] = useState<number | null>(null)
  const [errorDetail, setErrorDetail] = useState('')
  const [state, setState] = useState<SearchState>('empty')
  const scopeAnchorRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    let active = true
    workspaceApi.listSearchScopes()
      .then((nodes) => {
        if (!active) return
        setScopes(nodes)
        setScopeError(false)
      })
      .catch(() => {
        if (active) setScopeError(true)
      })
      .finally(() => {
        if (active) setScopeLoading(false)
      })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (textareaRef.current) autoGrow(textareaRef.current)
  }, [query])

  useEffect(() => {
    if (!scopeOpen) return
    function onPointerDown(event: MouseEvent) {
      if (scopeAnchorRef.current && !scopeAnchorRef.current.contains(event.target as Node)) setScopeOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setScopeOpen(false)
    }
    window.addEventListener('mousedown', onPointerDown)
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('mousedown', onPointerDown)
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [scopeOpen])

  function selectMode(nextMode: SearchMode) {
    if (nextMode === 'advanced') return
    setMode(nextMode)
    setQueryPlan(null)
    setErrorDetail('')
  }

  function toggleScope(uid: string) {
    setSelectedNodeIds((current) => current.includes(uid)
      ? current.filter((item) => item !== uid)
      : [...current, uid])
  }

  async function runSearch() {
    const normalizedQuery = query.trim()
    if (!normalizedQuery) {
      setErrorDetail(t('search.queryRequired'))
      return
    }
    setErrorDetail('')
    setState('loading')
    try {
      const response = await workspaceApi.searchDocuments({
        mode,
        query: normalizedQuery,
        nodeIds: selectedNodeIds,
      })
      setResults(response.results)
      setQueryPlan(response.queryPlan)
      setDurationMs(response.durationMs)
      setState(response.results.length ? 'ready' : 'empty')
    } catch (error) {
      setErrorDetail(error instanceof Error ? error.message : t('state.errorDescription'))
      setState(error instanceof ApiClientError && error.unauthorized ? 'unauthorized' : 'error')
    }
  }

  return <section className="panel search-panel">
    <div className="document-header">
      <div><span className="eyebrow">{t('search.workspace')}</span><h1>{t('search.documentSearch')}</h1><p className="search-description">{mode === 'basic' ? t('search.basicDescription') : t('search.advancedDescription')}</p></div>
      <div className="search-mode-switch" role="tablist" aria-label={t('search.mode')}>
        <button role="tab" aria-selected={mode === 'basic'} className={mode === 'basic' ? 'active' : ''} onClick={() => selectMode('basic')}>{t('search.basic')}</button>
        <button role="tab" aria-selected={mode === 'advanced'} className={mode === 'advanced' ? 'active' : ''} disabled title={t('search.advancedLocked')} onClick={() => selectMode('advanced')}>{t('search.advanced')}<span className="future-badge">{t('future.badge')}</span></button>
      </div>
    </div>
    <form className="chat-compose search-compose" onSubmit={(event) => { event.preventDefault(); void runSearch() }}>
      <div className="chat-compose-chips">
        <div className="chat-compose-tool-group" ref={scopeAnchorRef}>
          <button type="button" className={`compose-chip ${selectedNodeIds.length ? 'active' : ''}`} onClick={() => setScopeOpen((value) => !value)}>
            <Icon name="layers" size={13}/>{selectedNodeIds.length ? t('search.selectedScope', { count: selectedNodeIds.length }) : t('search.allDocuments')}
          </button>
          {scopeOpen && <div className="scope-popover">
            <div className="scope-popover-head">
              <strong>{t('search.scope')}</strong>
              <button type="button" onClick={() => setScopeOpen(false)} aria-label={t('search.scopeClose')}><Icon name="x" size={13}/></button>
            </div>
            <p>{selectedNodeIds.length ? t('search.scopeSelected', { count: selectedNodeIds.length }) : t('search.scopeAllDescription')}</p>
            {scopeLoading && <small>{t('search.scopeLoading')}</small>}
            {scopeError && <small className="scope-error">{t('search.scopeFailed')}</small>}
            {!scopeLoading && !scopeError && !scopes.length && <small>{t('search.noScopes')}</small>}
            {!scopeLoading && !scopeError && !!scopes.length && <div className="chat-scope-list">{scopes.map((node) => <label key={node.uid} style={{ paddingLeft: `${node.depth * 8}px` }}><input type="checkbox" checked={selectedNodeIds.includes(node.uid)} onChange={() => toggleScope(node.uid)}/><span><strong>{node.name}</strong><small>{node.nodeType === 'directory' ? t('search.folderFiles', { count: node.fileCount }) : node.ext.replace('.', '').toUpperCase()}</small></span></label>)}</div>}
            {!!selectedNodeIds.length && <button type="button" className="scope-clear" onClick={() => setSelectedNodeIds([])}>{t('search.clearScope')}</button>}
          </div>}
        </div>
      </div>
      <textarea
        ref={textareaRef}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault()
            if (state !== 'loading') void runSearch()
          }
        }}
        placeholder={mode === 'basic' ? t('search.basicPlaceholder') : t('search.advancedPlaceholder')}
        rows={1}
      />
      <button className="send-button" type="submit" disabled={state === 'loading'} aria-label={t('search.run')}><Icon name="search" size={17}/></button>
    </form>
    {errorDetail && state !== 'error' && state !== 'unauthorized' && <p className="field-error" role="alert">{errorDetail}</p>}
    {mode === 'advanced' && queryPlan && <div className="query-plan"><Icon name="sparkles" size={15}/><div><strong>{t('search.interpretedQuery')}</strong><p>{queryPlan.retrievalQuery}</p><small>{t('search.planMeta', { source: queryPlan.source, confidence: queryPlan.confidence === null ? '—' : `${Math.round(queryPlan.confidence * 100)}%` })}</small>{!!queryPlan.filters.length && <small>{t('search.planFilters')}: {queryPlan.filters.map(filterLabel).join(' · ')}</small>}{queryPlan.warnings.map((warning) => <small className="plan-warning" key={`${warning.code}-${warning.message}`}>{warning.message}</small>)}</div></div>}
    <div className="results-header"><div><span className="eyebrow">{t('search.topResults')}</span><h2>{t('search.results')}</h2></div>{durationMs !== null && <span>{t('search.duration', { duration: durationMs.toFixed(1) })}</span>}</div>
    {(state === 'error' || state === 'unauthorized') && errorDetail && <p className="search-error-detail" role="alert">{errorDetail}</p>}
    {state === 'ready' ? <div className="result-list">{results.map((result, index) => <ResultCard key={result.uid} result={result} rank={index + 1} onOpen={() => onOpenDocument(result.uid)} />)}</div> : <AsyncState kind={state} onRetry={state === 'error' ? () => void runSearch() : undefined} />}
  </section>
}
