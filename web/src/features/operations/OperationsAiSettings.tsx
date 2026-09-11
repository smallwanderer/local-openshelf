import type { ServerPolicySummary } from '../../api/models'
import { AsyncState } from '../../components/AsyncState'
import { Icon } from '../../components/Icon'
import { useI18n } from '../../i18n'

interface OperationsAiSettingsProps {
  policy: ServerPolicySummary | null
  loading: boolean
  failed: boolean
  authorized: boolean
  authResolved: boolean
}

function valueOrDash(value: string | number | null | undefined): string {
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

export function OperationsAiSettings({ policy, loading, failed, authorized, authResolved }: OperationsAiSettingsProps) {
  const { t } = useI18n()
  if (!authResolved) return <section className="panel operations-panel"><AsyncState kind="loading" /></section>
  if (!authorized) return <section className="panel operations-panel"><div className="async-state unauthorized" role="status"><span><Icon name="x" /></span><strong>{t('operations.accessTitle')}</strong><p>{t('operations.accessDescription')}</p></div></section>

  const runtimeStatus = policy?.rag.available
    ? t('runtime.ready')
    : policy?.rag.configured
      ? t('runtime.unavailable')
      : t('runtime.notConfigured')

  return <section className="panel settings-panel">
    <div className="panel-title"><div><span className="eyebrow">{t('operations.navAi')}</span><h2>{t('operations.aiSettingsTitle')}</h2></div></div>
    {failed && <div className="settings-error">{t('operations.aiSettingsLoadFailed')}</div>}
    <div className="setting-row"><div><strong>{t('operations.aiPolicy')}</strong><p>{t('operations.aiPolicyDescription')}</p></div><div className="setting-value"><span className="policy-badge">{valueOrDash(policy?.rag.priorityPreset)}</span><small>{t('operations.serverManaged')}</small></div></div>
    <div className="setting-row"><div><strong>{t('operations.ragRuntime')}</strong><p>{t('operations.ragRuntimeDescription')}</p></div><div className="setting-value"><strong>{loading ? t('state.loadingTitle') : runtimeStatus}</strong><small>{policy?.rag.model ? `${policy.rag.model} · ${policy.rag.runtime}` : '—'}</small></div></div>
    <div className="setting-row"><div><strong>{t('operations.embeddingRuntime')}</strong><p>{t('operations.embeddingRuntimeDescription')}</p></div><div className="setting-value"><strong>{valueOrDash(policy?.embedding.model)}</strong><small>{policy ? `${policy.embedding.provider} · ${policy.embedding.dimension}D · ${policy.embedding.distanceStrategy}` : '—'}</small></div></div>
    <div className="settings-note"><Icon name="sparkles"/><p><strong>{t('operations.operatorTitle')}</strong><br/>{t('operations.operatorDescription')}</p></div>
  </section>
}
