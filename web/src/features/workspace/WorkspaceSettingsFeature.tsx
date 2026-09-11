import { type ChangeEvent, type FormEvent, useEffect, useMemo, useState } from 'react'
import type { ServerPolicySummary } from '../../api/models'
import {
  evaluationDatasetApi, evaluationRunApi, qualityProfileApi,
  type EvaluationDataset, type EvaluationRun, type GenerationConfig,
  type PromptPolicy, type PromptRoute, type QualityFieldSchema,
  type QualityProfileEnvelope, type QualityProfileRow, type QualityProfileVersionSummary,
  type RetrievalConfig, type SaveQualityProfileDraftInput,
} from '../../api/qualityProfiles'
import { tenantApi, type WorkspaceMember, type WorkspaceSummary } from '../../api/tenants'
import { ApiClientError } from '../../api/http'
import { Icon } from '../../components/Icon'
import { type TranslationKey, useI18n } from '../../i18n'

interface WorkspaceSettingsFeatureProps {
  policy: ServerPolicySummary | null
  loading: boolean
  failed: boolean
}

type SettingsTab = 'general' | 'parameters' | 'evaluation'

interface ParamForm {
  retrieval: RetrievalConfig
  generation: GenerationConfig
  prompt_policy: PromptPolicy
}

const RETRIEVAL_WEIGHT_FIELDS: (keyof RetrievalConfig)[] = ['candidate_multiplier', 'query_sparse_top_n']
const POOLING_FIELDS: (keyof RetrievalConfig)[] = ['pooling_method', 'pool_top_k', 'pool_tau', 'doc_length_penalty_alpha', 'per_node_candidate_cap']
const EXPOSURE_FIELDS: (keyof RetrievalConfig)[] = ['search_top_k', 'rag_search_top_k', 'retrieval_threshold', 'evidence_top_k', 'evidence_context_window']
const GENERATION_FIELDS: (keyof GenerationConfig)[] = ['max_output_tokens', 'temperature', 'top_p']

const STEPPER_FIELDS = new Set(['candidate_multiplier', 'pool_top_k', 'per_node_candidate_cap', 'search_top_k', 'rag_search_top_k', 'evidence_top_k', 'evidence_context_window'])
const DECIMAL_FIELDS: Record<string, number> = { pool_tau: 1, doc_length_penalty_alpha: 2, retrieval_threshold: 2, temperature: 2, top_p: 2 }

const FALLBACK_RETRIEVAL_SCHEMA: Record<string, QualityFieldSchema> = {
  dense_weight: { type: 'number', tier: 'core', minimum: 0, maximum: 1, step: 0.05, effects: ['retrieval_quality'] },
  sparse_weight: { type: 'number', tier: 'core', minimum: 0, maximum: 1, step: 0.05, effects: ['retrieval_quality'] },
  search_top_k: { type: 'integer', tier: 'core', minimum: 1, maximum: 50, step: 1, effects: ['retrieval_quality', 'latency'] },
  rag_search_top_k: { type: 'integer', tier: 'core', minimum: 1, maximum: 10, step: 1, effects: ['context_quality', 'latency'] },
  retrieval_threshold: { type: 'number', tier: 'core', minimum: 0, maximum: 1, step: 0.05, effects: ['retrieval_quality'] },
  evidence_top_k: { type: 'integer', tier: 'core', minimum: 1, maximum: 10, step: 1, effects: ['context_quality'] },
  evidence_context_window: { type: 'integer', tier: 'core', minimum: 0, maximum: 3, step: 1, effects: ['context_quality', 'latency'] },
  candidate_multiplier: { type: 'integer', tier: 'advanced', minimum: 1, maximum: 50, step: 1, effects: ['retrieval_quality', 'latency'] },
  per_node_candidate_cap: { type: 'integer', tier: 'advanced', minimum: 1, maximum: 20, step: 1, effects: ['retrieval_quality'] },
  query_sparse_top_n: { type: 'integer', tier: 'advanced', minimum: 1, maximum: 256, step: 1, effects: ['retrieval_quality', 'latency'] },
  pooling_method: { type: 'enum', tier: 'advanced', choices: ['normalized_logsumexp', 'normalized_softmax', 'max'], effects: ['retrieval_quality'] },
  pool_top_k: { type: 'integer', tier: 'advanced', minimum: 1, maximum: 20, step: 1, effects: ['retrieval_quality'] },
  pool_tau: { type: 'number', tier: 'advanced', minimum: 0.1, maximum: 20, step: 0.1, effects: ['retrieval_quality'] },
  doc_length_penalty_alpha: { type: 'number', tier: 'advanced', minimum: 0, maximum: 1, step: 0.05, effects: ['retrieval_quality'] },
  contextual_compression: { type: 'boolean', tier: 'advanced', effects: ['context_quality', 'latency'] },
}
const FALLBACK_GENERATION_SCHEMA: Record<string, QualityFieldSchema> = {
  max_output_tokens: { type: 'integer', tier: 'core', minimum: 64, maximum: 8192, step: 64, effects: ['generation_quality', 'latency'] },
  temperature: { type: 'number', tier: 'core', minimum: 0, maximum: 2, step: 0.05, effects: ['generation_quality'] },
  top_p: { type: 'number', tier: 'advanced', minimum: 0.01, maximum: 1, step: 0.01, effects: ['generation_quality'] },
}

function copyConfig<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiClientError) return error.message
  return error instanceof Error ? error.message : fallback
}

function formatDate(value: string | null, locale: string): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat(locale === 'ko' ? 'ko-KR' : 'en-US', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

function validationTone(state: string): string {
  if (state === 'verified') return 'verified'
  if (state === 'stale' || state === 'unverified') return 'warning'
  return 'neutral'
}

function changedOverrides<T extends Record<string, unknown>>(active: T, candidate: T): Partial<T> {
  return Object.fromEntries(Object.keys(candidate)
    .filter((key) => JSON.stringify(active[key]) !== JSON.stringify(candidate[key]))
    .map((key) => [key, candidate[key]])) as Partial<T>
}

function axisOverrides<T extends Record<string, unknown>>(activeEffective: T, candidate: T, draftChangedFields: string[], resetFields: string[]): Partial<T> {
  const keys = new Set([...Object.keys(changedOverrides(activeEffective, candidate)), ...draftChangedFields])
  resetFields.forEach((field) => keys.delete(field))
  return Object.fromEntries([...keys].map((field) => [field, candidate[field]])) as Partial<T>
}

function ProfileSummary({ revision, kind }: { revision: QualityProfileRow; kind: 'active' | 'draft' }) {
  const { locale, t } = useI18n()
  return <div className={`quality-revision ${kind}`}>
    <div><span className={`quality-status ${validationTone(revision.validation.state)}`}>{t(`workspaceSettings.validation.${revision.validation.state}` as TranslationKey)}</span><strong>{kind === 'active' ? t('workspaceSettings.activeVersion', { version: revision.version }) : t('workspaceSettings.draftRevision', { revision: revision.revision })}</strong></div>
    <small>{formatDate(revision.updated_at, locale)}{revision.note ? ` · ${revision.note}` : ''}</small>
  </div>
}

function EffectBadges({ effects = [] }: { effects?: string[] }) {
  const { t } = useI18n()
  return <span className="quality-effects">{effects.map((effect) => <em key={effect}>{t(`workspaceSettings.effect.${effect}` as TranslationKey)}</em>)}</span>
}

function Stepper({ value, min, max, step = 1, disabled, onChange }: { value: number; min: number; max: number; step?: number; disabled: boolean; onChange: (value: number) => void }) {
  return <div className="stepper">
    <button type="button" disabled={disabled || value <= min} onClick={() => onChange(Math.max(min, Math.round((value - step) * 100) / 100))}>−</button>
    <span className="stepper-value">{value}</span>
    <button type="button" disabled={disabled || value >= max} onClick={() => onChange(Math.min(max, Math.round((value + step) * 100) / 100))}>+</button>
  </div>
}

function SliderControl({ value, min, max, step, disabled, decimals = 0, onChange }: { value: number; min: number; max: number; step: number; disabled: boolean; decimals?: number; onChange: (value: number) => void }) {
  return <div className="range-slider">
    <input type="range" min={min} max={max} step={step} value={value} disabled={disabled} onChange={(event) => onChange(Number(event.target.value))} />
    <span className="range-value">{decimals ? value.toFixed(decimals) : value}</span>
  </div>
}

function SegControl({ value, choices, disabled, labelFor, onChange }: { value: string; choices: string[]; disabled: boolean; labelFor: (choice: string) => string; onChange: (value: string) => void }) {
  return <div className="seg-control">{choices.map((choice) => <button key={choice} type="button" className={value === choice ? 'on' : ''} disabled={disabled} onClick={() => onChange(choice)}>{labelFor(choice)}</button>)}</div>
}

function DualWeightSlider({ dense, disabled, onChange }: { dense: number; disabled: boolean; onChange: (dense: number) => void }) {
  const { t } = useI18n()
  return <div className="dual-slider">
    <input type="range" min={0} max={1} step={0.05} value={dense} disabled={disabled} onChange={(event) => onChange(Number(event.target.value))} />
    <div className="dual-slider-labels">
      <span>{t('workspaceSettings.field.dense_weight')} {dense.toFixed(2)}</span>
      <span>{t('workspaceSettings.field.sparse_weight')} {(1 - dense).toFixed(2)}</span>
    </div>
  </div>
}

interface FieldRowProps {
  name: string
  schema: QualityFieldSchema
  value: unknown
  activeValue: unknown
  defaultValue: unknown
  disabled: boolean
  onChange: (value: unknown) => void
  onRestore: () => void
  onReset: () => void
}

function FieldRow({ name, schema, value, activeValue, defaultValue, disabled, onChange, onRestore, onReset }: FieldRowProps) {
  const { t } = useI18n()
  const isChanged = JSON.stringify(value) !== JSON.stringify(activeValue)
  const inherited = JSON.stringify(value) === JSON.stringify(defaultValue)

  let control: React.ReactNode
  if (name === 'contextual_compression') {
    const enabled = Boolean((value as { enabled?: boolean } | undefined)?.enabled)
    control = <button type="button" className={`quality-toggle ${enabled ? 'active' : ''}`} role="switch" aria-checked={enabled} disabled={disabled} onClick={() => onChange({ enabled: !enabled })}><i /><span>{enabled ? t('action.enabled') : t('action.disabled')}</span></button>
  } else if (schema.type === 'enum') {
    control = <SegControl value={String(value ?? '')} choices={schema.choices ?? []} disabled={disabled} labelFor={(choice) => t(`workspaceSettings.choice.${choice}` as TranslationKey)} onChange={onChange} />
  } else if (name === 'retrieval_threshold') {
    const isSet = value != null
    control = <div className="range-slider nullable">
      <label className="nullable-toggle"><input type="checkbox" checked={isSet} disabled={disabled} onChange={(event) => onChange(event.target.checked ? (schema.minimum ?? 0) : null)} />{t('workspaceSettings.useThreshold')}</label>
      {isSet && <SliderControl value={Number(value)} min={schema.minimum ?? 0} max={schema.maximum ?? 1} step={schema.step ?? 0.05} disabled={disabled} decimals={2} onChange={onChange} />}
    </div>
  } else if (STEPPER_FIELDS.has(name)) {
    control = <Stepper value={Number(value)} min={schema.minimum ?? 0} max={schema.maximum ?? 100} step={schema.step ?? 1} disabled={disabled} onChange={onChange} />
  } else {
    control = <SliderControl value={Number(value)} min={schema.minimum ?? 0} max={schema.maximum ?? 100} step={schema.step ?? 1} disabled={disabled} decimals={DECIMAL_FIELDS[name] ?? 0} onChange={onChange} />
  }

  return <div className={`quality-field ${isChanged ? 'changed' : ''}`}>
    <div className="quality-field-head"><span><strong>{t(`workspaceSettings.field.${name}` as TranslationKey)}</strong><small>{t(`workspaceSettings.fieldHelp.${name}` as TranslationKey)}</small></span><EffectBadges effects={schema.effects} /></div>
    <div className="quality-input-row">
      {control}
      <span className="quality-field-actions"><button type="button" className="text-button" disabled={disabled || !isChanged} onClick={onRestore}>{t('workspaceSettings.restoreActive')}</button><button type="button" className="text-button" disabled={disabled || inherited} onClick={onReset}>{t('workspaceSettings.inheritDefault')}</button></span>
    </div>
    <div className="quality-field-meta"><small>{t('workspaceSettings.currentValue', { value: String(activeValue ?? '—') })}</small><small>{t('workspaceSettings.defaultValue', { value: String(defaultValue ?? '—') })}</small>{isChanged && <b>{t('workspaceSettings.changed')}</b>}</div>
  </div>
}

function ParamGroup({ titleKey, helpKey, axis, children }: { titleKey: TranslationKey; helpKey: TranslationKey; axis?: 'retrieval' | 'generation'; children: React.ReactNode }) {
  const { t } = useI18n()
  return <section className="quality-param-group">
    <div className="quality-section-head"><div><h3>{t(titleKey)}</h3><p>{t(helpKey)}</p></div>{axis && <span className={`quality-axis-note axis-${axis}`}>{t(`workspaceSettings.axisLabel.${axis}` as TranslationKey)}</span>}</div>
    <div className="quality-field-grid">{children}</div>
  </section>
}

function DraftActions({ profile, busy, note, setNote, allowUnverified, setAllowUnverified, onSave, onDiscard, onApply }: {
  profile: QualityProfileEnvelope
  busy: boolean
  note: string
  setNote: (value: string) => void
  allowUnverified: boolean
  setAllowUnverified: (value: boolean) => void
  onSave: () => void
  onDiscard: () => void
  onApply: () => void
}) {
  const { t } = useI18n()
  const draft = profile.draft
  return <aside className="quality-draft-bar">
    <div><strong>{draft ? t('workspaceSettings.draftPending') : t('workspaceSettings.newDraft')}</strong><small>{t('workspaceSettings.draftFlow')}</small></div>
    <label><span>{t('workspaceSettings.changeNote')}</span><input value={note} disabled={busy || !profile.permissions.can_edit} maxLength={500} placeholder={t('workspaceSettings.changeNotePlaceholder')} onChange={(event) => setNote(event.target.value)} /></label>
    {draft && <label className="quality-unverified"><input type="checkbox" checked={allowUnverified} disabled={busy || !profile.permissions.can_apply} onChange={(event) => setAllowUnverified(event.target.checked)} /><span>{t('workspaceSettings.allowUnverified')}</span></label>}
    <div className="quality-actions">
      {draft && <button type="button" className="text-button danger" disabled={busy || !profile.permissions.can_edit} onClick={onDiscard}>{t('workspaceSettings.discard')}</button>}
      <button type="button" className="secondary-button" disabled={busy || !profile.permissions.can_edit} onClick={onSave}>{busy ? t('workspaceSettings.saving') : t('workspaceSettings.saveDraft')}</button>
      <button type="button" className="primary-button" disabled={busy || !draft || !profile.permissions.can_apply} onClick={onApply}>{t('workspaceSettings.apply')}</button>
    </div>
  </aside>
}

function GeneralSettingsPanel() {
  const { t } = useI18n()
  const [workspace, setWorkspace] = useState<WorkspaceSummary | null>(null)
  const [members, setMembers] = useState<WorkspaceMember[]>([])
  const [loadingMembers, setLoadingMembers] = useState(true)
  const [membersError, setMembersError] = useState('')
  const [pendingUserId, setPendingUserId] = useState<number | null>(null)

  const [issuing, setIssuing] = useState(false)
  const [issuedCode, setIssuedCode] = useState('')
  const [codeError, setCodeError] = useState('')
  const [copied, setCopied] = useState(false)

  const [inviteEmail, setInviteEmail] = useState('')
  const [sendingInvite, setSendingInvite] = useState(false)
  const [inviteError, setInviteError] = useState('')
  const [inviteSent, setInviteSent] = useState(false)

  useEffect(() => {
    let active = true
    setLoadingMembers(true)
    Promise.all([tenantApi.currentWorkspace(), tenantApi.listMembers()])
      .then(([currentWorkspace, list]) => {
        if (!active) return
        setWorkspace(currentWorkspace); setMembers(list)
      })
      .catch((err) => { if (active) setMembersError(errorMessage(err, t('workspace.genericError'))) })
      .finally(() => { if (active) setLoadingMembers(false) })
    return () => { active = false }
  }, [t])

  const isAdmin = workspace?.role === 'admin'

  async function changeRole(member: WorkspaceMember, role: 'admin' | 'member') {
    if (pendingUserId !== null || role === member.role) return
    setPendingUserId(member.user_id)
    setMembersError('')
    try {
      const updated = await tenantApi.changeMemberRole(member.user_id, role)
      setMembers((current) => current.map((item) => item.user_id === member.user_id ? updated : item))
    } catch (err) {
      setMembersError(errorMessage(err, t('workspace.genericError')))
    } finally {
      setPendingUserId(null)
    }
  }

  async function removeMember(member: WorkspaceMember) {
    if (pendingUserId !== null || !window.confirm(t('workspace.removeMemberConfirm'))) return
    setPendingUserId(member.user_id)
    setMembersError('')
    try {
      await tenantApi.removeMember(member.user_id)
      setMembers((current) => current.filter((item) => item.user_id !== member.user_id))
    } catch (err) {
      setMembersError(errorMessage(err, t('workspace.genericError')))
    } finally {
      setPendingUserId(null)
    }
  }

  async function issueCode() {
    if (issuing) return
    setIssuing(true)
    setCodeError('')
    setCopied(false)
    try {
      const payload = await tenantApi.issueInviteCode()
      setIssuedCode(payload.code)
    } catch (err) {
      setCodeError(errorMessage(err, t('workspace.genericError')))
    } finally {
      setIssuing(false)
    }
  }

  async function copyCode() {
    if (!issuedCode) return
    try {
      await navigator.clipboard.writeText(issuedCode)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  async function sendInvite(event: FormEvent) {
    event.preventDefault()
    const email = inviteEmail.trim()
    if (!email || sendingInvite) return
    setSendingInvite(true)
    setInviteError('')
    setInviteSent(false)
    try {
      await tenantApi.createInvite(email)
      setInviteSent(true)
      setInviteEmail('')
    } catch (err) {
      setInviteError(errorMessage(err, t('workspace.genericError')))
    } finally {
      setSendingInvite(false)
    }
  }

  if (loadingMembers) return <div className="quality-loading"><Icon name="refresh" /><span>{t('state.loadingTitle')}</span></div>

  return <div className="quality-general">
    <div className="quality-section-head">
      <div><h3>{t('workspaceSettings.generalTitle')}</h3><p>{t('workspaceSettings.generalDescription')}</p></div>
      <span className="quality-axis-note">{t('workspaceSettings.memberCount', { count: members.length })}</span>
    </div>

    {membersError && <div className="settings-error" role="alert">{membersError}</div>}

    <div className="workspace-member-list">
      {members.map((member) => <div key={member.user_id} className="workspace-member-row">
        <span className="share-avatar">{(member.display_name || member.email).slice(0, 1).toUpperCase()}</span>
        <div className="workspace-member-identity"><strong>{member.display_name || member.email}</strong><small>{member.email}</small></div>
        {isAdmin ? <select
          className="workspace-member-role-select"
          value={member.role}
          disabled={pendingUserId === member.user_id}
          onChange={(event) => void changeRole(member, event.target.value as 'admin' | 'member')}
        >
          <option value="admin">{t('workspace.roleAdmin')}</option>
          <option value="member">{t('workspace.roleMember')}</option>
        </select> : <span className="workspace-member-role">{t(member.role === 'admin' ? 'workspace.roleAdmin' : 'workspace.roleMember')}</span>}
        {isAdmin && <button type="button" className="text-button danger" disabled={pendingUserId === member.user_id} onClick={() => void removeMember(member)}>{t('workspace.removeMember')}</button>}
      </div>)}
    </div>

    {isAdmin ? <div className="workspace-invite-grid">
      <div className="workspace-invite-card">
        <h4>{t('workspace.inviteCodeTitle')}</h4>
        <p>{t('workspace.inviteCodeDescription')}</p>
        <div className="workspace-inline-row">
          <button type="button" className="secondary-button" disabled={issuing} onClick={() => void issueCode()}>{issuing ? t('workspace.inviteCodeIssuing') : t('workspace.inviteCodeIssue')}</button>
          {issuedCode && <button type="button" className="secondary-button" onClick={() => void copyCode()}><Icon name={copied ? 'check' : 'copy'} size={14} />{copied ? t('workspace.inviteCodeCopied') : t('workspace.inviteCodeCopy')}</button>}
        </div>
        {issuedCode && <code className="workspace-code">{issuedCode}</code>}
        {issuedCode && <p className="workspace-muted">{t('workspace.inviteCodeHint')}</p>}
        {codeError && <p className="dialog-error" role="alert">{codeError}</p>}
      </div>

      <form className="workspace-invite-card" onSubmit={(event) => void sendInvite(event)}>
        <h4>{t('workspace.inviteByEmailTitle')}</h4>
        <div className="workspace-inline-row">
          <input
            type="email"
            value={inviteEmail}
            disabled={sendingInvite}
            placeholder={t('workspace.inviteByEmailPlaceholder')}
            onChange={(event) => { setInviteEmail(event.target.value); setInviteSent(false) }}
          />
          <button type="submit" className="primary-button" disabled={!inviteEmail.trim() || sendingInvite}>
            {sendingInvite ? t('workspace.inviteByEmailSending') : t('workspace.inviteByEmailSubmit')}
          </button>
        </div>
        {inviteSent && <p className="workspace-muted">{t('workspace.inviteByEmailSent')}</p>}
        {inviteError && <p className="dialog-error" role="alert">{inviteError}</p>}
      </form>
    </div> : <div className="quality-readonly"><Icon name="users" /><span><strong>{t('workspaceSettings.readonlyTitle')}</strong><small>{t('workspace.adminOnlyNotice')}</small></span></div>}
  </div>
}

export function WorkspaceSettingsFeature({ policy, loading: policyLoading, failed: policyFailed }: WorkspaceSettingsFeatureProps) {
  const { locale, t } = useI18n()
  const [tab, setTab] = useState<SettingsTab>('parameters')
  const [profile, setProfile] = useState<QualityProfileEnvelope | null>(null)
  const [versions, setVersions] = useState<QualityProfileVersionSummary[]>([])
  const [form, setForm] = useState<ParamForm | null>(null)
  const [promptRoute, setPromptRoute] = useState<PromptRoute>('document_rag')
  const [promptPreview, setPromptPreview] = useState('')
  const [note, setNote] = useState('')
  const [resetFields, setResetFields] = useState<{ retrieval: string[]; generation: string[] }>({ retrieval: [], generation: [] })
  const [allowUnverified, setAllowUnverified] = useState(false)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const [evalDatasets, setEvalDatasets] = useState<EvaluationDataset[]>([])
  const [evalDatasetUid, setEvalDatasetUid] = useState('')
  const [evalCreatingDataset, setEvalCreatingDataset] = useState(false)
  const [evalDatasetName, setEvalDatasetName] = useState('')
  const [evalDatasetItemsText, setEvalDatasetItemsText] = useState('')
  const [evalDatasetError, setEvalDatasetError] = useState('')
  const [evalDatasetBusy, setEvalDatasetBusy] = useState(false)
  const [evalRun, setEvalRun] = useState<EvaluationRun | null>(null)
  const [evalBusy, setEvalBusy] = useState(false)
  const [evalError, setEvalError] = useState('')

  async function loadProfile(alive = () => true) {
    setLoading(true); setError(''); setNotice('')
    try {
      const payload = await qualityProfileApi.get()
      if (!alive()) return
      setProfile(payload)
      const base = payload.draft ?? payload.active
      setForm({
        retrieval: copyConfig(base.retrieval.effective),
        generation: copyConfig(base.generation.effective),
        prompt_policy: copyConfig(base.prompt_policy.effective as unknown as PromptPolicy),
      })
      setNote(payload.draft?.note ?? '')
      setResetFields({ retrieval: [], generation: [] })
      setPromptPreview('')
    } catch (loadError) {
      if (alive()) setError(errorMessage(loadError, t('workspaceSettings.loadFailed')))
    } finally { if (alive()) setLoading(false) }
  }

  async function loadEvaluationTab(alive = () => true) {
    setLoading(true); setError('')
    try {
      const [versionsPayload, datasetsPayload] = await Promise.all([
        qualityProfileApi.listVersions(),
        evaluationDatasetApi.list('retrieval'),
      ])
      if (!alive()) return
      setVersions(versionsPayload.results)
      setEvalDatasets(datasetsPayload.datasets)
      setEvalDatasetUid(''); setEvalCreatingDataset(false); setEvalDatasetName(''); setEvalDatasetItemsText('')
      setEvalDatasetError(''); setEvalRun(null); setEvalError('')
    } catch (loadError) {
      if (alive()) setError(errorMessage(loadError, t('workspaceSettings.loadFailed')))
    } finally { if (alive()) setLoading(false) }
  }

  useEffect(() => {
    let alive = true
    if (tab === 'general') { /* self-loading panel */ }
    else if (tab === 'parameters') void loadProfile(() => alive)
    else void loadEvaluationTab(() => alive)
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab])

  const retrievalSchema = useMemo(() => ({ ...FALLBACK_RETRIEVAL_SCHEMA, ...(profile?.schema.retrieval ?? {}) }), [profile])
  const generationSchema = useMemo(() => ({ ...FALLBACK_GENERATION_SCHEMA, ...(profile?.schema.generation ?? {}) }), [profile])
  const editDisabled = busy || !profile?.permissions.can_edit

  function changeField(axis: 'retrieval' | 'generation', field: string, value: unknown) {
    setResetFields((current) => ({ ...current, [axis]: current[axis].filter((item) => item !== field) }))
    setForm((current) => current && { ...current, [axis]: { ...current[axis], [field]: value } })
  }

  function inheritField(axis: 'retrieval' | 'generation', field: string, value: unknown) {
    setResetFields((current) => ({ ...current, [axis]: current[axis].includes(field) ? current[axis] : [...current[axis], field] }))
    setForm((current) => current && { ...current, [axis]: { ...current[axis], [field]: value } })
  }

  function changeDenseWeight(dense: number) {
    const sparse = Math.round((1 - dense) * 100) / 100
    setResetFields((current) => ({ ...current, retrieval: current.retrieval.filter((item) => item !== 'dense_weight' && item !== 'sparse_weight') }))
    setForm((current) => current && { ...current, retrieval: { ...current.retrieval, dense_weight: dense, sparse_weight: sparse } })
  }

  function promptRouteOverrides(): Partial<PromptPolicy> | undefined {
    if (!profile || !form) return undefined
    const activeEffective = profile.active.prompt_policy.effective as unknown as PromptPolicy
    const routes: PromptRoute[] = ['document_rag', 'no_retrieval']
    const changedRoutes = routes.filter((route) => (
      form.prompt_policy[route].mode !== activeEffective[route].mode
      || (form.prompt_policy[route].instruction || '') !== (activeEffective[route].instruction || '')
    ))
    const draftChanged = profile.draft?.prompt_policy.changed_fields ?? []
    const keys = new Set([...changedRoutes, ...draftChanged])
    if (!keys.size) return undefined
    return Object.fromEntries([...keys].map((route) => [route, {
      mode: form.prompt_policy[route as PromptRoute].mode,
      instruction: form.prompt_policy[route as PromptRoute].mode === 'replace' ? form.prompt_policy[route as PromptRoute].instruction : null,
    }])) as Partial<PromptPolicy>
  }

  async function saveDraft() {
    if (!profile || !form) return
    const sections: SaveQualityProfileDraftInput = {}
    const retrievalOverrides = axisOverrides(profile.active.retrieval.effective, form.retrieval, profile.draft?.retrieval.changed_fields ?? [], resetFields.retrieval)
    if (Object.keys(retrievalOverrides).length || resetFields.retrieval.length) sections.retrieval = { overrides: retrievalOverrides, reset_fields: resetFields.retrieval }
    const generationOverrides = axisOverrides(profile.active.generation.effective, form.generation, profile.draft?.generation.changed_fields ?? [], resetFields.generation)
    if (Object.keys(generationOverrides).length || resetFields.generation.length) sections.generation = { overrides: generationOverrides, reset_fields: resetFields.generation }
    const promptOverrides = promptRouteOverrides()
    if (promptOverrides) sections.prompt_policy = promptOverrides

    if (!Object.keys(sections).length) { setNotice(t('workspaceSettings.noChanges')); return }
    setBusy(true); setError(''); setNotice('')
    try {
      await qualityProfileApi.saveDraft(profile.draft?.revision ?? 0, sections, note)
      await loadProfile(); setNotice(t('workspaceSettings.draftSaved'))
    } catch (saveError) { setError(errorMessage(saveError, t('workspaceSettings.saveFailed'))) }
    finally { setBusy(false) }
  }

  async function discardDraft() {
    if (!profile?.draft || !window.confirm(t('workspaceSettings.discardConfirm'))) return
    setBusy(true); setError(''); setNotice('')
    try {
      await qualityProfileApi.discardDraft(profile.draft.revision)
      await loadProfile(); setNotice(t('workspaceSettings.draftDiscarded'))
    } catch (discardError) { setError(errorMessage(discardError, t('workspaceSettings.discardFailed'))) }
    finally { setBusy(false) }
  }

  async function applyDraft() {
    if (!profile?.draft || !window.confirm(t('workspaceSettings.applyConfirm'))) return
    setBusy(true); setError(''); setNotice('')
    try {
      const runUid = profile.draft.validation.last_run_uid
      await qualityProfileApi.apply(profile.draft.revision, runUid, allowUnverified, note)
      setAllowUnverified(false); await loadProfile(); setNotice(t('workspaceSettings.applied'))
    } catch (applyError) { setError(errorMessage(applyError, t('workspaceSettings.applyFailed'))) }
    finally { setBusy(false) }
  }

  async function previewPrompt() {
    if (!profile?.draft) { setError(t('workspaceSettings.saveBeforePreview')); return }
    setBusy(true); setError(''); setPromptPreview('')
    try {
      const preview = await qualityProfileApi.previewPrompt(profile.draft.revision, promptRoute)
      setPromptPreview(preview.assembled_prompt)
    } catch (previewError) { setError(errorMessage(previewError, t('workspaceSettings.previewFailed'))) }
    finally { setBusy(false) }
  }

  async function importPromptFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || !form) return
    const extension = file.name.toLowerCase().slice(file.name.lastIndexOf('.'))
    if (!['.txt', '.md'].includes(extension)) {
      setError(t('workspaceSettings.promptFileTypeError'))
      return
    }
    try {
      const instruction = (await file.text()).replace(/^﻿/, '').trim()
      if (!instruction) throw new Error(t('workspaceSettings.promptFileEmpty'))
      if (instruction.length > 12_000) throw new Error(t('workspaceSettings.promptFileTooLarge'))
      setForm({ ...form, prompt_policy: { ...form.prompt_policy, [promptRoute]: { mode: 'replace', instruction } } })
      setError('')
      setNotice(t('workspaceSettings.promptFileLoaded', { name: file.name }))
      setPromptPreview('')
    } catch (fileError) {
      setError(errorMessage(fileError, t('workspaceSettings.promptFileReadFailed')))
    }
  }

  function selectTab(next: SettingsTab) {
    setPromptPreview(''); setAllowUnverified(false); setTab(next)
  }

  useEffect(() => {
    if (!evalRun || evalRun.status === 'succeeded' || evalRun.status === 'failed') return
    let alive = true
    const timer = setInterval(() => {
      evaluationRunApi.get(evalRun.uid).then((result) => {
        if (!alive) return
        setEvalRun(result.run)
        if (result.run.status === 'succeeded') {
          qualityProfileApi.get().then((refreshed) => { if (alive) setProfile(refreshed) }).catch(() => {})
        }
      }).catch(() => {})
    }, 2000)
    return () => { alive = false; clearInterval(timer) }
  }, [evalRun])

  async function createEvaluationDataset() {
    if (evalDatasetBusy) return
    setEvalDatasetBusy(true); setEvalDatasetError('')
    try {
      if (!evalDatasetName.trim()) throw new Error(t('workspaceSettings.evalDatasetNameRequired'))
      let items: unknown
      try { items = JSON.parse(evalDatasetItemsText) } catch { throw new Error(t('workspaceSettings.evalDatasetInvalidJson')) }
      const result = await evaluationDatasetApi.create('retrieval', evalDatasetName.trim(), items as never)
      setEvalDatasets((current) => [result.dataset, ...current])
      setEvalDatasetUid(result.dataset.uid)
      setEvalCreatingDataset(false); setEvalDatasetName(''); setEvalDatasetItemsText('')
    } catch (datasetError) {
      setEvalDatasetError(errorMessage(datasetError, t('workspaceSettings.evalDatasetSaveFailed')))
    } finally { setEvalDatasetBusy(false) }
  }

  async function runEvaluation() {
    if (!evalDraftRevision || !evalDatasetUid || evalBusy) return
    setEvalBusy(true); setEvalError(''); setEvalRun(null)
    try {
      const started = await evaluationRunApi.startRetrieval(evalDraftRevision, evalDatasetUid)
      setEvalRun(started.run)
    } catch (runError) {
      setEvalError(errorMessage(runError, t('workspaceSettings.evalRunFailed')))
    } finally { setEvalBusy(false) }
  }

  const evalDraftRevision = profile?.draft?.revision

  return <section className="panel settings-panel quality-settings">
    <div className="panel-title quality-heading"><div><span className="eyebrow">{t('workspaceSettings.eyebrow')}</span><h2>{t('workspaceSettings.title')}</h2><p>{t('workspaceSettings.description')}</p></div>{policy && <span className="quality-runtime"><i className={`status-dot ${policy.rag.available ? '' : 'warn'}`} />{policyLoading ? t('state.loadingTitle') : t('workspaceSettings.runtimeManaged')}</span>}</div>
    {(policyFailed || error) && <div className="settings-error" role="alert">{error || t('workspaceSettings.loadFailed')}</div>}
    {notice && <div className="quality-notice" role="status"><Icon name="check" size={14} />{notice}</div>}

    <nav className="quality-tabs" aria-label={t('workspaceSettings.sections')}>{(['general', 'parameters', 'evaluation'] as SettingsTab[]).map((key) => <button key={key} type="button" className={tab === key ? 'active' : ''} onClick={() => selectTab(key)}>{t(`workspaceSettings.tab.${key}` as TranslationKey)}</button>)}</nav>

    {tab === 'general' ? <GeneralSettingsPanel />
      : loading ? <div className="quality-loading"><Icon name="refresh" /><span>{t('state.loadingTitle')}</span></div>
      : tab === 'evaluation' ? <div className="quality-evaluation">
        <div className="quality-section-head"><div><h3>{t('workspaceSettings.evaluationTitle')}</h3><p>{t('workspaceSettings.evaluationDescription')}</p></div></div>
        <div className="quality-flow"><span><b>1</b>{t('workspaceSettings.flowDataset')}</span><i /><span><b>2</b>{t('workspaceSettings.flowDraft')}</span><i /><span><b>3</b>{t('workspaceSettings.flowCompare')}</span><i /><span><b>4</b>{t('workspaceSettings.flowApply')}</span></div>
        <p className="quality-axis-note-line">{t('workspaceSettings.evalRetrievalOnlyNote')}</p>

        {!evalDraftRevision ? <div className="quality-callout">
          <Icon name="sparkles" />
          <div><strong>{t('workspaceSettings.evalNeedsDraftTitle')}</strong><p>{t('workspaceSettings.evalNeedsDraftDescription')}</p></div>
          <button type="button" className="secondary-button" onClick={() => selectTab('parameters')}>{t('workspaceSettings.openDraft')}</button>
        </div> : <div className="quality-eval-config">
          <div className="quality-eval-dataset-row">
            <label><span>{t('workspaceSettings.evalDatasetLabel')}</span>
              <select value={evalDatasetUid} onChange={(event) => setEvalDatasetUid(event.target.value)}>
                <option value="">{t('workspaceSettings.evalDatasetPlaceholder')}</option>
                {evalDatasets.map((dataset) => <option key={dataset.uid} value={dataset.uid}>{dataset.name} ({dataset.item_count})</option>)}
              </select>
            </label>
            <button type="button" className="text-button" onClick={() => setEvalCreatingDataset((value) => !value)}>{t('workspaceSettings.evalNewDataset')}</button>
          </div>

          {evalCreatingDataset && <div className="quality-eval-dataset-form">
            <label><span>{t('workspaceSettings.evalDatasetName')}</span><input value={evalDatasetName} maxLength={200} onChange={(event) => setEvalDatasetName(event.target.value)} /></label>
            <label><span>{t('workspaceSettings.evalDatasetItems')}</span><textarea value={evalDatasetItemsText} placeholder={t('workspaceSettings.evalDatasetItemsPlaceholder')} onChange={(event) => setEvalDatasetItemsText(event.target.value)} /></label>
            <small>{t('workspaceSettings.evalDatasetItemsHelp')}</small>
            {evalDatasetError && <p className="dialog-error" role="alert">{evalDatasetError}</p>}
            <div className="quality-eval-dataset-actions"><button type="button" className="secondary-button" disabled={evalDatasetBusy} onClick={() => void createEvaluationDataset()}>{evalDatasetBusy ? t('workspaceSettings.saving') : t('workspaceSettings.evalDatasetSave')}</button></div>
          </div>}

          <button type="button" className="primary-button" disabled={!evalDatasetUid || evalBusy} onClick={() => void runEvaluation()}>{evalBusy ? t('workspaceSettings.evalRunning') : t('workspaceSettings.evalRun')}</button>
          {evalError && <p className="dialog-error" role="alert">{evalError}</p>}
        </div>}

        {evalRun && <div className="quality-eval-result">
          <span className={`quality-status ${evalRun.status === 'succeeded' ? 'verified' : evalRun.status === 'failed' ? 'warning' : 'neutral'}`}>{t(`workspaceSettings.evalStatus.${evalRun.status}` as TranslationKey)}</span>
          {evalRun.status === 'succeeded' && <div className="quality-eval-metrics">
            <div><strong>{evalRun.metrics.hit_rate_at_1}</strong><small>{t('workspaceSettings.evalMetricHitAt1')}</small></div>
            <div><strong>{evalRun.metrics.hit_rate_at_k}</strong><small>{t('workspaceSettings.evalMetricHitAtK')}</small></div>
            <div><strong>{evalRun.metrics.mrr_at_k}</strong><small>{t('workspaceSettings.evalMetricMrr')}</small></div>
            <div><strong>{evalRun.metrics.queries}</strong><small>{t('workspaceSettings.evalMetricQueries')}</small></div>
            <div><strong>{evalRun.metrics.chunk_count}</strong><small>{t('workspaceSettings.evalMetricChunks')}</small></div>
          </div>}
          {evalRun.status === 'succeeded' && <p className="quality-eval-verified-note">{t('workspaceSettings.evalVerifiedNotice')}</p>}
          {evalRun.status === 'succeeded' && <button type="button" className="secondary-button" onClick={() => selectTab('parameters')}>{t('workspaceSettings.evalGoApply')}</button>}
          {evalRun.status === 'failed' && <p className="dialog-error" role="alert">{evalRun.error_message}</p>}
        </div>}

        <div className="quality-section-head versions"><div><h3>{t('workspaceSettings.historyTitle')}</h3><p>{t('workspaceSettings.historyDescription')}</p></div></div>
        <div className="quality-version-list">{versions.length ? versions.map((version) => <div key={version.uid}><span><b>v{version.version}</b>{version.changed_axes.map((axis) => <em key={axis}>{t(`workspaceSettings.axisLabel.${axis}` as TranslationKey)}</em>)}</span><span><strong>{version.note || t('workspaceSettings.noNote')}</strong><small>{formatDate(version.applied_at, locale)}</small></span><span className={`quality-status ${validationTone(version.validation.state)}`}>{t(`workspaceSettings.validation.${version.validation.state}` as TranslationKey)}</span></div>) : <p>{t('workspaceSettings.noHistory')}</p>}</div>
      </div>
      : profile && form ? <div className="quality-profile-editor">
        <div className="quality-revision-grid"><ProfileSummary revision={profile.active} kind="active" />{profile.draft ? <ProfileSummary revision={profile.draft} kind="draft" /> : <div className="quality-revision empty"><strong>{t('workspaceSettings.noDraft')}</strong><small>{t('workspaceSettings.noDraftDescription')}</small></div>}</div>
        {!profile.permissions.can_edit && <div className="quality-readonly"><Icon name="users" /><span><strong>{t('workspaceSettings.readonlyTitle')}</strong><small>{t('workspaceSettings.readonlyDescription')}</small></span></div>}

        <div className="quality-notice merge-note" role="note"><Icon name="layers" size={14} /><span><strong>{t('workspaceSettings.mergeNoticeTitle')}</strong><small>{t('workspaceSettings.mergeNoticeDescription')}</small></span></div>

        <ParamGroup titleKey="workspaceSettings.group.retrievalWeights" helpKey="workspaceSettings.groupHelp.retrievalWeights" axis="retrieval">
          <div className="quality-field">
            <div className="quality-field-head"><span><strong>{t('workspaceSettings.field.dense_weight')} / {t('workspaceSettings.field.sparse_weight')}</strong><small>{t('workspaceSettings.fieldHelp.dense_weight')}</small></span></div>
            <DualWeightSlider dense={Number(form.retrieval.dense_weight)} disabled={editDisabled} onChange={changeDenseWeight} />
          </div>
          {RETRIEVAL_WEIGHT_FIELDS.map((field) => <FieldRow key={String(field)} name={String(field)} schema={retrievalSchema[String(field)]} value={form.retrieval[field]} activeValue={profile.active.retrieval.effective[field]} defaultValue={profile.defaults.retrieval[field]} disabled={editDisabled} onChange={(value) => changeField('retrieval', String(field), value)} onRestore={() => changeField('retrieval', String(field), profile.active.retrieval.effective[field])} onReset={() => inheritField('retrieval', String(field), profile.defaults.retrieval[field])} />)}
        </ParamGroup>

        <ParamGroup titleKey="workspaceSettings.group.documentPooling" helpKey="workspaceSettings.groupHelp.documentPooling" axis="retrieval">
          {POOLING_FIELDS.map((field) => <FieldRow key={String(field)} name={String(field)} schema={retrievalSchema[String(field)]} value={form.retrieval[field]} activeValue={profile.active.retrieval.effective[field]} defaultValue={profile.defaults.retrieval[field]} disabled={editDisabled} onChange={(value) => changeField('retrieval', String(field), value)} onRestore={() => changeField('retrieval', String(field), profile.active.retrieval.effective[field])} onReset={() => inheritField('retrieval', String(field), profile.defaults.retrieval[field])} />)}
        </ParamGroup>

        <ParamGroup titleKey="workspaceSettings.group.resultExposure" helpKey="workspaceSettings.groupHelp.resultExposure" axis="retrieval">
          {EXPOSURE_FIELDS.map((field) => <FieldRow key={String(field)} name={String(field)} schema={retrievalSchema[String(field)]} value={form.retrieval[field]} activeValue={profile.active.retrieval.effective[field]} defaultValue={profile.defaults.retrieval[field]} disabled={editDisabled} onChange={(value) => changeField('retrieval', String(field), value)} onRestore={() => changeField('retrieval', String(field), profile.active.retrieval.effective[field])} onReset={() => inheritField('retrieval', String(field), profile.defaults.retrieval[field])} />)}
        </ParamGroup>

        {Number(form.retrieval.dense_weight) + Number(form.retrieval.sparse_weight) !== 1 && <p className="quality-inline-warning">{t('workspaceSettings.weightWarning')}</p>}

        <ParamGroup titleKey="workspaceSettings.group.generation" helpKey="workspaceSettings.groupHelp.generation" axis="generation">
          {GENERATION_FIELDS.map((field) => <FieldRow key={String(field)} name={String(field)} schema={generationSchema[String(field)]} value={form.generation[field]} activeValue={profile.active.generation.effective[field]} defaultValue={profile.defaults.generation[field]} disabled={editDisabled} onChange={(value) => changeField('generation', String(field), value)} onRestore={() => changeField('generation', String(field), profile.active.generation.effective[field])} onReset={() => inheritField('generation', String(field), profile.defaults.generation[field])} />)}
        </ParamGroup>

        <section className="quality-param-group quality-prompt">
          <div className="quality-section-head"><div><h3>{t('workspaceSettings.group.promptPolicy')}</h3><p>{t('workspaceSettings.groupHelp.promptPolicy')}</p></div></div>
          <div className="prompt-route-tabs">{(['document_rag', 'no_retrieval'] as PromptRoute[]).map((route) => <button type="button" className={promptRoute === route ? 'active' : ''} onClick={() => { setPromptRoute(route); setPromptPreview('') }} key={route}>{t(`workspaceSettings.promptRoute.${route}` as TranslationKey)}</button>)}</div>
          <div className="fixed-contract-note"><Icon name="layers" size={16} /><span><strong>{t('workspaceSettings.fixedContract')}</strong><small>{profile.fixed_contract || t('workspaceSettings.fixedContractDescription')}</small></span></div>
          <div className="prompt-mode-row"><label><input type="radio" name="prompt-mode" value="inherit" checked={form.prompt_policy[promptRoute].mode === 'inherit'} disabled={editDisabled} onChange={() => setForm({ ...form, prompt_policy: { ...form.prompt_policy, [promptRoute]: { mode: 'inherit', instruction: null } } })} /><span><strong>{t('workspaceSettings.promptInherit')}</strong><small>{t('workspaceSettings.promptInheritDescription')}</small></span></label><label><input type="radio" name="prompt-mode" value="replace" checked={form.prompt_policy[promptRoute].mode === 'replace'} disabled={editDisabled} onChange={() => setForm({ ...form, prompt_policy: { ...form.prompt_policy, [promptRoute]: { mode: 'replace', instruction: form.prompt_policy[promptRoute].instruction || '' } } })} /><span><strong>{t('workspaceSettings.promptReplace')}</strong><small>{t('workspaceSettings.promptReplaceDescription')}</small></span></label></div>
          <div className="prompt-import-row"><label className={`secondary-button ${editDisabled ? 'disabled' : ''}`}><Icon name="upload" size={14} />{t('workspaceSettings.promptImport')}<input type="file" accept=".txt,.md,text/plain,text/markdown" disabled={editDisabled} onChange={(event) => void importPromptFile(event)} /></label><small>{t('workspaceSettings.promptImportDescription')}</small></div>
          {form.prompt_policy[promptRoute].mode === 'replace' && <label className="prompt-editor"><span><strong>{t('workspaceSettings.workspaceInstruction')}</strong><small>{(form.prompt_policy[promptRoute].instruction || '').length} / 12,000</small></span><textarea disabled={editDisabled} maxLength={12000} value={form.prompt_policy[promptRoute].instruction || ''} placeholder={t('workspaceSettings.promptPlaceholder')} onChange={(event) => setForm({ ...form, prompt_policy: { ...form.prompt_policy, [promptRoute]: { mode: 'replace', instruction: event.target.value } } })} /></label>}
          {profile.provider_disclosure && <p className="provider-disclosure">{profile.provider_disclosure}</p>}
          <div className="prompt-preview-actions"><button type="button" className="secondary-button" disabled={busy || !profile.draft} onClick={() => void previewPrompt()}>{t('workspaceSettings.preview')}</button><small>{t('workspaceSettings.previewDescription')}</small></div>
          {promptPreview && <pre className="prompt-preview">{promptPreview}</pre>}
        </section>

        <ParamGroup titleKey="workspaceSettings.group.compression" helpKey="workspaceSettings.groupHelp.compression" axis="retrieval">
          <FieldRow name="contextual_compression" schema={retrievalSchema.contextual_compression} value={form.retrieval.contextual_compression} activeValue={profile.active.retrieval.effective.contextual_compression} defaultValue={profile.defaults.retrieval.contextual_compression} disabled={editDisabled} onChange={(value) => changeField('retrieval', 'contextual_compression', value)} onRestore={() => changeField('retrieval', 'contextual_compression', profile.active.retrieval.effective.contextual_compression)} onReset={() => inheritField('retrieval', 'contextual_compression', profile.defaults.retrieval.contextual_compression)} />
        </ParamGroup>

        <DraftActions profile={profile} busy={busy} note={note} setNote={setNote} allowUnverified={allowUnverified} setAllowUnverified={setAllowUnverified} onSave={() => void saveDraft()} onDiscard={() => void discardDraft()} onApply={() => void applyDraft()} />
      </div>
        : <div className="quality-empty"><Icon name="settings" /><strong>{t('workspaceSettings.apiUnavailable')}</strong><p>{t('workspaceSettings.apiUnavailableDescription')}</p></div>}
  </section>
}
