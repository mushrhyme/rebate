import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { api, type User } from '../api/client'

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
    api.me()
      .then(u => {
        setUser(u)
        setMustChangePassword(u.force_password_change)
      })
      .catch(() => localStorage.removeItem('session_id'))
      .finally(() => setLoading(false))
  }, [])

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
