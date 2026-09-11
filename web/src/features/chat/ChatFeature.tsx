import { type FormEvent, useEffect, useRef, useState } from 'react'
import { ApiClientError } from '../../api/http'
import { llmSettingsApi, type LLMSettings } from '../../api/llmSettings'
import type { ChatPhase, RagCitation, RagConversation, RagConversationMessage, RagStarted, SearchResult, SearchScopeNode } from '../../api/models'
import { workspaceApi } from '../../api/workspace'
import { Icon } from '../../components/Icon'
import { ResultCard } from '../../components/ResultCard'
import { useI18n } from '../../i18n'

function citationResult(citation: RagCitation): SearchResult {
  const score = citation.docScore ?? citation.hybridScore ?? citation.denseScore ?? 0
  const fileType = (citation.nodeName.split('.').pop() || 'FILE').replace('.', '').toUpperCase()
  return {
    uid: `${citation.nodeId}:${citation.id}`,
    title: citation.nodeName || citation.nodeId,
    fileType,
    score,
    text: citation.text,
    page: citation.pages,
    section: citation.section,
    evidences: [{
      chunkId: citation.chunkId ?? 0,
      text: citation.text,
      contextText: citation.text,
      section: citation.section,
      pages: citation.pages,
      score: citation.hybridScore ?? citation.docScore,
    }],
  }
}

function durationMetric(metrics: Record<string, unknown>): number | null {
  for (const key of ['end_to_end_ms', 'worker_total_ms', 'llm_total_ms', 'total_ms']) {
    if (typeof metrics[key] === 'number') return Math.round(metrics[key])
  }
  return null
}

const COMPOSE_MAX_HEIGHT = 120

function autoGrow(el: HTMLTextAreaElement) {
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, COMPOSE_MAX_HEIGHT)}px`
}

interface ChatFeatureProps {
  conversationUid: string | null
  onConversationChange: (uid: string | null) => void
  onOpenDocument: (uid: string) => void
}

export function ChatFeature({ conversationUid, onConversationChange, onOpenDocument }: ChatFeatureProps) {
  const { t, locale } = useI18n()
  const [question, setQuestion] = useState('')
  const [submittedQuestion, setSubmittedQuestion] = useState('')
  const [answer, setAnswer] = useState('')
  const [citations, setCitations] = useState<RagCitation[]>([])
  const [phase, setPhase] = useState<ChatPhase>('idle')
  const [error, setError] = useState<Error | null>(null)
  const [started, setStarted] = useState<RagStarted | null>(null)
  const [metrics, setMetrics] = useState<Record<string, unknown>>({})
  const [scopes, setScopes] = useState<SearchScopeNode[]>([])
  const [selectedNodeIds, setSelectedNodeIds] = useState<string[]>([])
  const [scopeLoading, setScopeLoading] = useState(true)
  const [scopeFailed, setScopeFailed] = useState(false)
  const [conversations, setConversations] = useState<RagConversation[]>([])
  const [messages, setMessages] = useState<RagConversationMessage[]>([])
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyFailed, setHistoryFailed] = useState(false)
  const [scopeOpen, setScopeOpen] = useState(false)
  const [llmSettings, setLlmSettings] = useState<LLMSettings | null>(null)
  const [llmOpen, setLlmOpen] = useState(false)
  const [llmSaving, setLlmSaving] = useState(false)
  const [llmError, setLlmError] = useState(false)
  const cancelRef = useRef<() => void>(() => undefined)
  const skipConversationLoadRef = useRef<string | null>(null)
  const scopeAnchorRef = useRef<HTMLDivElement>(null)
  const llmAnchorRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const busy = phase === 'searching' || phase === 'preparing' || phase === 'streaming'

  useEffect(() => {
    let active = true
    workspaceApi.listSearchScopes()
      .then((nodes) => { if (active) setScopes(nodes) })
      .catch(() => { if (active) setScopeFailed(true) })
      .finally(() => { if (active) setScopeLoading(false) })
    workspaceApi.listRagConversations()
      .then((items) => { if (active) setConversations(items) })
      .catch(() => { if (active) setHistoryFailed(true) })
      .finally(() => { if (active) setHistoryLoading(false) })
    llmSettingsApi.get()
      .then((settings) => { if (active) setLlmSettings(settings) })
      .catch(() => undefined)
    return () => {
      active = false
      cancelRef.current()
    }
  }, [])

  useEffect(() => {
    let active = true
    if (conversationUid && skipConversationLoadRef.current === conversationUid) {
      skipConversationLoadRef.current = null
      return () => { active = false }
    }
    cancelRef.current()
    cancelRef.current = () => undefined
    setSubmittedQuestion('')
    setAnswer('')
    setCitations([])
    setError(null)
    setStarted(null)
    setMetrics({})
    setPhase('idle')
    if (!conversationUid) {
      setMessages([])
      return () => { active = false }
    }
    Promise.all([
      workspaceApi.getRagConversation(conversationUid),
      workspaceApi.listRagConversationMessages(conversationUid),
    ]).then(([conversation, items]) => {
      if (!active) return
      setMessages(items)
      setSelectedNodeIds(conversation.defaultNodeIds)
      const latestAssistant = [...items].reverse().find((message) => message.role === 'assistant' && message.ragJob)
      if (latestAssistant?.ragJob) {
        setCitations(latestAssistant.ragJob.citations)
        setMetrics(latestAssistant.ragJob.performanceMetrics)
      }
    }).catch((loadError) => {
      if (active) setError(loadError instanceof Error ? loadError : new Error(String(loadError)))
    })
    return () => { active = false }
  }, [conversationUid])

  useEffect(() => {
    if (textareaRef.current) autoGrow(textareaRef.current)
  }, [question])

  useEffect(() => {
    if (!scopeOpen && !llmOpen) return
    function onPointerDown(event: MouseEvent) {
      if (scopeOpen && scopeAnchorRef.current && !scopeAnchorRef.current.contains(event.target as Node)) setScopeOpen(false)
      if (llmOpen && llmAnchorRef.current && !llmAnchorRef.current.contains(event.target as Node)) setLlmOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape') return
      setScopeOpen(false)
      setLlmOpen(false)
    }
    window.addEventListener('mousedown', onPointerDown)
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('mousedown', onPointerDown)
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [scopeOpen, llmOpen])

  const statusText = phase === 'searching'
    ? t('chat.status.searching')
    : phase === 'preparing'
      ? t('chat.status.preparing', { count: citations.length })
      : phase === 'streaming'
        ? t('chat.status.streaming')
        : phase === 'done'
          ? t('chat.status.done')
          : phase === 'canceled'
            ? t('chat.status.canceled')
            : phase === 'error'
              ? t('chat.status.error')
              : t('chat.status.idle')

  function errorMessage(current: Error): string {
    if (!(current instanceof ApiClientError)) return current.message
    if (current.code === 'RAG_CAPACITY_EXCEEDED') {
      return current.retryAfterSeconds === null
        ? t('chat.capacity')
        : t('chat.capacityRetry', { seconds: current.retryAfterSeconds })
    }
    if (current.code === 'LLM_RUNTIME_UNAVAILABLE') return t('chat.runtimeUnavailable')
    if (current.code === 'RAG_STREAM_INCOMPLETE') return t('chat.streamIncomplete')
    if (current.code === 'RAG_GENERATION_FAILED') return t('chat.generationFailed')
    if (current.code === 'SEARCH_FAILED') return t('chat.searchFailed')
    return current.message
  }

  async function startRequest() {
    const nextQuestion = question.trim()
    if (!nextQuestion) return
    let targetConversationUid = conversationUid
    if (!targetConversationUid) {
      try {
        const created = await workspaceApi.createRagConversation(nextQuestion.slice(0, 160), selectedNodeIds)
        targetConversationUid = created.uid
        skipConversationLoadRef.current = created.uid
        setConversations((current) => [created, ...current.filter((item) => item.uid !== created.uid)])
        onConversationChange(created.uid)
      } catch (createError) {
        setPhase('error')
        setError(createError instanceof Error ? createError : new Error(String(createError)))
        return
      }
    }
    cancelRef.current()
    setSubmittedQuestion(nextQuestion)
    setAnswer('')
    setCitations([])
    setError(null)
    setStarted(null)
    setMetrics({})
    const requestId = crypto.randomUUID()
    cancelRef.current = workspaceApi.streamAnswer({
      question: nextQuestion,
      language: locale,
      nodeIds: selectedNodeIds,
      conversationUid: targetConversationUid,
      clientRequestId: requestId,
    }, {
      onPhase: setPhase,
      onStarted: setStarted,
      onSources: setCitations,
      onToken: (token) => setAnswer((current) => current + token),
      onCompleted: (completed) => {
        setAnswer(completed.answer)
        setCitations(completed.citations)
        setMetrics(completed.performanceMetrics)
        workspaceApi.listRagConversationMessages(targetConversationUid as string)
          .then((items) => { setMessages(items); setSubmittedQuestion('') })
          .catch(() => setHistoryFailed(true))
        workspaceApi.listRagConversations().then(setConversations).catch(() => setHistoryFailed(true))
      },
      onCanceled: setMetrics,
      onError: setError,
    })
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (busy) {
      cancel()
      return
    }
    startRequest()
  }

  function cancel() {
    cancelRef.current()
    cancelRef.current = () => undefined
    setPhase('canceled')
  }

  async function selectLlmEndpoint(endpointId: number | null) {
    if (llmSaving) return
    setLlmSaving(true)
    setLlmError(false)
    try {
      const updated = await llmSettingsApi.select(endpointId)
      setLlmSettings(updated)
      setLlmOpen(false)
    } catch {
      setLlmError(true)
    } finally {
      setLlmSaving(false)
    }
  }

  function toggleScope(uid: string) {
    setSelectedNodeIds((current) => current.includes(uid)
      ? current.filter((value) => value !== uid)
      : [...current, uid])
  }

  function scrollToCitation(id: number) {
    document.getElementById(`rag-citation-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  function historyDate(item: RagConversation): string {
    const date = new Date(item.updatedAt)
    return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
  }

  const duration = durationMetric(metrics)

  return <div className="chat-layout">
    <section className="panel chat-panel">
      <div className="chat-scroll">
        {messages.length === 0 && phase === 'idle' && <div className="chat-welcome"><span className="ai-orb large"><Icon name="sparkles" size={23}/></span><h2>{t('chat.askTitle')}</h2><p>{t('chat.askDescription')}</p><div className="suggestions"><button onClick={() => setQuestion(t('chat.suggestionPolicy'))}>{t('chat.suggestionPolicy')}</button><button onClick={() => setQuestion(t('chat.suggestionStrategy'))}>{t('chat.suggestionStrategy')}</button></div></div>}
        {messages.map((message) => message.role === 'user'
          ? <div className="message user-message" key={message.uid}>{message.content}</div>
          : <div className={`message assistant-message ${message.ragJob?.status === 'failed' ? 'has-error' : ''}`} key={message.uid}>
              <div className="answer-status"><span className="complete-dot"><Icon name="check" size={11}/></span>{message.ragJob?.status === 'completed' ? t('chat.status.done') : message.ragJob?.status ?? t('chat.status.error')}</div>
              {message.ragJob?.answer && <p className="answer-text">{message.ragJob.answer}</p>}
              {message.ragJob?.errorMessage && <div className="chat-error"><p>{message.ragJob.errorMessage}</p></div>}
              {message.ragJob && message.ragJob.citations.length > 0 && <div className="citation-row">{message.ragJob.citations.map((citation) => <button type="button" key={citation.id} onClick={() => setCitations(message.ragJob?.citations ?? [])}>[{citation.id}] {citation.nodeName}</button>)}</div>}
            </div>
        )}
        {submittedQuestion && <>
          <div className="message user-message">{submittedQuestion}</div>
          <div className={`message assistant-message ${phase === 'error' ? 'has-error' : ''}`}>
            <div className="answer-status"><span className={busy ? 'pulse-dot' : 'complete-dot'}>{phase === 'done' && <Icon name="check" size={11}/>}</span>{statusText}</div>
            {answer && <p className="answer-text">{answer}</p>}
            {error && <div className="chat-error"><p>{errorMessage(error)}</p><button type="button" onClick={startRequest}><Icon name="refresh" size={13}/>{t('chat.retry')}</button></div>}
            {phase === 'canceled' && <button type="button" className="chat-retry" onClick={startRequest}><Icon name="refresh" size={13}/>{t('chat.retry')}</button>}
            {phase === 'done' && citations.length > 0 && <div className="citation-row">{citations.map((citation) => <button type="button" key={citation.id} onClick={() => scrollToCitation(citation.id)}>[{citation.id}] {citation.nodeName}</button>)}</div>}
            {phase === 'done' && <div className="chat-meta">{started?.llmModel && <span>{t('chat.model', { model: started.llmModel })}</span>}{duration !== null && <span>{t('chat.duration', { duration })}</span>}</div>}
          </div>
        </>}
      </div>
      <form className="chat-compose" onSubmit={submit}>
        <div className="chat-compose-chips">
          <div className="chat-compose-tool-group" ref={scopeAnchorRef}>
            <button type="button" className={`compose-chip ${selectedNodeIds.length ? 'active' : ''}`} onClick={() => setScopeOpen((value) => !value)}>
              <Icon name="layers" size={13}/>{selectedNodeIds.length ? t('chat.selectedScope', { count: selectedNodeIds.length }) : t('chat.allDocuments')}
            </button>
            {scopeOpen && <div className="scope-popover">
              <div className="scope-popover-head">
                <strong>{t('chat.scope')}</strong>
                <button type="button" onClick={() => setScopeOpen(false)} aria-label={t('chat.scopeClose')}><Icon name="x" size={13}/></button>
              </div>
              <p>{selectedNodeIds.length ? t('chat.selectedScope', { count: selectedNodeIds.length }) : t('chat.scopeAllDescription')}</p>
              {scopeLoading && <small>{t('chat.scopeLoading')}</small>}
              {scopeFailed && <small className="scope-error">{t('chat.scopeFailed')}</small>}
              {!scopeLoading && !scopeFailed && <div className="chat-scope-list">{scopes.map((scope) => <label key={scope.uid} style={{ paddingLeft: `${scope.depth * 8}px` }}><input type="checkbox" checked={selectedNodeIds.includes(scope.uid)} onChange={() => toggleScope(scope.uid)}/><span><strong>{scope.name}</strong><small>{scope.nodeType === 'directory' ? t('chat.folderFiles', { count: scope.fileCount }) : scope.ext.toUpperCase()}</small></span></label>)}</div>}
              {selectedNodeIds.length > 0 && <button type="button" className="scope-clear" onClick={() => setSelectedNodeIds([])}>{t('chat.clearScope')}</button>}
            </div>}
          </div>
          {llmSettings && <div className="chat-compose-tool-group" ref={llmAnchorRef}>
            <button type="button" className={`compose-chip ${llmSettings.active.source === 'external' ? 'active' : ''}`} onClick={() => setLlmOpen((value) => !value)}>
              <Icon name="settings" size={13}/>{llmSettings.active.model || llmSettings.active.label}
            </button>
            {llmOpen && <div className="scope-popover">
              <div className="scope-popover-head">
                <strong>{t('chat.modelLabel')}</strong>
                <button type="button" onClick={() => setLlmOpen(false)} aria-label={t('chat.modelClose')}><Icon name="x" size={13}/></button>
              </div>
              {!llmSettings.can_edit && <p>{t('chat.modelReadOnly')}</p>}
              <div className="chat-scope-list">
                <label>
                  <input type="radio" name="llm-endpoint" checked={llmSettings.selected_endpoint_id === null} disabled={!llmSettings.can_edit || llmSaving} onChange={() => selectLlmEndpoint(null)}/>
                  <span><strong>{t('chat.modelServerDefault')}</strong><small>{llmSettings.server_default_model}</small></span>
                </label>
                {llmSettings.endpoints.map((endpoint) => <label key={endpoint.id}>
                  <input type="radio" name="llm-endpoint" checked={llmSettings.selected_endpoint_id === endpoint.id} disabled={!llmSettings.can_edit || llmSaving} onChange={() => selectLlmEndpoint(endpoint.id)}/>
                  <span><strong>{endpoint.name}</strong><small>{endpoint.default_model}</small></span>
                </label>)}
              </div>
              {llmError && <small className="scope-error">{t('chat.modelSelectFailed')}</small>}
            </div>}
          </div>}
        </div>
        <textarea
          ref={textareaRef}
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder={t('chat.placeholder')}
          rows={1}
        />
        <button className={`send-button ${busy ? 'is-stop' : ''}`} aria-label={busy ? t('chat.cancel') : t('chat.send')} disabled={!busy && !question.trim()}><Icon name={busy ? 'square' : 'arrow'} size={busy ? 15 : 17}/></button>
      </form>
    </section>
    <aside className="panel evidence-panel">
      <div className="evidence-head"><div><span className="eyebrow">{t('chat.evidenceLabel')}</span><h2>{t('chat.evidenceDocuments')}</h2></div><span>{citations.length}</span></div>
      <details className="chat-history">
        <summary><Icon name="clock" size={14}/>{t('chat.sessions')}</summary>
        <button type="button" className="scope-clear" onClick={() => onConversationChange(null)}>{t('chat.newSession')}</button>
        {historyLoading && <small>{t('chat.historyLoading')}</small>}
        {historyFailed && <small className="scope-error">{t('chat.historyFailed')}</small>}
        {!historyLoading && !historyFailed && conversations.length === 0 && <small>{t('chat.historyEmpty')}</small>}
        {conversations.length > 0 && <div className="chat-history-list">{conversations.map((item) => <button type="button" className={item.uid === conversationUid ? 'active' : ''} key={item.uid} onClick={() => onConversationChange(item.uid)}><strong>{item.title || t('chat.untitledSession')}</strong><span>{historyDate(item)}</span></button>)}</div>}
      </details>
      {citations.length === 0 ? <div className="evidence-empty"><Icon name="document" size={26}/><strong>{phase === 'done' ? t('chat.noEvidenceFound') : t('chat.noEvidence')}</strong><p>{phase === 'done' ? t('chat.noEvidenceFoundDescription') : t('chat.noEvidenceDescription')}</p></div> : <div className="evidence-list">{citations.map((citation) => <div id={`rag-citation-${citation.id}`} key={`${citation.nodeId}:${citation.id}`}><ResultCard result={citationResult(citation)} rank={citation.id} onOpen={() => onOpenDocument(citation.nodeId)}/></div>)}</div>}
    </aside>
  </div>
}
