import { useEffect, useRef, useState } from 'react'
import type { ServerPolicySummary } from './api/models'
import { systemApi } from './api/system'
import type { SessionBootstrapResponse } from './api/types'
import { AsyncState } from './components/AsyncState'
import { Icon } from './components/Icon'
import { ChatFeature } from './features/chat/ChatFeature'
import { DocumentsFeature } from './features/documents/DocumentsFeature'
import { HomeFeature } from './features/home/HomeFeature'
import { OperationsAiSettings } from './features/operations/OperationsAiSettings'
import { OperationsFeature } from './features/operations/OperationsFeature'
import { SearchFeature } from './features/search/SearchFeature'
import { AccountSettingsFeature } from './features/settings/AccountSettingsFeature'
import { InviteInbox } from './features/workspace/InviteInbox'
import { WorkspaceList } from './features/workspace/WorkspaceList'
import { WorkspaceSettingsFeature } from './features/workspace/WorkspaceSettingsFeature'
import { useI18n } from './i18n'

type Section = 'home' | 'workspace' | 'operations'
type KnowledgeTab = 'documents' | 'retrieval' | 'chat' | 'accountSettings' | 'workspaceSettings'
type OperationsTab = 'summary' | 'ai' | 'traces'

interface RouteState {
  section: Section
  tab: KnowledgeTab
  operationsTab: OperationsTab
  conversationUid: string | null
}

function routeFromPath(pathname: string): RouteState {
  const path = pathname.replace(/\/+$/, '') || '/'
  const conversationMatch = path.match(/^\/workspace\/chat\/([0-9a-f-]+)$/i)
  if (conversationMatch) return { section: 'workspace', tab: 'chat', operationsTab: 'summary', conversationUid: conversationMatch[1] }
  if (path === '/workspace/home') return { section: 'home', tab: 'documents', operationsTab: 'summary', conversationUid: null }
  if (path === '/workspace/search') return { section: 'workspace', tab: 'retrieval', operationsTab: 'summary', conversationUid: null }
  if (path === '/workspace/chat') return { section: 'workspace', tab: 'chat', operationsTab: 'summary', conversationUid: null }
  if (path === '/workspace/settings/workspace') return { section: 'workspace', tab: 'workspaceSettings', operationsTab: 'summary', conversationUid: null }
  if (path === '/workspace/settings') return { section: 'workspace', tab: 'accountSettings', operationsTab: 'summary', conversationUid: null }
  if (path === '/workspace/operations/ai') return { section: 'operations', tab: 'documents', operationsTab: 'ai', conversationUid: null }
  if (path === '/workspace/operations/traces') return { section: 'operations', tab: 'documents', operationsTab: 'traces', conversationUid: null }
  if (path === '/workspace/operations') return { section: 'operations', tab: 'documents', operationsTab: 'summary', conversationUid: null }
  return { section: 'workspace', tab: 'documents', operationsTab: 'summary', conversationUid: null }
}

function pathForRoute(section: Section, tab: KnowledgeTab, operationsTab: OperationsTab, documentsQuery: string, conversationUid: string | null): string {
  if (section === 'home') return '/workspace/home/'
  if (section === 'operations') {
    if (operationsTab === 'ai') return '/workspace/operations/ai/'
    if (operationsTab === 'traces') return '/workspace/operations/traces/'
    return '/workspace/operations/'
  }
  if (tab === 'retrieval') return '/workspace/search/'
  if (tab === 'chat') return conversationUid ? `/workspace/chat/${conversationUid}/` : '/workspace/chat/'
  if (tab === 'accountSettings') return '/workspace/settings/'
  if (tab === 'workspaceSettings') return '/workspace/settings/workspace/'
  return `/workspace/documents/${documentsQuery}`
}

function OperationsPlaceholder({ authorized, authResolved }: { authorized: boolean; authResolved: boolean }) {
  const { t } = useI18n()
  if (!authResolved) return <section className="panel operations-panel"><AsyncState kind="loading" /></section>
  if (!authorized) return <section className="panel operations-panel"><div className="async-state unauthorized" role="status"><span><Icon name="x" /></span><strong>{t('operations.accessTitle')}</strong><p>{t('operations.accessDescription')}</p></div></section>
  return <section className="panel operations-panel"><div className="async-state empty" role="status"><span><Icon name="search" /></span><strong>{t('operations.tracePlaceholderTitle')}</strong><p>{t('operations.tracePlaceholderDescription')}</p><span className="future-badge">{t('future.badge')}</span></div></section>
}

function Brand({ onClick }: { onClick: () => void }) {
  const { t } = useI18n()
  return <button type="button" className="brand" onClick={onClick} aria-label={t('nav.home')}><img className="brand-mark" src={`${import.meta.env.BASE_URL}icons/dotori-mark.svg`} alt="" /><span>Dotori</span><b>{t('brand.preview')}</b></button>
}

function App() {
  const { t } = useI18n()
  const initialRoute = routeFromPath(window.location.pathname)
  const [section, setSection] = useState<Section>(initialRoute.section)
  const [tab, setTab] = useState<KnowledgeTab>(initialRoute.tab)
  const [operationsTab, setOperationsTab] = useState<OperationsTab>(initialRoute.operationsTab)
  const [conversationUid, setConversationUid] = useState<string | null>(initialRoute.conversationUid)
  const [pendingOpenUid, setPendingOpenUid] = useState<string | null>(null)
  const [mobileNav, setMobileNav] = useState(false)
  const [session, setSession] = useState<SessionBootstrapResponse | null>(null)
  const [serverPolicy, setServerPolicy] = useState<ServerPolicySummary | null>(null)
  const [systemLoading, setSystemLoading] = useState(true)
  const [systemError, setSystemError] = useState(false)
  const documentsQueryRef = useRef(
    initialRoute.section === 'workspace' && initialRoute.tab === 'documents' ? window.location.search : '',
  )

  useEffect(() => {
    let active = true
    systemApi.getSession()
      .then(async (currentSession) => {
        if (!active) return
        setSession(currentSession)
        if (!currentSession.auth.authenticated) return
        const policy = await systemApi.getServerPolicy()
        if (active) setServerPolicy(policy)
      })
      .catch(() => { if (active) setSystemError(true) })
      .finally(() => { if (active) setSystemLoading(false) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    const restoreRoute = () => {
      const restored = routeFromPath(window.location.pathname)
      setSection(restored.section)
      setTab(restored.tab)
      setOperationsTab(restored.operationsTab)
      setConversationUid(restored.conversationUid)
      setMobileNav(false)
    }
    window.addEventListener('popstate', restoreRoute)
    return () => window.removeEventListener('popstate', restoreRoute)
  }, [])

  function navigate(nextSection: Section, nextTab: KnowledgeTab = tab, nextOperationsTab: OperationsTab = operationsTab, nextConversationUid: string | null = nextTab === 'chat' ? conversationUid : null) {
    const leavingDocuments = section === 'workspace' && tab === 'documents' && !(nextSection === 'workspace' && nextTab === 'documents')
    if (leavingDocuments) documentsQueryRef.current = window.location.search
    setSection(nextSection)
    setTab(nextTab)
    setOperationsTab(nextOperationsTab)
    setConversationUid(nextConversationUid)
    setMobileNav(false)
    const nextPath = pathForRoute(nextSection, nextTab, nextOperationsTab, documentsQueryRef.current, nextConversationUid)
    const currentPath = window.location.pathname + window.location.search
    if (currentPath !== nextPath) window.history.pushState({}, '', nextPath)
  }

  function openAccountSettings() {
    navigate('workspace', 'accountSettings')
  }

  function openDocumentDetail(uid: string) {
    setPendingOpenUid(uid)
    navigate('workspace', 'documents')
  }

  const displayName = session?.user?.display_name || session?.user?.email.split('@')[0] || t('user.guest')
  const initials = displayName.trim().split(/\s+/).map((part) => part[0]).join('').slice(0, 2).toUpperCase() || 'D'
  const runtimeState = systemLoading
    ? t('runtime.loading')
    : serverPolicy?.rag.available
      ? t('runtime.ready')
      : serverPolicy?.rag.configured
        ? t('runtime.unavailable')
        : t('runtime.notConfigured')
  const runtimeDetail = serverPolicy?.rag.model || (systemError ? t('runtime.loadFailed') : t('runtime.noModel'))
  const runtimeMeta = serverPolicy?.rag.runtime
    ? t('runtime.meta', { runtime: serverPolicy.rag.runtime, concurrency: serverPolicy.rag.servingConcurrency || 1 })
    : t('runtime.serverManaged')

  return <div className="app-frame">
    <aside className={`sidebar ${mobileNav ? 'is-open' : ''}`}>
      <div className="sidebar-top"><Brand onClick={() => navigate('home', 'documents')} /><button className="mobile-close" onClick={() => setMobileNav(false)}><Icon name="x" /></button></div>
      <nav className="primary-nav" aria-label={t('nav.main')}>
        <button className={section === 'home' ? 'active' : ''} onClick={() => navigate('home', 'documents')}><Icon name="home" />{t('nav.home')}</button>
        <button className={section === 'workspace' ? 'active' : ''} onClick={() => navigate('workspace', tab)}><Icon name="database" />{t('nav.workspace')}</button>
        {session?.user?.is_staff && <button className={section === 'operations' ? 'active' : ''} onClick={() => navigate('operations', 'documents', 'summary')}><Icon name="layers" />{t('nav.operations')}</button>}
      </nav>
      <div className="sidebar-caption">{t('nav.workspace')}</div>
      <WorkspaceList
        enabled={Boolean(session?.auth.authenticated && session.user?.email_verified)}
        onNavigateHome={() => navigate('home', 'documents')}
      />
      <div className="sidebar-spacer" />
      <div className="runtime-card"><div className="runtime-head"><span className={`status-dot ${serverPolicy?.rag.available ? '' : 'warn'}`} />{runtimeState}</div><strong>{runtimeDetail}</strong><small>{runtimeMeta}</small></div>
      <div className="user-row"><span className="user-avatar">{initials}</span><span><strong>{displayName}</strong><small>{session?.user?.is_staff ? t('user.operator') : session?.auth.authenticated ? t('user.member') : t('user.guest')}</small></span></div>
    </aside>

    {mobileNav && <button className="nav-scrim" onClick={() => setMobileNav(false)} aria-label={t('action.closeMenu')} />}

    <main className="main-shell">
      <header className="topbar">
        <button className="mobile-menu" onClick={() => setMobileNav(true)}><Icon name="menu" /></button>
        <div className="topbar-title"><span className="topbar-mark"><Icon name="database" size={16}/></span><strong>Dotori</strong></div>
        {section === 'workspace' && <nav className="workspace-tabs" aria-label={t('nav.workMenu')}>
          {([['documents', t('nav.documents')], ['retrieval', t('nav.searchTest')], ['chat', t('nav.ragChat')], ['workspaceSettings', t('nav.workspaceSettings')]] as [KnowledgeTab, string][]).map(([key, label]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => navigate('workspace', key)}>{label}</button>)}
        </nav>}
        {section === 'operations' && session?.user?.is_staff && <nav className="workspace-tabs" aria-label={t('operations.navLabel')}>
          {([['summary', t('operations.navSummary')], ['ai', t('operations.navAi')], ['traces', t('operations.navTraces')]] as [OperationsTab, string][]).map(([key, label]) => <button key={key} className={operationsTab === key ? 'active' : ''} onClick={() => navigate('operations', 'documents', key)}>{label}</button>)}
        </nav>}
        <div className="top-actions"><InviteInbox enabled={Boolean(session?.auth.authenticated && session.user?.email_verified)} /><button className={`icon-button ${section === 'workspace' && tab === 'accountSettings' ? 'active' : ''}`} title={t('action.accountSettings')} aria-label={t('action.accountSettings')} onClick={openAccountSettings}><Icon name="settings" /></button><button className="help-button" title={t('future.plannedHelp')} aria-label={t('future.plannedHelp')} disabled>?</button></div>
      </header>

      {section === 'home' ? <HomeFeature onOpen={() => navigate('workspace', 'documents')} userName={displayName} tenantEnabled={Boolean(session?.auth.authenticated && session.user?.email_verified)} /> : section === 'operations' ? <div className="workspace-page">{operationsTab === 'summary' ? <OperationsFeature authorized={Boolean(session?.user?.is_staff)} authResolved={!systemLoading} /> : operationsTab === 'ai' ? <OperationsAiSettings policy={serverPolicy} loading={systemLoading} failed={systemError} authorized={Boolean(session?.user?.is_staff)} authResolved={!systemLoading} /> : <OperationsPlaceholder authorized={Boolean(session?.user?.is_staff)} authResolved={!systemLoading} />}</div> : <div className="workspace-page">
        {tab === 'documents' && <DocumentsFeature onSearch={() => navigate('workspace', 'retrieval')} onAsk={() => navigate('workspace', 'chat')} pendingOpenUid={pendingOpenUid} onPendingOpenHandled={() => setPendingOpenUid(null)} />}
        {tab === 'retrieval' && <SearchFeature onOpenDocument={openDocumentDetail} />}
        {tab === 'chat' && <ChatFeature conversationUid={conversationUid} onConversationChange={(uid) => navigate('workspace', 'chat', 'summary', uid)} onOpenDocument={openDocumentDetail} />}
        {tab === 'accountSettings' && <AccountSettingsFeature session={session} loading={systemLoading} failed={systemError} />}
        {tab === 'workspaceSettings' && <WorkspaceSettingsFeature policy={serverPolicy} loading={systemLoading} failed={systemError} />}
      </div>}
    </main>
  </div>
}

export default App
