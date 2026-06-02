import { createContext, useContext, useState, useEffect, useCallback, useRef, type ReactNode } from 'react'
import { useAuth } from './AuthContext'

const BASE = (import.meta as any).env?.VITE_API_URL ?? ''

function sessionHeaders(): Record<string, string> {
  const sid = localStorage.getItem('session_id')
  return sid ? { 'X-Session-ID': sid } : {}
}

export interface FormEntry {
  id: string
  name: string
  short_name: string
  issuer: string
  status: string
  tbdCount: number
  lastEditor: string | null
  lastEditedAt: string | null
}

interface FormsContextValue {
  forms: FormEntry[]
  loading: boolean
  reload: () => Promise<void>
  addForm: (data: { shortName: string; memo?: string; netFormula: string; cfKeywords: string }) => Promise<string>
}

const FormsContext = createContext<FormsContextValue | null>(null)

export function FormsProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  const [forms, setForms] = useState<FormEntry[]>([])
  const [loading, setLoading] = useState(true)
  // StrictMode에서 같은 user_id로 effect가 2번 실행되는 것을 막음
  const fetchedForRef = useRef<number | null>(null)

  const reload = useCallback(async () => {
    try {
      const res = await fetch(`${BASE}/api/v3/forms`, { headers: sessionHeaders() })
      if (res.ok) {
        const data: any[] = await res.json()
        setForms(data.map(f => ({
          id: f.form_id,
          name: f.name,
          short_name: f.short_name ?? f.form_id,
          issuer: f.short_name ?? f.form_id,
          status: '운영중',
          tbdCount: f.tbd_count ?? 0,
          lastEditor: f.last_editor ?? null,
          lastEditedAt: f.last_edited_at ?? null,
        })))
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!user) {
      setForms([])
      setLoading(false)
      fetchedForRef.current = null
      return
    }
    // 이미 이 user_id로 fetch했으면 건너뜀 (StrictMode 이중 실행 · 동일 유저 재렌더 방지)
    if (fetchedForRef.current === user.user_id) return
    fetchedForRef.current = user.user_id
    reload()
  }, [user, reload])

  async function addForm(data: { shortName: string; memo?: string; netFormula: string; cfKeywords: string }): Promise<string> {
    const res = await fetch(`${BASE}/api/v3/forms`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...sessionHeaders() },
      body: JSON.stringify({
        short_name: data.shortName,
        memo: data.memo ?? '',
        net_formula: data.netFormula,
        cf_keywords: data.cfKeywords,
      }),
    })
    if (!res.ok) throw new Error('양식 생성 실패')
    const created = await res.json()
    await reload()
    return created.form_id
  }

  return (
    <FormsContext.Provider value={{ forms, loading, reload, addForm }}>
      {children}
    </FormsContext.Provider>
  )
}

export function useForms() {
  const ctx = useContext(FormsContext)
  if (!ctx) throw new Error('useForms must be used within FormsProvider')
  return ctx
}
