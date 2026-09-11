import { useI18n } from '../i18n'
import { Icon } from './Icon'

export type AsyncStateKind = 'loading' | 'empty' | 'error' | 'unauthorized'

export function AsyncState({ kind, onRetry }: { kind: AsyncStateKind; onRetry?: () => void }) {
  const { t } = useI18n()
  const content = {
    loading: [t('state.loadingTitle'), t('state.loadingDescription')],
    empty: [t('state.emptyTitle'), t('state.emptyDescription')],
    error: [t('state.errorTitle'), t('state.errorDescription')],
    unauthorized: [t('state.unauthorizedTitle'), t('state.unauthorizedDescription')],
  }[kind]

  return <div className={`async-state ${kind}`} role={kind === 'error' ? 'alert' : 'status'}>
    <span><Icon name={kind === 'loading' ? 'refresh' : kind === 'error' ? 'x' : 'document'} /></span>
    <strong>{content[0]}</strong>
    <p>{content[1]}</p>
    {onRetry && <button className="secondary-button" onClick={onRetry}>{t('state.retry')}</button>}
  </div>
}
