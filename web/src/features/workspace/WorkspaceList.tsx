import { useEffect, useState } from 'react'
import { ApiClientError } from '../../api/http'
import { tenantApi, type WorkspaceSummary } from '../../api/tenants'
import { useI18n } from '../../i18n'

interface WorkspaceListProps {
  enabled: boolean
  onNavigateHome: () => void
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiClientError ? err.message : fallback
}

export function WorkspaceList({ enabled, onNavigateHome }: WorkspaceListProps) {
  const { t } = useI18n()
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([])
  const [activeUid, setActiveUid] = useState('')
  const [switching, setSwitching] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!enabled) return
    let active = true
    tenantApi.listWorkspaces()
      .then((payload) => {
        if (!active) return
        setWorkspaces(payload.workspaces)
        setActiveUid(payload.active_workspace_uid)
      })
      .catch(() => {})
    return () => { active = false }
  }, [enabled])

  async function selectWorkspace(uid: string) {
    if (switching) return
    if (uid === activeUid) {
      onNavigateHome()
      return
    }
    setSwitching(uid)
    setError('')
    try {
      await tenantApi.switchWorkspace(uid)
      window.location.href = '/workspace/documents/'
    } catch (err) {
      setError(errorMessage(err, t('workspace.genericError')))
      setSwitching('')
    }
  }

  if (!enabled) return null

  return <div className="workspace-list">
    {error && <p className="dialog-error" role="alert">{error}</p>}
    {workspaces.map((workspace) => {
      const isActive = workspace.uid === activeUid
      return <div key={workspace.uid} className="workspace-row-wrap">
        <button
          type="button"
          className={`workspace-row ${isActive ? 'active' : ''}`}
          disabled={Boolean(switching)}
          onClick={() => void selectWorkspace(workspace.uid)}
          aria-label={t('workspace.switchLabel')}
        >
          <span className="workspace-avatar">{workspace.name.slice(0, 1).toUpperCase()}</span>
          <span className="workspace-row-label">
            <strong>{workspace.name}</strong>
            <small>{switching === workspace.uid ? t('workspace.switching') : t(workspace.role === 'admin' ? 'workspace.roleAdmin' : 'workspace.roleMember')}</small>
          </span>
        </button>
      </div>
    })}
  </div>
}
