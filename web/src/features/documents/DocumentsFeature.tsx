import { type FormEvent, useCallback, useEffect, useState } from 'react'
import { ApiClientError } from '../../api/http'
import type {
  DocumentFolder,
  DocumentListResult,
  DocumentMutationResult,
  DocumentReadiness,
  DocumentSummary,
  DocumentView,
} from '../../api/models'
import { workspaceApi } from '../../api/workspace'
import { AsyncState, type AsyncStateKind } from '../../components/AsyncState'
import { Icon } from '../../components/Icon'
import { useI18n } from '../../i18n'

type LoadState = AsyncStateKind | 'ready'
type MutationNotice = { kind: 'success' | 'error' | 'warning'; message: string; errors: string[] }
type LayoutMode = 'list' | 'grid'
type DocSearchMode = 'normal' | 'ai'
type AiRow = { document: DocumentSummary; score: number; snippet: string }

const VIEW_STORAGE_KEY = 'dotori:documents:view'

function loadSavedLayoutMode(): LayoutMode {
  try {
    return window.localStorage.getItem(VIEW_STORAGE_KEY) === 'grid' ? 'grid' : 'list'
  } catch {
    return 'list'
  }
}

function saveLayoutMode(mode: LayoutMode) {
  try {
    window.localStorage.setItem(VIEW_STORAGE_KEY, mode)
  } catch {
    // localStorage can be disabled in private or restricted browser modes.
  }
}

function readLocationState() {
  const params = new URLSearchParams(window.location.search)
  const rawView = params.get('document_view')
  const view: DocumentView = rawView === 'recent' || rawView === 'starred' || rawView === 'trash'
    ? rawView
    : 'files'
  const rawPage = Number(params.get('page') || 1)
  return {
    view,
    folderUid: view === 'files' ? params.get('folder') : null,
    page: Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 1,
  }
}

function errorState(error: unknown): AsyncStateKind {
  return error instanceof ApiClientError && error.unauthorized ? 'unauthorized' : 'error'
}

const IMAGE_TYPES = new Set(['JPG', 'JPEG', 'PNG', 'GIF', 'SVG', 'WEBP'])
const DOC_TYPES = new Set(['DOC', 'DOCX', 'TXT', 'CSV', 'HWP', 'HWPX', 'MD'])

function documentTypeCategory(document: DocumentSummary): 'folder' | 'pdf' | 'image' | 'doc' | 'other' {
  if (document.nodeType === 'directory') return 'folder'
  if (document.type === 'PDF') return 'pdf'
  if (IMAGE_TYPES.has(document.type)) return 'image'
  if (DOC_TYPES.has(document.type)) return 'doc'
  return 'other'
}

function compareBySortOrder(a: DocumentSummary, b: DocumentSummary, order: string): number {
  switch (order) {
    case 'date_asc':
      return a.updatedAt.localeCompare(b.updatedAt)
    case 'name_asc':
      return a.name.localeCompare(b.name)
    case 'size_desc':
      return (b.size ?? -1) - (a.size ?? -1)
    default:
      return b.updatedAt.localeCompare(a.updatedAt)
  }
}

function sortDocuments(documents: DocumentSummary[], order: string): DocumentSummary[] {
  const sorted = [...documents]
  sorted.sort((a, b) => {
    if (a.nodeType === 'directory' && b.nodeType !== 'directory') return -1
    if (a.nodeType !== 'directory' && b.nodeType === 'directory') return 1
    return compareBySortOrder(a, b, order)
  })
  return sorted
}

function mutationErrors(error: unknown): string[] {
  if (error instanceof ApiClientError) {
    if (Array.isArray(error.details)) return error.details.map(String)
    return [error.message]
  }
  return [error instanceof Error ? error.message : String(error)]
}

export function DocumentsFeature({ onSearch, onAsk, pendingOpenUid, onPendingOpenHandled }: { onSearch: () => void; onAsk: () => void; pendingOpenUid?: string | null; onPendingOpenHandled?: () => void }) {
  const { t } = useI18n()
  const initialLocation = readLocationState()
  const [view, setView] = useState<DocumentView>(initialLocation.view)
  const [folderUid, setFolderUid] = useState<string | null>(initialLocation.folderUid)
  const [page, setPage] = useState(initialLocation.page)
  const [filter, setFilter] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [layoutMode, setLayoutMode] = useState<LayoutMode>(loadSavedLayoutMode)
  const [sortOrder, setSortOrder] = useState('date_desc')
  const [result, setResult] = useState<DocumentListResult | null>(null)
  const [readiness, setReadiness] = useState<DocumentReadiness | null>(null)
  const [folderChain, setFolderChain] = useState<DocumentSummary[]>([])
  const [state, setState] = useState<LoadState>('loading')
  const [selected, setSelected] = useState<string[]>([])
  const [detailDocument, setDetailDocument] = useState<DocumentSummary | null>(null)
  const [parsedText, setParsedText] = useState<string | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [shareOpen, setShareOpen] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [createFolderOpen, setCreateFolderOpen] = useState(false)
  const [moveOpen, setMoveOpen] = useState(false)
  const [trashConfirm, setTrashConfirm] = useState<{ uids: string[] } | null>(null)
  const [mutating, setMutating] = useState(false)
  const [notice, setNotice] = useState<MutationNotice | null>(null)
  const [searchMode, setSearchMode] = useState<DocSearchMode>('normal')
  const [aiState, setAiState] = useState<LoadState>('empty')
  const [aiRows, setAiRows] = useState<AiRow[]>([])
  const [aiSearched, setAiSearched] = useState(false)

  const loadDocuments = useCallback(async () => {
    setState('loading')
    try {
      const nextResult = await workspaceApi.listDocuments({
        view,
        query: filter,
        parentUid: folderUid,
        page,
        limit: 50,
      })
      setResult(nextResult)
      setState(nextResult.documents.length ? 'ready' : 'empty')
    } catch (error) {
      setState(errorState(error))
    }
  }, [filter, folderUid, page, view])

  const runAiSearch = useCallback(async () => {
    const query = filter.trim()
    if (!query) {
      setAiRows([])
      setAiState('empty')
      setAiSearched(false)
      return
    }
    setAiState('loading')
    setAiSearched(true)
    try {
      const response = await workspaceApi.searchDocuments({
        mode: 'advanced',
        query,
        nodeIds: [],
      })
      const hydrated = await Promise.all(response.results.map(async (result): Promise<AiRow | null> => {
        const document = await workspaceApi.getDocument(result.uid).catch(() => null)
        return document ? { document, score: result.score, snippet: result.text } : null
      }))
      const filtered = hydrated.filter((row): row is AiRow => row !== null)
      setAiRows(filtered)
      setAiState(filtered.length ? 'ready' : 'empty')
    } catch (error) {
      setAiRows([])
      setAiState(errorState(error))
    }
  }, [filter])

  useEffect(() => {
    if (searchMode === 'ai') return
    const timer = window.setTimeout(() => void loadDocuments(), 150)
    return () => window.clearTimeout(timer)
  }, [loadDocuments, searchMode])

  useEffect(() => {
    void workspaceApi.getReadiness().then(setReadiness).catch(() => setReadiness(null))
  }, [])

  useEffect(() => {
    let active = true
    if (!folderUid) {
      setFolderChain([])
      return
    }
    async function loadChain() {
      const chain: DocumentSummary[] = []
      let uid: string | null = folderUid
      while (uid && chain.length < 25) {
        const folder: DocumentSummary | null = await workspaceApi.getDocument(uid).catch(() => null)
        if (!folder) break
        chain.unshift(folder)
        uid = folder.parentUid
      }
      if (active) setFolderChain(chain)
    }
    void loadChain()
    return () => { active = false }
  }, [folderUid])

  useEffect(() => {
    if (!pendingOpenUid) return
    let active = true
    workspaceApi.getDocument(pendingOpenUid)
      .then((document) => { if (active) void openDocument(document) })
      .catch(() => undefined)
      .finally(() => onPendingOpenHandled?.())
    return () => { active = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingOpenUid])

  useEffect(() => {
    const url = new URL(window.location.href)
    if (view === 'files') url.searchParams.delete('document_view')
    else url.searchParams.set('document_view', view)
    if (view === 'files' && folderUid) url.searchParams.set('folder', folderUid)
    else url.searchParams.delete('folder')
    if (page > 1) url.searchParams.set('page', String(page))
    else url.searchParams.delete('page')
    window.history.replaceState({}, '', url)
  }, [folderUid, page, view])

  useEffect(() => {
    const restoreLocation = () => {
      const restored = readLocationState()
      setView(restored.view)
      setFolderUid(restored.folderUid)
      setPage(restored.page)
    }
    window.addEventListener('popstate', restoreLocation)
    return () => window.removeEventListener('popstate', restoreLocation)
  }, [])

  const rows = sortDocuments(
    (result?.documents ?? []).filter((document) =>
      (!typeFilter || documentTypeCategory(document) === typeFilter)
      && (!statusFilter || document.status === statusFilter)),
    sortOrder,
  )
  const currentReadyCount = (result?.documents ?? []).filter((document) => document.status === 'ready').length
  const activeRows: AiRow[] = searchMode === 'ai' ? aiRows : rows.map((document) => ({ document, score: 0, snippet: '' }))
  const activeState: LoadState = searchMode === 'ai' ? aiState : state

  function exitAiSearch() {
    setSearchMode('normal')
    setAiRows([])
    setAiState('empty')
    setAiSearched(false)
  }

  function selectView(nextView: DocumentView) {
    exitAiSearch()
    setView(nextView)
    setFolderUid(null)
    setPage(1)
    setSelected([])
  }

  function openFolder(folder: DocumentSummary) {
    exitAiSearch()
    setView('files')
    setFolderUid(folder.uid)
    setPage(1)
    setFilter('')
    setSelected([])
  }

  function goToFolder(uid: string | null) {
    exitAiSearch()
    setFolderUid(uid)
    setPage(1)
  }

  async function openDocument(document: DocumentSummary) {
    if (document.nodeType === 'directory') {
      openFolder(document)
      return
    }
    setDetailDocument(document)
    setParsedText(null)
    setDetailLoading(true)
    setShareOpen(false)
    const [detailResult, textResult] = await Promise.allSettled([
      workspaceApi.getDocument(document.uid),
      workspaceApi.getParsedText(document.uid),
    ])
    if (detailResult.status === 'fulfilled') setDetailDocument(detailResult.value)
    if (textResult.status === 'fulfilled') setParsedText(textResult.value)
    else setParsedText('')
    setDetailLoading(false)
  }

  function closeDocument() {
    setDetailDocument(null)
    setParsedText(null)
    setShareOpen(false)
  }

  function toggleSelected(uid: string) {
    setSelected((current) => current.includes(uid) ? current.filter((item) => item !== uid) : [...current, uid])
  }

  async function refreshAfterMutation() {
    await loadDocuments()
    void workspaceApi.getReadiness().then(setReadiness).catch(() => setReadiness(null))
  }

  async function applyMutation(action: () => Promise<DocumentMutationResult>) {
    setMutating(true)
    setNotice(null)
    try {
      const outcome = await action()
      setNotice({
        kind: outcome.errors.length ? 'error' : 'success',
        message: t(outcome.errors.length ? 'document.partialFailure' : 'document.mutationSuccess', { count: outcome.count }),
        errors: outcome.errors,
      })
      setSelected([])
      closeDocument()
      await refreshAfterMutation()
      return outcome.errors.length === 0
    } catch (error) {
      setNotice({ kind: 'error', message: t('mutation.error'), errors: mutationErrors(error) })
      return false
    } finally {
      setMutating(false)
    }
  }

  function trashSelected() {
    setTrashConfirm({ uids: selected })
  }

  async function confirmTrash() {
    if (!trashConfirm) return
    const uids = trashConfirm.uids
    setTrashConfirm(null)
    await applyMutation(() => workspaceApi.trashDocuments(uids))
  }

  async function restoreSelected() {
    await applyMutation(() => workspaceApi.restoreDocuments(selected))
  }

  async function permanentlyDeleteSelected() {
    if (!window.confirm(t('document.confirmPermanentDelete'))) return
    await applyMutation(() => workspaceApi.permanentlyDeleteDocuments(selected))
  }

  async function toggleStar(document: DocumentSummary) {
    setMutating(true)
    try {
      await workspaceApi.toggleStar(document.uid)
      await refreshAfterMutation()
    } catch (error) {
      setNotice({ kind: 'error', message: t('mutation.error'), errors: mutationErrors(error) })
    } finally {
      setMutating(false)
    }
  }

  async function renameDocument(document: DocumentSummary) {
    const name = window.prompt(t('document.renamePrompt'), document.name)?.trim()
    if (!name || name === document.name) return
    setMutating(true)
    try {
      const renamed = await workspaceApi.renameDocument(document.uid, name)
      setDetailDocument(renamed)
      setNotice({ kind: 'success', message: t('document.mutationSuccess', { count: 1 }), errors: [] })
      await refreshAfterMutation()
    } catch (error) {
      setNotice({ kind: 'error', message: t('mutation.error'), errors: mutationErrors(error) })
    } finally {
      setMutating(false)
    }
  }

  async function retryDocument(document: DocumentSummary) {
    setMutating(true)
    try {
      await workspaceApi.retryAiProcessing(document.uid)
      const refreshed = await workspaceApi.getDocument(document.uid)
      setDetailDocument(refreshed)
      setNotice({ kind: 'success', message: t('document.mutationSuccess', { count: 1 }), errors: [] })
      await refreshAfterMutation()
    } catch (error) {
      setNotice({ kind: 'error', message: t('mutation.error'), errors: mutationErrors(error) })
    } finally {
      setMutating(false)
    }
  }

  async function toggleAi(document: DocumentSummary) {
    setMutating(true)
    try {
      const refreshed = await workspaceApi.setAiProcessing(document.uid, !document.aiProcessingEnabled)
      setDetailDocument(refreshed)
      setNotice({ kind: 'success', message: t('document.mutationSuccess', { count: 1 }), errors: [] })
      await refreshAfterMutation()
    } catch (error) {
      setNotice({ kind: 'error', message: t('mutation.error'), errors: mutationErrors(error) })
    } finally {
      setMutating(false)
    }
  }

  async function quickToggleAi(document: DocumentSummary) {
    setMutating(true)
    try {
      await workspaceApi.setAiProcessing(document.uid, !document.aiProcessingEnabled)
      await refreshAfterMutation()
    } catch (error) {
      setNotice({ kind: 'error', message: t('mutation.error'), errors: mutationErrors(error) })
    } finally {
      setMutating(false)
    }
  }

  function quickTrash(document: DocumentSummary) {
    setTrashConfirm({ uids: [document.uid] })
  }

  async function quickRestore(document: DocumentSummary) {
    await applyMutation(() => workspaceApi.restoreDocuments([document.uid]))
  }

  function changeLayoutMode(mode: LayoutMode) {
    setLayoutMode(mode)
    saveLayoutMode(mode)
  }

  function changeSearchMode(mode: DocSearchMode) {
    setSearchMode(mode)
    setSelected([])
    setAiRows([])
    setAiState('empty')
    setAiSearched(false)
  }

  function renderQuickActions(document: DocumentSummary, iconSize: number) {
    if (document.trashed) {
      return <button className="icon-button" disabled={mutating} title={t('document.restoreSelected')} onClick={(event) => { event.stopPropagation(); void quickRestore(document) }}><Icon name="refresh" size={iconSize} /></button>
    }
    return <>
      {document.nodeType !== 'directory' && <a className="icon-button" href={workspaceApi.getDownloadUrl(document.uid)} title={t('detail.download')} onClick={(event) => event.stopPropagation()}><Icon name="download" size={iconSize} /></a>}
      {document.nodeType !== 'directory' && <button className="icon-button" disabled={mutating} title={t(document.aiProcessingEnabled ? 'document.aiDisable' : 'document.aiEnable')} onClick={(event) => { event.stopPropagation(); void quickToggleAi(document) }}><Icon name="sparkles" size={iconSize} /></button>}
      <button className="icon-button" disabled={mutating} title={t('detail.trash')} onClick={(event) => { event.stopPropagation(); void quickTrash(document) }}><Icon name="trash" size={iconSize} /></button>
    </>
  }

  function statusLabel(document: DocumentSummary) {
    if (document.nodeType === 'directory') return t('document.folder')
    if (document.status === 'stale') return t('document.embeddingOutdated')
    if (document.status === 'failed') return t('document.failed')
    if (document.status === 'disabled') return t('document.disabled')
    return document.status === 'processing' ? t('document.processing') : t('document.ready')
  }

  const views: Array<[DocumentView, string]> = [
    ['files', t('document.view.all')],
    ['recent', t('document.view.recent')],
    ['starred', t('document.view.starred')],
    ['trash', t('document.view.trash')],
  ]

  return <>
    <section className="panel documents-panel">
      <div className="document-header">
        <div className="header-title-row">
          <h1 className="header-location"><button onClick={() => goToFolder(null)}>{t('home.myDocuments')}</button>{view === 'files' && folderChain.map((folder) => <span key={folder.uid} className="breadcrumb-segment"><Icon name="chevron" size={15} /><button onClick={() => goToFolder(folder.uid)}>{folder.name}</button></span>)}</h1>
          <div className="header-stats">
            <div className="stat-group"><span className="stat-label">{t('document.statOverall')}</span><span className="stat-value">{t('document.statUploaded', { count: readiness?.totalFiles ?? 0 })} · {t('document.statSearchable', { count: readiness?.searchableFiles ?? 0 })}</span></div>
            <div className="stat-group"><span className="stat-label">{t('document.statCurrent')}</span><span className="stat-value">{t('document.statUploaded', { count: result?.total ?? 0 })} · {t('document.statSearchable', { count: currentReadyCount })}</span></div>
          </div>
        </div>
        <div className="document-header-actions"><button className="secondary-button" onClick={() => setCreateFolderOpen(true)}><Icon name="plus" size={17} />{t('document.createFolder')}</button><button className="primary-button" onClick={() => setUploadOpen(true)}><Icon name="upload" size={17} />{t('document.add')}</button></div>
      </div>
      <div className="document-view-tabs" role="tablist">
        {views.map(([key, label]) => <button key={key} className={view === key ? 'active' : ''} onClick={() => selectView(key)}>{label}</button>)}
      </div>
      <div className="panel-toolbar">
        <div className="toolbar-left">
          <div className="search-mode-toggle" role="tablist" aria-label={t('document.searchModeLabel')}>
            <button type="button" role="tab" aria-selected={searchMode === 'normal'} className={searchMode === 'normal' ? 'active' : ''} onClick={() => changeSearchMode('normal')}>{t('document.searchModeNormal')}</button>
            <button type="button" role="tab" aria-selected={searchMode === 'ai'} className={searchMode === 'ai' ? 'active' : ''} disabled={view !== 'files'} title={view !== 'files' ? t('document.aiSearchUnavailable') : undefined} onClick={() => changeSearchMode('ai')}>{t('document.searchModeAi')}</button>
          </div>
          <label className="search-control">
            {searchMode === 'ai' ? <button type="button" className="search-run" title={t('document.aiSearchRun')} aria-label={t('document.aiSearchRun')} onClick={() => void runAiSearch()}><Icon name="search" size={17} /></button> : <Icon name="search" size={17} />}
            <input value={filter} onChange={(event) => { setFilter(event.target.value); setPage(1) }} onKeyDown={(event) => { if (event.key === 'Enter' && searchMode === 'ai') { event.preventDefault(); void runAiSearch() } }} placeholder={searchMode === 'ai' ? t('document.aiSearchPlaceholder') : t('document.searchPlaceholder')} />
          </label>
        </div>
        <div className="toolbar-right">
          {searchMode === 'normal' && <><select className="filter-select" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="">{t('document.allStatuses')}</option><option value="ready">{t('document.ready')}</option><option value="processing">{t('document.processing')}</option><option value="failed">{t('document.failed')}</option><option value="disabled">{t('document.disabled')}</option></select><select className="filter-select" value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}><option value="">{t('document.filter.allTypes')}</option><option value="pdf">{t('document.filter.pdf')}</option><option value="image">{t('document.filter.image')}</option><option value="doc">{t('document.filter.doc')}</option><option value="folder">{t('document.folder')}</option><option value="other">{t('document.filter.other')}</option></select><button type="button" className={`icon-button ${typeFilter === 'folder' ? 'active' : ''}`} aria-pressed={typeFilter === 'folder'} title={t('document.filter.foldersOnly')} onClick={() => setTypeFilter((current) => current === 'folder' ? '' : 'folder')}><Icon name="folder" size={15} /></button><select className="filter-select" value={sortOrder} onChange={(event) => setSortOrder(event.target.value)}><option value="date_desc">{t('document.sort.dateDesc')}</option><option value="date_asc">{t('document.sort.dateAsc')}</option><option value="name_asc">{t('document.sort.nameAsc')}</option><option value="size_desc">{t('document.sort.sizeDesc')}</option></select></>}
          <div className="layout-toggle"><button className={layoutMode === 'grid' ? 'active' : ''} title={t('document.layout.grid')} onClick={() => changeLayoutMode('grid')}><Icon name="grid" size={15} /></button><button className={layoutMode === 'list' ? 'active' : ''} title={t('document.layout.list')} onClick={() => changeLayoutMode('list')}><Icon name="list" size={15} /></button></div>
        </div>
      </div>
      {notice && <div className={`mutation-notice ${notice.kind}`} role="status"><div><strong>{notice.message}</strong>{notice.errors.length > 0 && <ul>{notice.errors.map((error, index) => <li key={`${error}-${index}`}>{error}</li>)}</ul>}</div><button onClick={() => setNotice(null)} aria-label={t('action.closeMenu')}><Icon name="x" size={14} /></button></div>}
      {mutating && <div className="mutation-working" role="status">{t('mutation.working')}</div>}
      {selected.length > 0 && <div className="selection-bar"><strong>{t('document.selectedCount', { count: selected.length })}</strong><div className="selection-actions">{view === 'trash' ? <><button disabled={mutating} onClick={() => void restoreSelected()}>{t('document.restoreSelected')}</button><button className="danger" disabled={mutating} onClick={() => void permanentlyDeleteSelected()}>{t('document.deleteSelected')}</button></> : <><button disabled={mutating} onClick={() => setMoveOpen(true)}>{t('document.moveSelected')}</button><button className="danger" disabled={mutating} onClick={() => void trashSelected()}>{t('document.trashSelected')}</button></>}<button onClick={() => setSelected([])}>{t('document.clearSelection')}</button></div></div>}
      {searchMode === 'ai' && !aiSearched ? <div className="async-state empty" role="status"><span><Icon name="sparkles" /></span><strong>{t('document.aiSearchIdleTitle')}</strong><p>{t('document.aiSearchIdleDescription')}</p></div> : activeState === 'ready' ? (layoutMode === 'list' ? <div className="document-table-wrap"><table className="document-table"><thead><tr><th className="check-cell"></th><th className="check-cell"></th><th>{t('document.column.document')}</th><th>{t('document.column.type')}</th><th>{t('document.column.size')}</th><th>{t('document.column.updated')}</th><th>{t('document.column.status')}</th><th></th></tr></thead><tbody>{activeRows.map(({ document, score, snippet }) => <tr key={document.uid} className="document-row" onClick={() => void openDocument(document)}><td className="check-cell"><button className={`checkbox ${selected.includes(document.uid) ? 'checked' : ''}`} onClick={(event) => { event.stopPropagation(); toggleSelected(document.uid) }}>{selected.includes(document.uid) && <Icon name="check" size={13} />}</button></td><td className="check-cell"><button className={`star-toggle ${document.starred ? 'active' : ''}`} disabled={mutating || view === 'trash'} title={t(document.starred ? 'document.unstar' : 'document.star')} onClick={(event) => { event.stopPropagation(); void toggleStar(document) }}>★</button></td><td><div className="document-name"><span className={`file-type ${document.type.toLowerCase()}`}>{document.nodeType === 'directory' ? <Icon name="folder" size={13} /> : document.type.slice(0, 1)}</span><div><strong>{document.name}</strong>{document.nodeType === 'directory' && <small>{document.path}</small>}{searchMode === 'ai' && <p className="ai-snippet">{snippet}</p>}</div></div></td><td>{document.nodeType === 'directory' ? t('document.folder') : document.type}</td><td>{document.sizeLabel}</td><td>{document.updatedLabel}</td><td>{searchMode === 'ai' ? <span className="ai-score">{score.toFixed(2)}</span> : <span className={`doc-status ${document.status}`}><i />{statusLabel(document)}</span>}</td><td className="row-actions">{renderQuickActions(document, 15)}</td></tr>)}</tbody></table></div> : <div className="document-grid">{activeRows.map(({ document, score, snippet }) => <div key={document.uid} className={`document-card ${selected.includes(document.uid) ? 'selected' : ''}`} onClick={() => void openDocument(document)}><button className={`checkbox card-select ${selected.includes(document.uid) ? 'checked' : ''}`} onClick={(event) => { event.stopPropagation(); toggleSelected(document.uid) }}>{selected.includes(document.uid) && <Icon name="check" size={11} />}</button><button className={`star-toggle card-star ${document.starred ? 'active' : ''}`} disabled={mutating || view === 'trash'} title={t(document.starred ? 'document.unstar' : 'document.star')} onClick={(event) => { event.stopPropagation(); void toggleStar(document) }}>★</button><div className="card-top"><span className={`file-type ${document.type.toLowerCase()}`}>{document.nodeType === 'directory' ? <Icon name="folder" size={13} /> : document.type.slice(0, 1)}</span></div><div className="card-name" title={document.name}>{document.name}</div>{searchMode === 'ai' ? <p className="ai-snippet">{snippet}</p> : <div className="card-meta"><span>{document.nodeType === 'directory' ? t('document.folder') : document.type}</span><span>·</span><span>{document.sizeLabel}</span></div>}{searchMode === 'ai' ? <span className="ai-score">{score.toFixed(2)}</span> : <span className={`doc-status ${document.status}`}><i />{statusLabel(document)}</span>}<div className="card-actions">{renderQuickActions(document, 14)}</div></div>)}</div>) : <AsyncState kind={activeState} onRetry={activeState === 'error' ? () => void (searchMode === 'ai' ? runAiSearch() : loadDocuments()) : undefined} />}
      <div className="table-footer"><span>{searchMode === 'ai' ? t('document.aiResultCount', { count: aiRows.length }) : t('document.displayed', { count: rows.length, total: result?.total ?? 0 })}</span>{searchMode === 'normal' && <div><button disabled={page <= 1} onClick={() => setPage((current) => Math.max(current - 1, 1))}>{t('pagination.previous')}</button><button className="active">{page}</button><button disabled={!result?.hasNext} onClick={() => setPage((current) => current + 1)}>{t('pagination.next')}</button></div>}</div>
    </section>
    {uploadOpen && <UploadDialog parentUid={folderUid} onClose={() => setUploadOpen(false)} onComplete={async (errors, duplicates) => {
      setUploadOpen(false)
      if (errors.length) {
        setNotice({ kind: 'error', message: t('upload.partialFailure'), errors: [...errors, ...duplicates] })
      } else if (duplicates.length) {
        setNotice({ kind: 'warning', message: t('upload.duplicatesNoted'), errors: duplicates })
      } else {
        setNotice({ kind: 'success', message: t('upload.success'), errors: [] })
      }
      await refreshAfterMutation()
    }} />}
    {createFolderOpen && <CreateFolderDialog parentUid={folderUid} onClose={() => setCreateFolderOpen(false)} onComplete={async () => { setCreateFolderOpen(false); setNotice({ kind: 'success', message: t('document.mutationSuccess', { count: 1 }), errors: [] }); await refreshAfterMutation() }} />}
    {moveOpen && <MoveDialog count={selected.length} onClose={() => setMoveOpen(false)} onMove={async (parentUid) => { const completed = await applyMutation(() => workspaceApi.moveDocuments(selected, parentUid)); if (completed) setMoveOpen(false) }} />}
    {detailDocument && <DocumentDetailDrawer document={detailDocument} parsedText={parsedText} loading={detailLoading} busy={mutating} onClose={closeDocument} onShare={() => setShareOpen(true)} onSearch={() => { closeDocument(); onSearch() }} onAsk={() => { closeDocument(); onAsk() }} onRename={() => void renameDocument(detailDocument)} onRetry={() => void retryDocument(detailDocument)} onToggleAi={() => void toggleAi(detailDocument)} onTrash={() => setTrashConfirm({ uids: [detailDocument.uid] })} onRestore={() => void applyMutation(() => workspaceApi.restoreDocuments([detailDocument.uid]))} onPermanentDelete={() => { if (window.confirm(t('document.confirmPermanentDelete'))) void applyMutation(() => workspaceApi.permanentlyDeleteDocuments([detailDocument.uid])) }} />}
    {detailDocument && shareOpen && <ShareDialog document={detailDocument} onClose={() => setShareOpen(false)} />}
    {trashConfirm && <ConfirmDialog
      title={t('document.confirmTrashTitle')}
      description={t('document.confirmTrash')}
      confirmLabel={t('detail.trash')}
      danger
      busy={mutating}
      onConfirm={() => void confirmTrash()}
      onCancel={() => setTrashConfirm(null)}
    />}
  </>
}

function DocumentDetailDrawer({ document, parsedText, loading, busy, onClose, onShare, onSearch, onAsk, onRename, onRetry, onToggleAi, onTrash, onRestore, onPermanentDelete }: { document: DocumentSummary; parsedText: string | null; loading: boolean; busy: boolean; onClose: () => void; onShare: () => void; onSearch: () => void; onAsk: () => void; onRename: () => void; onRetry: () => void; onToggleAi: () => void; onTrash: () => void; onRestore: () => void; onPermanentDelete: () => void }) {
  const { t } = useI18n()
  const processing = document.status === 'processing'
  const failed = document.status === 'failed' || document.status === 'stale'
  const disabled = document.status === 'disabled'
  const status = failed
    ? t('document.failed')
    : disabled
      ? t('document.disabled')
      : processing
        ? t('document.processing')
        : t('document.ready')
  return <div className="drawer-layer"><button className="drawer-scrim" onClick={onClose} aria-label={t('detail.close')} /><aside className="document-drawer" aria-label={t('detail.title')}><header className="drawer-header"><div className="drawer-document"><span className={`file-type ${document.type.toLowerCase()}`}>{document.type.slice(0, 1)}</span><div><span className="eyebrow">{t('detail.title')}</span><h2>{document.name}</h2></div></div><button className="icon-button" onClick={onClose} aria-label={t('detail.close')}><Icon name="x" /></button></header><div className="drawer-actions"><button className="primary-button" onClick={onAsk}><Icon name="message" size={16} />{t('detail.ask')}</button><button className="secondary-button" onClick={onSearch}><Icon name="search" size={16} />{t('detail.search')}</button><button className="secondary-button" onClick={onShare}><Icon name="share" size={16} />{t('detail.share')}<span className="future-badge">{t('future.badge')}</span></button></div><div className="drawer-scroll"><section className="detail-section detail-danger"><h3>{t('detail.manage')}</h3><div><a className="detail-action-link" href={workspaceApi.getDownloadUrl(document.uid)}><Icon name="download" size={15} />{t('detail.download')}</a>{document.trashed ? <><button disabled={busy} onClick={onRestore}><Icon name="refresh" size={15} />{t('document.restoreSelected')}</button><button className="danger" disabled={busy} onClick={onPermanentDelete}><Icon name="trash" size={15} />{t('document.deleteSelected')}</button></> : <><button disabled={busy} onClick={onRename}>{t('document.rename')}</button><button disabled={busy} onClick={onToggleAi}>{t(document.aiProcessingEnabled ? 'document.aiDisable' : 'document.aiEnable')}</button>{failed && <button disabled={busy} onClick={onRetry}><Icon name="refresh" size={15} />{t('detail.reprocess')}</button>}<button className="danger" disabled={busy} onClick={onTrash}><Icon name="trash" size={15} />{t('detail.trash')}</button></>}</div></section><section className="detail-section"><h3>{t('detail.summary')}</h3><p className="detail-summary">{document.summary || t('detail.noSummary')}</p>{document.tags.length > 0 && <div className="tag-list">{document.tags.map((tag) => <span key={tag}>#{tag}</span>)}</div>}</section><section className="detail-section"><div className="detail-section-title"><h3>{t('detail.processing')}</h3><span className={`doc-status ${document.status}`}><i />{status}</span></div><div className="pipeline-list"><div className="complete"><span><Icon name="check" size={12} /></span><div><strong>{t('detail.uploaded')}</strong><small>{document.createdLabel}</small></div></div><div className={document.aiStatus?.parseStatus === 'completed' ? 'complete' : 'active'}><span>{document.aiStatus?.parseStatus === 'completed' ? <Icon name="check" size={12} /> : <Icon name="clock" size={12} />}</span><div><strong>{t('detail.parsed')}</strong><small>{document.aiStatus?.parseLabel || t('detail.parsedDescription')}</small></div></div><div className={document.status === 'ready' ? 'complete' : 'active'}><span>{document.status === 'ready' ? <Icon name="check" size={12} /> : <Icon name="clock" size={12} />}</span><div><strong>{t('detail.indexed')}</strong><small>{document.aiStatus?.embeddingLabel || (processing ? t('detail.indexingDescription') : t('detail.indexedDescription'))}</small></div></div></div></section><section className="detail-section"><h3>{t('detail.information')}</h3><dl className="detail-grid"><div><dt>{t('detail.fileType')}</dt><dd>{document.type}</dd></div><div><dt>{t('detail.fileSize')}</dt><dd>{document.sizeLabel}</dd></div><div><dt>{t('detail.uploadDate')}</dt><dd>{document.createdLabel}</dd></div><div><dt>{t('detail.updated')}</dt><dd>{document.updatedLabel}</dd></div><div><dt>{t('detail.chunks')}</dt><dd>{document.chunks}</dd></div><div><dt>{t('detail.owner')}</dt><dd>Grey</dd></div></dl></section><section className="detail-section"><h3>{t('detail.parsedText')}</h3>{loading ? <div className="parsed-text-loading">{t('state.loadingTitle')}</div> : parsedText ? <pre className="parsed-text">{parsedText}</pre> : <p className="detail-summary">{t('detail.parsedTextUnavailable')}</p>}</section></div></aside></div>
}

function ShareDialog({ document, onClose }: { document: DocumentSummary; onClose: () => void }) {
  const { t } = useI18n()
  return <div className="modal-backdrop share-backdrop" onMouseDown={onClose}><div className="share-dialog" onMouseDown={(event) => event.stopPropagation()}><div className="dialog-head"><div><h2>{t('share.title')} <span className="future-badge">{t('future.badge')}</span></h2><p>{document.name}</p></div><button onClick={onClose}><Icon name="x" /></button></div><div className="share-scope-note"><Icon name="database" size={16} /><span><strong>{t('share.serverOnly')}</strong><small>{t('share.planned')}</small></span></div><label className="share-invite"><span>{t('share.addPerson')}</span><div><input disabled placeholder={t('share.searchAccount')} /><button className="primary-button" disabled>{t('share.add')}</button></div></label><div className="share-people"><span className="share-label">{t('share.people')}</span><div className="share-person"><span className="share-avatar">GR</span><div><strong>Grey</strong><small>grey@local · {t('share.owner')}</small></div><span>{t('share.owner')}</span></div></div><div className="dialog-actions"><button className="primary-button" onClick={onClose}>{t('share.done')}</button></div></div></div>
}

function UploadDialog({ parentUid, onClose, onComplete }: { parentUid: string | null; onClose: () => void; onComplete: (errors: string[], duplicates: string[]) => Promise<void> | void }) {
  const { t } = useI18n()
  const [files, setFiles] = useState<File[]>([])
  const [aiProcessingEnabled, setAiProcessingEnabled] = useState(true)
  const [progress, setProgress] = useState(0)
  const [busy, setBusy] = useState(false)
  const [dragActive, setDragActive] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function addFiles(list: FileList | null) {
    if (!list || !list.length) return
    setFiles((current) => [...current, ...Array.from(list)])
  }

  function removeFile(index: number) {
    setFiles((current) => current.filter((_, position) => position !== index))
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!files.length) return
    setBusy(true)
    setError(null)
    setProgress(0)
    const uploadErrors: string[] = []
    const duplicateNotices: string[] = []
    for (let index = 0; index < files.length; index += 1) {
      const target = files[index]
      try {
        const result = await workspaceApi.uploadDocument(target, {
          parentUid,
          aiProcessingEnabled,
          onProgress: (percent) => setProgress(Math.round(((index + percent / 100) / files.length) * 100)),
        })
        if (result.document.name !== target.name) duplicateNotices.push(t('upload.renamedForDuplicate', { name: target.name, renamed: result.document.name }))
        else if (result.status === 'duplicate') duplicateNotices.push(t('upload.duplicateContentNotice', { name: target.name }))
      } catch (caught) {
        uploadErrors.push(`${target.name}: ${mutationErrors(caught).join(' ')}`)
      }
    }
    setBusy(false)
    if (uploadErrors.length && uploadErrors.length === files.length) {
      setError(uploadErrors.join(' '))
      return
    }
    await onComplete(uploadErrors, duplicateNotices)
  }

  return <div className="modal-backdrop" onMouseDown={busy ? undefined : onClose}><form className="upload-dialog" onSubmit={(event) => void submit(event)} onMouseDown={(event) => event.stopPropagation()}><div className="dialog-head"><div><h2>{t('document.add')}</h2><p>{t('upload.description')}</p></div><button type="button" disabled={busy} onClick={onClose}><Icon name="x" /></button></div><label
      className={`drop-zone ${dragActive ? 'active' : ''}`}
      onDragOver={(event) => { event.preventDefault(); if (!busy) setDragActive(true) }}
      onDragLeave={(event) => { event.preventDefault(); setDragActive(false) }}
      onDrop={(event) => {
        event.preventDefault()
        setDragActive(false)
        if (!busy) addFiles(event.dataTransfer.files)
      }}
    ><input type="file" multiple disabled={busy} onChange={(event) => { addFiles(event.target.files); event.target.value = '' }} /><span><Icon name="upload" size={23} /></span><strong>{files.length ? t('upload.chosenCount', { count: files.length }) : t('upload.drop')}</strong><p>{t('upload.orChoose')}</p></label>{files.length > 0 && <ul className="upload-file-list">{files.map((target, index) => <li key={`${target.name}-${index}`}><span>{target.name}</span>{!busy && <button type="button" onClick={() => removeFile(index)} aria-label={t('upload.remove')}><Icon name="x" size={12} /></button>}</li>)}</ul>}<label className="upload-ai-option"><input type="checkbox" checked={aiProcessingEnabled} disabled={busy} onChange={(event) => setAiProcessingEnabled(event.target.checked)} />{t('upload.aiProcessing')}</label>{busy && <div className="upload-progress"><span style={{ width: `${progress}%` }} /><small>{t('upload.progress', { percent: progress })}</small></div>}{error && <p className="dialog-error" role="alert">{error}</p>}<div className="dialog-actions"><button type="button" className="secondary-button" disabled={busy} onClick={onClose}>{t('upload.cancel')}</button><button type="submit" className="primary-button" disabled={!files.length || busy}>{t('upload.submit')}</button></div></form></div>
}

function CreateFolderDialog({ parentUid, onClose, onComplete }: { parentUid: string | null; onClose: () => void; onComplete: () => Promise<void> | void }) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const folderName = name.trim()
    if (!folderName) return
    setBusy(true)
    setError(null)
    try {
      await workspaceApi.createFolder(folderName, parentUid)
      await onComplete()
    } catch (caught) {
      setError(mutationErrors(caught).join(' '))
    } finally {
      setBusy(false)
    }
  }

  return <div className="modal-backdrop" onMouseDown={busy ? undefined : onClose}><form className="compact-dialog" onSubmit={(event) => void submit(event)} onMouseDown={(event) => event.stopPropagation()}><div className="dialog-head"><div><h2>{t('folder.title')}</h2><p>{t('folder.description')}</p></div><button type="button" disabled={busy} onClick={onClose}><Icon name="x" /></button></div><label className="dialog-field"><span>{t('folder.name')}</span><input autoFocus value={name} disabled={busy} placeholder={t('folder.placeholder')} onChange={(event) => setName(event.target.value)} /></label>{error && <p className="dialog-error" role="alert">{error}</p>}<div className="dialog-actions"><button type="button" className="secondary-button" disabled={busy} onClick={onClose}>{t('upload.cancel')}</button><button type="submit" className="primary-button" disabled={!name.trim() || busy}>{t('folder.submit')}</button></div></form></div>
}

function MoveDialog({ count, onClose, onMove }: { count: number; onClose: () => void; onMove: (parentUid: string | null) => Promise<void> }) {
  const { t } = useI18n()
  const [folders, setFolders] = useState<DocumentFolder[]>([])
  const [destination, setDestination] = useState('root')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    void workspaceApi.listFolders().then(setFolders).catch((caught) => setError(mutationErrors(caught).join(' ')))
  }, [])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await onMove(destination === 'root' ? null : destination)
    } catch (caught) {
      setError(mutationErrors(caught).join(' '))
    } finally {
      setBusy(false)
    }
  }

  return <div className="modal-backdrop" onMouseDown={busy ? undefined : onClose}><form className="compact-dialog" onSubmit={(event) => void submit(event)} onMouseDown={(event) => event.stopPropagation()}><div className="dialog-head"><div><h2>{t('move.title')}</h2><p>{t('move.description', { count })}</p></div><button type="button" disabled={busy} onClick={onClose}><Icon name="x" /></button></div><label className="dialog-field"><span>{t('move.destination')}</span><select value={destination} disabled={busy} onChange={(event) => setDestination(event.target.value)}><option value="root">{t('move.root')}</option>{folders.map((folder) => <option key={folder.uid} value={folder.uid}>{folder.path}</option>)}</select></label>{error && <p className="dialog-error" role="alert">{error}</p>}<div className="dialog-actions"><button type="button" className="secondary-button" disabled={busy} onClick={onClose}>{t('upload.cancel')}</button><button type="submit" className="primary-button" disabled={busy}>{t('move.submit')}</button></div></form></div>
}

function ConfirmDialog({ title, description, confirmLabel, danger, busy, onConfirm, onCancel }: { title: string; description: string; confirmLabel: string; danger?: boolean; busy?: boolean; onConfirm: () => void; onCancel: () => void }) {
  const { t } = useI18n()
  return <div className="modal-backdrop" onMouseDown={busy ? undefined : onCancel}><div className="compact-dialog" onMouseDown={(event) => event.stopPropagation()}><div className="dialog-head"><div><h2>{title}</h2><p>{description}</p></div><button type="button" disabled={busy} onClick={onCancel}><Icon name="x" /></button></div><div className="dialog-actions"><button type="button" className="secondary-button" disabled={busy} onClick={onCancel}>{t('upload.cancel')}</button><button type="button" className={`primary-button${danger ? ' danger' : ''}`} disabled={busy} onClick={onConfirm}>{confirmLabel}</button></div></div></div>
}
