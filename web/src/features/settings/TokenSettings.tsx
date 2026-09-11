import { type FormEvent, useCallback, useEffect, useState } from 'react'
import { tokensApi, type AccessTokenLevel, type AccessTokenSummary } from '../../api/tokens'
import { Icon } from '../../components/Icon'
import { useI18n, type Locale } from '../../i18n'

interface TokenSettingsProps {
  enabled: boolean
}

function formatDate(value: string | null, locale: Locale): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat(locale === 'ko' ? 'ko-KR' : 'en-US', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

function tokenKey(token: Pick<AccessTokenSummary, 'id' | 'token_type'>): string {
  return `${token.token_type}:${token.id}`
}

export function TokenSettings({ enabled }: TokenSettingsProps) {
  const { locale, t } = useI18n()
  const [tokens, setTokens] = useState<AccessTokenSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [name, setName] = useState('')
  const [accessLevel, setAccessLevel] = useState<AccessTokenLevel>('read_only')
  const [issuing, setIssuing] = useState(false)
  const [issuedSecret, setIssuedSecret] = useState<string | null>(null)
  const [issuedLevel, setIssuedLevel] = useState<AccessTokenLevel>('read_only')
  const [copied, setCopied] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)
  const [pendingRevokeKey, setPendingRevokeKey] = useState<string | null>(null)
  const [revokingKey, setRevokingKey] = useState<string | null>(null)
  const [pendingDeleteKey, setPendingDeleteKey] = useState<string | null>(null)
  const [deletingKey, setDeletingKey] = useState<string | null>(null)

  const loadTokens = useCallback(async () => {
    if (!enabled) return
    setLoading(true)
    setError('')
    try {
      setTokens(await tokensApi.list())
    } catch {
      setError(t('settings.tokenLoadFailed'))
    } finally {
      setLoading(false)
    }
  }, [enabled, t])

  useEffect(() => {
    if (enabled) {
      void loadTokens()
    } else {
      setTokens([])
      setIssuedSecret(null)
    }
  }, [enabled, loadTokens])

  async function issueToken(event: FormEvent) {
    event.preventDefault()
    const tokenName = name.trim()
    if (!tokenName || issuing) return
    setIssuing(true)
    setError('')
    setIssuedSecret(null)
    setCopied(false)
    setCopyFailed(false)
    try {
      const payload = await tokensApi.issue(tokenName, accessLevel)
      setTokens((current) => [payload.token, ...current])
      setIssuedSecret(payload.secret)
      setIssuedLevel(payload.token.access_level)
      setName('')
    } catch {
      setError(t('settings.tokenIssueFailed'))
    } finally {
      setIssuing(false)
    }
  }

  async function copySecret() {
    if (!issuedSecret) return
    setCopyFailed(false)
    try {
      if (!navigator.clipboard) throw new Error('Clipboard API unavailable')
      await navigator.clipboard.writeText(issuedSecret)
      setCopied(true)
    } catch {
      setCopied(false)
      setCopyFailed(true)
    }
  }

  async function revokeToken(token: AccessTokenSummary) {
    const key = tokenKey(token)
    if (revokingKey) return
    setRevokingKey(key)
    setError('')
    try {
      await tokensApi.revoke(token)
      setTokens((current) => token.token_type === 'sync'
        ? current.filter((item) => tokenKey(item) !== key)
        : current.map((item) => tokenKey(item) === key ? { ...item, is_active: false } : item))
      setPendingRevokeKey(null)
    } catch {
      setError(t('settings.tokenRevokeFailed'))
    } finally {
      setRevokingKey(null)
    }
  }

  async function deleteToken(token: AccessTokenSummary) {
    const key = tokenKey(token)
    if (deletingKey) return
    setDeletingKey(key)
    setError('')
    try {
      await tokensApi.remove(token)
      setTokens((current) => current.filter((item) => tokenKey(item) !== key))
      setPendingDeleteKey(null)
    } catch {
      setError(t('settings.tokenDeleteFailed'))
    } finally {
      setDeletingKey(null)
    }
  }

  const accessLevelLabel = (level: AccessTokenLevel) => {
    if (level === 'read_only') return t('settings.tokenTypeReadOnly')
    if (level === 'read_write') return t('settings.tokenTypeReadWrite')
    return t('settings.tokenTypeSync')
  }
  const accessLevelDescription = (level: AccessTokenLevel) => {
    if (level === 'read_only') return t('settings.tokenTypeReadOnlyDescription')
    if (level === 'read_write') return t('settings.tokenTypeReadWriteDescription')
    return t('settings.tokenTypeSyncDescription')
  }
  const accessLevelMark = (level: AccessTokenLevel) => level === 'read_only' ? 'RO' : level === 'read_write' ? 'RW' : 'SYNC'
  const scopeLabel = (scope: string) => {
    const key = `settings.tokenScope.${scope}` as Parameters<typeof t>[0]
    return scope === 'documents:read' || scope === 'documents:write' || scope === 'search' || scope === 'rag' || scope === 'status:read' || scope === 'sync'
      ? t(key)
      : scope
  }

  return <section className="cli-token-settings" aria-labelledby="token-settings-title">
    <div className="cli-token-heading">
      <div><span className="eyebrow">ACCESS</span><h3 id="token-settings-title">{t('settings.tokenTitle')}</h3><p>{t('settings.tokenDescription')}</p></div>
      <button className="secondary-button" type="button" disabled={!enabled || loading} onClick={() => void loadTokens()}><Icon name="refresh" size={14}/>{t('settings.tokenRefresh')}</button>
    </div>

    {!enabled ? <div className="cli-token-empty">{t('settings.tokenUnavailable')}</div> : <>
      <form className="cli-token-form token-kind-form" onSubmit={(event) => void issueToken(event)}>
        <label><span>{t('settings.tokenType')}</span><select value={accessLevel} disabled={issuing || loading} onChange={(event) => setAccessLevel(event.target.value as AccessTokenLevel)}><option value="read_only">{t('settings.tokenTypeReadOnly')}</option><option value="read_write">{t('settings.tokenTypeReadWrite')}</option><option value="sync">{t('settings.tokenTypeSync')}</option></select></label>
        <label><span>{t('settings.tokenName')}</span><input value={name} maxLength={128} autoComplete="off" disabled={issuing || loading} placeholder={t(accessLevel === 'sync' ? 'settings.tokenNamePlaceholderSync' : 'settings.tokenNamePlaceholderCli')} onChange={(event) => setName(event.target.value)} /></label>
        <button className="primary-button" type="submit" disabled={!name.trim() || issuing || loading}>{issuing ? t('settings.tokenIssuing') : t('settings.tokenIssue')}</button>
        <p className="token-type-description"><strong>{accessLevelLabel(accessLevel)}</strong>{accessLevelDescription(accessLevel)}</p>
      </form>

      {issuedSecret && <div className="cli-secret" role="status">
        <div><Icon name="check" size={17}/><span><strong>{t('settings.tokenSecretTitle', { type: accessLevelLabel(issuedLevel) })}</strong><small>{t('settings.tokenSecretDescription')}</small></span></div>
        <code>{issuedSecret}</code>
        <div className="cli-secret-actions"><button className="primary-button" type="button" onClick={() => void copySecret()}><Icon name={copied ? 'check' : 'copy'} size={14}/>{copied ? t('settings.tokenCopied') : t('settings.tokenCopy')}</button><button className="secondary-button" type="button" onClick={() => { setIssuedSecret(null); setCopied(false); setCopyFailed(false) }}>{t('settings.tokenSecretDone')}</button></div>
        {copyFailed && <p role="alert">{t('settings.tokenCopyFailed')}</p>}
      </div>}

      {error && <div className="settings-error" role="alert">{error}</div>}
      <div className="cli-token-list" aria-live="polite">
        {loading ? <div className="cli-token-empty">{t('state.loadingTitle')}</div> : tokens.length === 0 ? <div className="cli-token-empty">{t('settings.tokenEmpty')}</div> : tokens.map((token) => {
          const key = tokenKey(token)
          return <article key={key} className={`cli-token-item ${token.is_active ? '' : 'revoked'}`}>
            <div className="cli-token-main"><span className={`cli-token-mark ${token.token_type}`}>{accessLevelMark(token.access_level)}</span><span><strong>{token.name}</strong><code>{token.prefix}…</code></span></div>
            <div className="cli-token-scopes"><b>{accessLevelLabel(token.access_level)}</b>{token.scopes.map((scope) => <span key={scope} className={scope === 'sync' ? 'sync' : ''}>{scopeLabel(scope)}</span>)}</div>
            <div className="cli-token-meta"><span>{token.is_active ? t('settings.tokenActive') : t('settings.tokenRevoked')}</span><small>{t('settings.tokenCreated', { date: formatDate(token.created_at, locale) })}</small><small>{t('settings.tokenLastUsed', { date: formatDate(token.last_used_at, locale) })}</small></div>
            <div className="cli-token-actions">{token.is_active
              ? (pendingRevokeKey === key
                  ? <><button className="cli-revoke-confirm" type="button" disabled={revokingKey === key} onClick={() => void revokeToken(token)}>{revokingKey === key ? t('settings.tokenRevoking') : t('settings.tokenRevokeConfirm')}</button><button className="secondary-button" type="button" disabled={Boolean(revokingKey)} onClick={() => setPendingRevokeKey(null)}>{t('upload.cancel')}</button></>
                  : <button className="secondary-button cli-revoke" type="button" disabled={Boolean(revokingKey)} onClick={() => setPendingRevokeKey(key)}><Icon name="x" size={14}/>{t('settings.tokenRevoke')}</button>)
              : (pendingDeleteKey === key
                  ? <><button className="cli-revoke-confirm" type="button" disabled={deletingKey === key} onClick={() => void deleteToken(token)}>{deletingKey === key ? t('settings.tokenDeleting') : t('settings.tokenDeleteConfirm')}</button><button className="secondary-button" type="button" disabled={Boolean(deletingKey)} onClick={() => setPendingDeleteKey(null)}>{t('upload.cancel')}</button></>
                  : <button className="secondary-button cli-revoke" type="button" disabled={Boolean(deletingKey)} onClick={() => setPendingDeleteKey(key)}><Icon name="trash" size={14}/>{t('settings.tokenDelete')}</button>)}</div>
          </article>
        })}
      </div>
    </>}
  </section>
}
