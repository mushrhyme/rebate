import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { api, ApiError, type User } from '../api/client'

interface AuthContextValue {
  user: User | null
  loading: boolean
  mustChangePassword: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)
  const [mustChangePassword, setMustChangePassword] = useState(false)

  useEffect(() => {
    const sid = localStorage.getItem('session_id')
    if (!sid) { setLoading(false); return }
    let cancelled = false

    // 분석 중 백엔드가 느릴 때 새 탭의 me()가 타임아웃·일시 오류로 실패하면
    // 예전 코드는 session_id를 지워 모든 탭이 로그아웃됐다 ("로그인 튕김"의 원인).
    // → 401일 때만 세션이 제거되고(client.ts), 일시 오류는 백오프 재시도한다.
    const RETRY_DELAYS = [0, 2000, 5000]
    const bootstrap = async () => {
      for (const delay of RETRY_DELAYS) {
        if (delay) await new Promise(r => setTimeout(r, delay))
        if (cancelled) return
        try {
          const u = await api.me()
          if (cancelled) return
          setUser(u)
          setMustChangePassword(u.force_password_change)
          return
        } catch (e) {
          if (e instanceof ApiError && e.status === 401) return // client.ts가 세션 제거·리다이렉트 처리
          // 타임아웃·네트워크·5xx → 세션은 보존하고 재시도
        }
      }
      // 재시도 소진: 세션을 지우지 않는다 — 다른 탭의 로그인 상태를 파괴하지 않기 위함.
      // 이 탭은 미로그인 상태로 표시되며, 새로고침으로 복구 가능.
    }
    bootstrap().finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  // 탭 간 세션 동기화 — 다른 탭의 로그인/로그아웃을 이 탭에도 반영
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== 'session_id') return
      if (e.newValue === null) {
        setUser(null)
        setMustChangePassword(false)
      } else if (e.newValue !== e.oldValue) {
        api.me()
          .then(u => { setUser(u); setMustChangePassword(u.force_password_change) })
          .catch(() => {})
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  // 슬라이딩 세션 갱신 — 로그인 상태에서 주기적으로(+탭 포커스/진입 시) 토큰을 재발급받아
  // 작업 도중 고정 만료로 세션이 끊기는 것을 막는다. 앱을 열고 활동하는 한 세션이 유지된다.
  useEffect(() => {
    if (!user) return
    const REFRESH_MS = 15 * 60 * 1000  // 15분 — 만료(jwt_expire_hours, 기본 24h)보다 충분히 짧게
    const doRefresh = async () => {
      if (!localStorage.getItem('session_id')) return
      try {
        const res = await api.refresh()
        if (res?.session_id) localStorage.setItem('session_id', res.session_id)
      } catch {
        // 401이면 client.ts가 세션 제거·로그인 이동을 처리한다. 일시 오류는 다음 주기에 재시도.
      }
    }
    doRefresh()  // 진입 즉시 한 번 — 만료가 임박한 토큰도 바로 연장
    const id = window.setInterval(doRefresh, REFRESH_MS)
    const onFocus = () => doRefresh()
    window.addEventListener('focus', onFocus)
    return () => { window.clearInterval(id); window.removeEventListener('focus', onFocus) }
  }, [user])

  async function login(username: string, password: string) {
    const res = await api.login(username, password)
    if (!res.user) {
      throw new Error('서버 응답 오류가 발생했습니다. 네트워크 연결을 확인해 주세요.')
    }
    localStorage.setItem('session_id', res.session_id)
    setUser(res.user)
    setMustChangePassword(res.user.force_password_change)
  }

  async function logout() {
    await api.logout().catch(() => {})
    localStorage.removeItem('session_id')
    setUser(null)
    setMustChangePassword(false)
  }

  async function changePassword(currentPassword: string, newPassword: string) {
    await api.changePassword(currentPassword, newPassword)
    setMustChangePassword(false)
    setUser(prev => prev ? { ...prev, force_password_change: false } : null)
  }

  return (
    <AuthContext.Provider value={{ user, loading, mustChangePassword, login, logout, changePassword }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
