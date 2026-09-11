import type { SessionBootstrapResponse } from '../../api/types'
import { Icon } from '../../components/Icon'
import { useI18n } from '../../i18n'
import { TokenSettings } from './TokenSettings'

interface AccountSettingsFeatureProps {
  session: SessionBootstrapResponse | null
  loading: boolean
  failed: boolean
}

export function AccountSettingsFeature({ session, loading, failed }: AccountSettingsFeatureProps) {
  const { locale, setLocale, t } = useI18n()

  return <section className="panel settings-panel">
    <div className="panel-title"><div><span className="eyebrow">{t('accountSettings.eyebrow')}</span><h2>{t('accountSettings.title')}</h2></div><span className="saved-state"><Icon name="check" size={14}/>{t('settings.localSaved')}</span></div>
    {failed && <div className="settings-error">{t('accountSettings.loadFailed')}</div>}
    <div className="setting-row"><div><strong>{t('settings.account')}</strong><p>{t('settings.accountDescription')}</p></div><div className="setting-value"><strong>{loading ? t('state.loadingTitle') : session?.user?.display_name || t('user.guest')}</strong><small>{session?.user?.email || '—'}</small></div></div>
    <div className="setting-row"><div><strong>{t('settings.language')}</strong><p>{t('settings.languageDescription')}</p></div><div className="language-options" role="group" aria-label={t('settings.language')}><button className={locale === 'ko' ? 'active' : ''} aria-pressed={locale === 'ko'} onClick={() => setLocale('ko')}>{t('settings.korean')}</button><button className={locale === 'en' ? 'active' : ''} aria-pressed={locale === 'en'} onClick={() => setLocale('en')}>{t('settings.english')}</button></div></div>
    <TokenSettings enabled={Boolean(session?.auth.authenticated && session.user?.email_verified)} />
  </section>
}
