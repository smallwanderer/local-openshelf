import { useEffect, useState } from 'react'
import { ApiClientError } from '../../api/http'
import type { DocumentReadiness } from '../../api/models'
import { tenantApi, type WorkspaceSummary } from '../../api/tenants'
import { workspaceApi } from '../../api/workspace'
import { Icon } from '../../components/Icon'
import { useI18n } from '../../i18n'
import { CreateWorkspaceModal } from '../workspace/CreateWorkspaceModal'

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiClientError ? err.message : fallback
}

export function HomeFeature({ onOpen, userName, tenantEnabled }: { onOpen: () => void; userName: string; tenantEnabled: boolean }) {
  const { locale, t } = useI18n()
  const [readiness, setReadiness] = useState<DocumentReadiness | null>(null)
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([])
  const [activeUid, setActiveUid] = useState('')
  const [creatingWorkspace, setCreatingWorkspace] = useState(false)
  const [switching, setSwitching] = useState('')
  const [switchError, setSwitchError] = useState('')

  useEffect(() => {
    let active = true
    workspaceApi.getReadiness().then((value) => { if (active) setReadiness(value) }).catch(() => undefined)
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (!tenantEnabled) return
    let active = true
    tenantApi.listWorkspaces()
      .then((payload) => {
        if (!active) return
        setWorkspaces(payload.workspaces)
        setActiveUid(payload.active_workspace_uid)
      })
      .catch(() => undefined)
    return () => { active = false }
  }, [tenantEnabled])

  async function openWorkspace(workspace: WorkspaceSummary) {
    if (workspace.uid === activeUid) {
      onOpen()
      return
    }
    if (switching) return
    setSwitching(workspace.uid)
    setSwitchError('')
    try {
      await tenantApi.switchWorkspace(workspace.uid)
      window.location.href = '/workspace/documents/'
    } catch (err) {
      setSwitchError(errorMessage(err, t('workspace.genericError')))
      setSwitching('')
    }
  }

  const date = new Intl.DateTimeFormat(locale, { dateStyle: 'long' }).format(new Date())

  return <div className="home-page">
    <div className="home-greeting"><span>{date}</span><h1>{t('home.greeting', { name: userName })}</h1><p>{t('home.prompt')}</p></div>
    {switchError && <p className="dialog-error" role="alert">{switchError}</p>}
    <div className="home-grid">
      <button className="new-knowledge" disabled={!tenantEnabled} onClick={() => setCreatingWorkspace(true)}><span><Icon name="plus" /></span><strong>{t('home.newStore')}</strong><small>{t('home.newStoreDescription')}</small></button>
      {workspaces.map((workspace) => {
        const isActive = workspace.uid === activeUid
        return <button
          key={workspace.uid}
          className="knowledge-card"
          disabled={Boolean(switching)}
          onClick={() => void openWorkspace(workspace)}
        >
          <div className="card-top"><span className="knowledge-symbol"><Icon name="book" /></span><Icon name="dots" /></div>
          <strong>{workspace.name}</strong>
          <p>{isActive ? t('home.myDocumentsDescription') : t('home.switchToOpen')}</p>
          <div className="card-meta">
            {isActive ? <>
              <span>{readiness ? t('document.dynamicCount', { count: readiness.totalFiles }) : '—'}</span>
              <span className="ready-pill">{readiness ? t('document.readiness', { ready: readiness.searchableFiles, total: readiness.totalFiles }) : t('state.loadingTitle')}</span>
            </> : <>
              <span>{t(workspace.role === 'admin' ? 'workspace.roleAdmin' : 'workspace.roleMember')}</span>
              <span className="ready-pill">{switching === workspace.uid ? t('workspace.switching') : t('workspace.switchLabel')}</span>
            </>}
          </div>
        </button>
      })}
      <button className="knowledge-card planned-card" disabled><em className="future-badge">{t('future.badge')}</em><div className="card-top"><span className="knowledge-symbol blue"><Icon name="archive" /></span><Icon name="dots" /></div><strong>{t('home.researchArchive')}</strong><p>{t('home.researchDescription')}</p><div className="card-meta"><span>—</span><span className="ready-pill">{t('workspace.planned')}</span></div></button>
    </div>
    {creatingWorkspace && <CreateWorkspaceModal onClose={() => setCreatingWorkspace(false)} />}
  </div>
}
