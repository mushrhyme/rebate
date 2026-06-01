import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import { useAuth } from '../context/AuthContext'

export function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await login(username, password)
      navigate('/', { replace: true })
    } catch (err: any) {
      setError(err.message ?? '로그인 실패')
    } finally {
      setLoading(false)
    }
  }

  const inputStyle: React.CSSProperties = {
    width: '100%', border: '1.5px solid var(--border)', borderRadius: 10,
    padding: '11px 14px', fontSize: 14, outline: 'none',
    background: 'var(--bg)', color: 'var(--text-1)', fontFamily: 'inherit',
    boxSizing: 'border-box',
  }

  return (
    <div style={{
      height: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'var(--bg)',
    }}>
      {/* 배경 그라디언트 */}
      <div style={{
        position: 'fixed', inset: 0, pointerEvents: 'none',
        background: 'radial-gradient(ellipse 60% 50% at 50% 0%, rgba(10,110,110,0.07) 0%, transparent 70%)',
      }} />

      <div style={{
        width: 380, background: 'var(--card)',
        borderRadius: 20, border: '1px solid var(--border)',
        boxShadow: '0 24px 64px rgba(26,21,18,0.12)',
        padding: '40px 36px',
        position: 'relative',
      }}>
        {/* 로고 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 32 }}>
          <div style={{
            width: 40, height: 40, borderRadius: 11,
            background: 'var(--primary)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: 17, fontWeight: 800, color: '#fff',
            fontFamily: 'var(--mono)',
            boxShadow: '0 4px 14px rgba(10,110,110,0.35)',
          }}>R</div>
          <div>
            <p style={{ fontSize: 15, fontWeight: 700, color: 'var(--text-1)' }}>Rebate</p>
            <p style={{ fontSize: 11, color: 'var(--text-3)', letterSpacing: '0.05em' }}>청구서 분석 시스템</p>
          </div>
        </div>

        <h1 style={{ fontSize: 20, fontWeight: 700, color: 'var(--text-1)', marginBottom: 6 }}>로그인</h1>
        <p style={{ fontSize: 13, color: 'var(--text-3)', marginBottom: 28 }}>계정 정보를 입력하세요</p>

        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--text-2)', marginBottom: 7 }}>
              아이디
            </label>
            <input
              type="text"
              value={username}
              onChange={e => setUsername(e.target.value)}
              placeholder="아이디 입력"
              autoComplete="username"
              required
              style={inputStyle}
            />
          </div>

          <div>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--text-2)', marginBottom: 7 }}>
              비밀번호
            </label>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              placeholder="비밀번호 입력"
              autoComplete="current-password"
              required
              style={inputStyle}
            />
          </div>

          {error && (
            <p style={{
              fontSize: 12, color: '#b03030',
              background: '#fae8e8', border: '1px solid #f0c8c8',
              borderRadius: 8, padding: '9px 13px',
            }}>
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading || !username || !password}
            style={{
              marginTop: 6,
              width: '100%', padding: '12px 0', borderRadius: 10,
              background: username && password ? 'var(--primary)' : '#ede9e1',
              color: username && password ? '#fff' : 'var(--text-3)',
              border: 'none', fontSize: 14, fontWeight: 700,
              cursor: username && password && !loading ? 'pointer' : 'not-allowed',
              boxShadow: username && password ? '0 4px 14px rgba(10,110,110,0.3)' : 'none',
              display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
              transition: 'all 0.15s',
            }}
          >
            {loading
              ? <><Loader2 size={15} style={{ animation: 'spin 0.8s linear infinite' }} /> 로그인 중...</>
              : '로그인'
            }
          </button>
        </form>

        <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
      </div>
    </div>
  )
}
