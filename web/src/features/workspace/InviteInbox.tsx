import { useEffect, useState } from 'react'
import { ApiClientError } from '../../api/http'
import { tenantApi, type WorkspaceInvitation } from '../../api/tenants'
import { Icon } from '../../components/Icon'
import { useI18n } from '../../i18n'

interface InviteInboxProps {
  enabled: boolean
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiClientError ? err.message : fallback
}

export function InviteInbox({ enabled }: InviteInboxProps) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const [invitations, setInvitations] = useState<WorkspaceInvitation[]>([])
  const [pendingId, setPendingId] = useState<number | null>(null)
  const [pendingAction, setPendingAction] = useState<'accept' | 'decline' | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!enabled) return
    let active = true
    tenantApi.listInviteInbox()
      .then((list) => { if (active) setInvitations(list) })
      .catch(() => {})
    return () => { active = false }
  }, [enabled])

  async function respond(invitation: WorkspaceInvitation, accept: boolean) {
    if (pendingId !== null) return
    setPendingId(invitation.id)
    setPendingAction(accept ? 'accept' : 'decline')
    setError('')
    try {
      if (accept) {
        await tenantApi.acceptInvite(invitation.id)
        window.location.reload()
        return
      }
      await tenantApi.declineInvite(invitation.id)
      setInvitations((current) => current.filter((item) => item.id !== invitation.id))
    } catch (err) {
      setError(errorMessage(err, t('workspace.genericError')))
    } finally {
      setPendingId(null)
      setPendingAction(null)
    }
  }

  if (!enabled) return null

  return <>
    <button
      type="button"
      className="icon-button invite-inbox-button"
      title={t('invite.inboxLabel')}
      aria-label={t('invite.inboxLabel')}
      onClick={() => setOpen(true)}
    >
      <Icon name="mail" />
      {invitations.length > 0 && <span className="invite-inbox-badge">{invitations.length}</span>}
    </button>
    {open && <div className="modal-backdrop" onMouseDown={() => setOpen(false)}>
      <div className="compact-dialog" onMouseDown={(event) => event.stopPropagation()}>
        <div className="dialog-head">
          <div><h2>{t('invite.inboxTitle')}</h2></div>
          <button type="button" onClick={() => setOpen(false)}><Icon name="x" /></button>
        </div>
        {error && <p className="dialog-error" role="alert">{error}</p>}
        {invitations.length === 0 ? <p className="workspace-muted">{t('invite.inboxEmpty')}</p> : <div className="share-people">
          {invitations.map((invitation) => <div key={invitation.id} className="share-person">
            <span className="share-avatar">{invitation.workspace.name.slice(0, 1).toUpperCase()}</span>
            <div><strong>{invitation.workspace.name}</strong>{invitation.invited_by && <small>{t('invite.from', { name: invitation.invited_by })}</small>}</div>
            <div className="workspace-inline-row">
              <button type="button" className="secondary-button" disabled={pendingId === invitation.id} onClick={() => void respond(invitation, false)}>
                {pendingId === invitation.id && pendingAction === 'decline' ? t('invite.declining') : t('invite.decline')}
              </button>
              <button type="button" className="primary-button" disabled={pendingId === invitation.id} onClick={() => void respond(invitation, true)}>
                {pendingId === invitation.id && pendingAction === 'accept' ? t('invite.accepting') : t('invite.accept')}
              </button>
            </div>
          </div>)}
        </div>}
      </div>
    </div>}
  </>
}
