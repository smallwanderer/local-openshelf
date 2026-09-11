import { type FormEvent, useState } from 'react'
import { ApiClientError } from '../../api/http'
import { tenantApi } from '../../api/tenants'
import { Icon } from '../../components/Icon'
import { useI18n } from '../../i18n'

interface CreateWorkspaceModalProps {
  onClose: () => void
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiClientError ? err.message : fallback
}

export function CreateWorkspaceModal({ onClose }: CreateWorkspaceModalProps) {
  const { t } = useI18n()
  const [teamName, setTeamName] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState('')

  const [joinCode, setJoinCode] = useState('')
  const [joining, setJoining] = useState(false)
  const [joinError, setJoinError] = useState('')

  async function createWorkspace(event: FormEvent) {
    event.preventDefault()
    const name = teamName.trim()
    if (!name || creating) return
    setCreating(true)
    setCreateError('')
    try {
      await tenantApi.createWorkspace(name, 'team')
      window.location.reload()
    } catch (err) {
      setCreateError(errorMessage(err, t('workspace.genericError')))
      setCreating(false)
    }
  }

  async function joinByCode(event: FormEvent) {
    event.preventDefault()
    const code = joinCode.trim()
    if (!code || joining) return
    setJoining(true)
    setJoinError('')
    try {
      await tenantApi.redeemInviteCode(code)
      window.location.href = '/workspace/documents/'
    } catch (err) {
      setJoinError(errorMessage(err, t('workspace.genericError')))
      setJoining(false)
    }
  }

  return <div className="modal-backdrop" onMouseDown={onClose}>
    <div className="compact-dialog" onMouseDown={(event) => event.stopPropagation()}>
      <div className="dialog-head">
        <div><h2>{t('home.newStore')}</h2></div>
        <button type="button" onClick={onClose}><Icon name="x" /></button>
      </div>

      <form className="dialog-field workspace-inline-form" onSubmit={(event) => void createWorkspace(event)}>
        <span>{t('workspace.createWorkspaceTitle')}</span>
        <p className="workspace-muted">{t('workspace.createDescription')}</p>
        <div className="workspace-inline-row">
          <input
            value={teamName}
            maxLength={128}
            disabled={creating}
            placeholder={t('workspace.createPlaceholder')}
            onChange={(event) => setTeamName(event.target.value)}
            autoFocus
          />
          <button type="submit" className="primary-button" disabled={!teamName.trim() || creating}>
            {creating ? t('workspace.createCreating') : t('workspace.createSubmit')}
          </button>
        </div>
        {createError && <p className="dialog-error" role="alert">{createError}</p>}
      </form>

      <form className="dialog-field workspace-inline-form" onSubmit={(event) => void joinByCode(event)}>
        <span>{t('workspace.joinByCodeTitle')}</span>
        <div className="workspace-inline-row">
          <input
            value={joinCode}
            disabled={joining}
            placeholder={t('workspace.joinCodePlaceholder')}
            onChange={(event) => setJoinCode(event.target.value)}
          />
          <button type="submit" className="primary-button" disabled={!joinCode.trim() || joining}>
            {joining ? t('workspace.joining') : t('workspace.joinSubmit')}
          </button>
        </div>
        {joinError && <p className="dialog-error" role="alert">{joinError}</p>}
      </form>
    </div>
  </div>
}
