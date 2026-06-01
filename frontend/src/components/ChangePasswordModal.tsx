import { useState } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import { useAuth } from '../context/AuthContext'

export function ChangePasswordModal() {
  const { user, changePassword } = useAuth()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [showCurrent, setShowCurrent] = useState(false)
  const [showNext, setShowNext] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const inputStyle: React.CSSProperties = {
    flex: 1, border: '1.5px solid var(--border)', borderRadius: 9,
    padding: '10px 13px', fontSize: 14, outline: 'none',
    background: 'var(--bg)', color: 'var(--text-1)', fontFamily: 'inherit',
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    if (!current) { setError('現在のパスワードを入力してください'); return }
    if (!next) { setError('新しいパスワードを入力してください'); return }
    if (next !== confirm) { setError('新しいパスワードが一致しません'); return }
    setLoading(true)
    try {
      await changePassword(current, next)
    } catch (err: any) {
      setError(err.message ?? 'パスワード変更に失敗しました')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{
      height: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'var(--bg)',
    }}>
      <div style={{
        position: 'fixed', inset: 0, pointerEvents: 'none',
        background: 'radial-gradient(ellipse 60% 50% at 50% 0%, rgba(10,110,110,0.07) 0%, transparent 70%)',
      }} />

      <div style={{
        width: 400, background: 'var(--card)',
        borderRadius: 20, border: '1px solid var(--border)',
        boxShadow: '0 24px 64px rgba(26,21,18,0.12)',
        padding: '40px 36px', position: 'relative',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 28 }}>
          <div style={{
            width: 40, height: 40, borderRadius: 11,
            background: 'var(--primary)', display: 'flex', alignItems: 'center',
            justifyContent: 'center', fontSize: 17, fontWeight: 800,
            color: '#fff', fontFamily: 'var(--mono)',
            boxShadow: '0 4px 14px rgba(10,110,110,0.35)',
          }}>R</div>
          <div>
            <p style={{ fontSize: 15, fontWeight: 700, color: 'var(--text-1)' }}>Rebate</p>
            <p style={{ fontSize: 11, color: 'var(--text-3)', letterSpacing: '0.05em' }}>청구서 분석 시스템</p>
          </div>
        </div>

        <h1 style={{ fontSize: 19, fontWeight: 700, color: 'var(--text-1)', marginBottom: 6 }}>
          初回パスワードの変更
        </h1>
        <p style={{ fontSize: 13, color: 'var(--text-3)', marginBottom: 28 }}>
          セキュリティのため、初回ログイン時はパスワードの変更が必要です。
        </p>

        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {[
            { label: '現在のパスワード（初期パスワード＝IDと同じ）', val: current, set: setCurrent, show: showCurrent, toggle: () => setShowCurrent(v => !v) },
            { label: '新しいパスワード', val: next, set: setNext, show: showNext, toggle: () => setShowNext(v => !v), hint: '※ ログインIDと同一のパスワードは使用できません' },
            { label: '新しいパスワード（確認）', val: confirm, set: setConfirm, show: showConfirm, toggle: () => setShowConfirm(v => !v) },
          ].map(({ label, val, set, show, toggle, hint }) => (
            <div key={label}>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--text-2)', marginBottom: 7 }}>
                {label}
              </label>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <input
                  type={show ? 'text' : 'password'}
                  value={val}
                  onChange={e => set(e.target.value)}
                  disabled={loading}
                  style={inputStyle}
                />
                <button type="button" onClick={toggle} tabIndex={-1} style={{
                  background: 'none', border: 'none', cursor: 'pointer',
                  color: 'var(--text-3)', padding: 4, display: 'flex',
                }}>
                  {show ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </div>
              {hint && <p style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 5 }}>{hint}</p>}
            </div>
          ))}

          {error && (
            <p style={{
              fontSize: 12, color: '#b03030',
              background: '#fae8e8', border: '1px solid #f0c8c8',
              borderRadius: 8, padding: '9px 13px',
            }}>{error}</p>
          )}

          <button
            type="submit"
            disabled={loading}
            style={{
              marginTop: 4, width: '100%', padding: '12px 0', borderRadius: 10,
              background: 'var(--primary)', color: '#fff',
              border: 'none', fontSize: 14, fontWeight: 700,
              cursor: loading ? 'not-allowed' : 'pointer',
              boxShadow: '0 4px 14px rgba(10,110,110,0.3)',
              opacity: loading ? 0.7 : 1,
            }}
          >
            {loading ? '変更中...' : 'パスワードを変更'}
          </button>
        </form>
      </div>
    </div>
  )
}
