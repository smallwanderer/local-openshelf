import { createContext, type ReactNode, useContext, useEffect, useMemo, useState } from 'react'
import en from './locales/en'
import ko from './locales/ko'

export type Locale = 'ko' | 'en'
export type TranslationKey = keyof typeof ko

type TranslationValues = Record<string, string | number>
type I18nContextValue = {
  locale: Locale
  setLocale: (locale: Locale) => void
  t: (key: TranslationKey, values?: TranslationValues) => string
}

const STORAGE_KEY = 'dotori.locale'
const resources = { ko, en }
const I18nContext = createContext<I18nContextValue | null>(null)

function getInitialLocale(): Locale {
  const saved = window.localStorage.getItem(STORAGE_KEY)
  return saved === 'en' || saved === 'ko' ? saved : 'ko'
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocale] = useState<Locale>(getInitialLocale)

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, locale)
    document.documentElement.lang = locale
  }, [locale])

  const value = useMemo<I18nContextValue>(() => ({
    locale,
    setLocale,
    t(key, values = {}) {
      return Object.entries(values).reduce(
        (text, [name, replacement]) => text.replaceAll(`{${name}}`, String(replacement)),
        resources[locale][key],
      )
    },
  }), [locale])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n() {
  const context = useContext(I18nContext)
  if (!context) throw new Error('useI18n must be used within I18nProvider')
  return context
}
