import { useState, useEffect, useCallback } from 'react'
import { Plus, RefreshCw, Trash2, RotateCcw, ShieldCheck, ShieldOff } from 'lucide-react'
import { api, type AdminUser, type CreateUserPayload } from '../api/client'

const COL: React.CSSProperties = { padding: '10px 12px', fontSize: 13, textAlign: 'left' }
const TH: React.CSSProperties = { ...COL, fontWeight: 600, color: 'rgba(255,255,255,0.45)', fontSize: 11, letterSpacing: '0.07em', textTransform: 'uppercase', background: 'rgba(255,255,255,0.03)', borderBottom: '1px solid rgba(255,255,255,0.07)' }

const EMPTY: CreateUserPayload = {
  username: '', display_name: '', display_name_ja: '',
  department_ko: '', department_ja: '', role: '', category: '', is_admin: false,
}

export function UserManagement() {
  const [users, setUsers] = useState<AdminUser[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showAdd, setShowAdd] = useState(false)
  const [form, setForm] = useState<CreateUserPayload>(EMPTY)
  const [saving, setSaving] = useState(false)
  const [actionId, setActionId] = useState<number | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<AdminUser | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setUsers(await api.getUsers())
    } catch (e: any) {
      setError(e.message ?? '불러오기 실패')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    if (!form.username || !form.display_name) return
    setSaving(true)
    try {
      await api.createUser(form)
      setForm(EMPTY)
      setShowAdd(false)
      await load()
    } catch (e: any) {
      setError(e.message ?? '생성 실패')
    } finally {
      setSaving(false)
    }
  }

  async function handleResetPassword(u: AdminUser) {
    setActionId(u.user_id)
    try {
      await api.updateUser(u.user_id, { reset_password: true })
      await load()
    } catch (e: any) {
      setError(e.message ?? '초기화 실패')
    } finally {
      setActionId(null)
    }
  }

  async function handleToggleActive(u: AdminUser) {
    setActionId(u.user_id)
    try {
      await api.updateUser(u.user_id, { is_active: !u.is_active })
      await load()
    } catch (e: any) {
      setError(e.message ?? '변경 실패')
    } finally {
      setActionId(null)
    }
  }

  async function handleToggleAdmin(u: AdminUser) {
    setActionId(u.user_id)
    try {
      await api.updateUser(u.user_id, { is_admin: !u.is_admin })
      await load()
    } catch (e: any) {
      setError(e.message ?? '변경 실패')
    } finally {
      setActionId(null)
    }
  }

  async function handleDelete(u: AdminUser) {
    setActionId(u.user_id)
    setConfirmDelete(null)
    try {
      await api.deleteUser(u.user_id)
      await load()
    } catch (e: any) {
      setError(e.message ?? '삭제 실패')
    } finally {
      setActionId(null)
    }
  }

  const inputStyle: React.CSSProperties = {
    border: '1.5px solid var(--border)', borderRadius: 8,
    padding: '8px 11px', fontSize: 13, outline: 'none',
    background: 'var(--bg)', color: 'var(--text-1)', fontFamily: 'inherit',
    width: '100%', boxSizing: 'border-box',
  }

  return (
    <div style={{ padding: '32px 36px', maxWidth: 1100, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: 'var(--text-1)', margin: 0 }}>사용자 관리</h1>
          <p style={{ fontSize: 13, color: 'var(--text-3)', marginTop: 4 }}>시스템 계정 등록·수정·삭제</p>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          <button onClick={load} style={{
            display: 'flex', alignItems: 'center', gap: 6,
            padding: '8px 14px', borderRadius: 9, border: '1px solid var(--border)',
            background: 'var(--card)', color: 'var(--text-2)', fontSize: 13,
            cursor: 'pointer', fontFamily: 'inherit',
          }}>
            <RefreshCw size={14} /> 새로고침
          </button>
          <button onClick={() => setShowAdd(v => !v)} style={{
            display: 'flex', alignItems: 'center', gap: 6,
            padding: '8px 16px', borderRadius: 9, border: 'none',
            background: '#0a6e6e', color: '#fff', fontSize: 13, fontWeight: 600,
            cursor: 'pointer', fontFamily: 'inherit',
            boxShadow: '0 3px 10px rgba(10,110,110,0.35)',
          }}>
            <Plus size={15} /> 사용자 추가
          </button>
        </div>
      </div>

      {/* 추가 폼 */}
      {showAdd && (
        <div style={{
          background: 'var(--card)', border: '1px solid var(--border)',
          borderRadius: 14, padding: '24px 28px', marginBottom: 24,
        }}>
          <h2 style={{ fontSize: 15, fontWeight: 700, color: 'var(--text-1)', marginBottom: 20 }}>새 사용자</h2>
          <form onSubmit={handleCreate}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 14 }}>
              {[
                { label: 'ID *', key: 'username', placeholder: 'e.g. o30250012' },
                { label: '이름(한글) *', key: 'display_name', placeholder: '홍길동' },
                { label: '名前(日本語)', key: 'display_name_ja', placeholder: '홍길동' },
                { label: '부서(한글)', key: 'department_ko', placeholder: '영업팀' },
                { label: '部署(日本語)', key: 'department_ja', placeholder: '営業チーム' },
                { label: '권한', key: 'role', placeholder: '팀원 / 팀장' },
                { label: '분류', key: 'category', placeholder: '영업 / 관리' },
              ].map(({ label, key, placeholder }) => (
                <div key={key}>
                  <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text-3)', marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.06em' }}>{label}</label>
                  <input
                    value={(form as any)[key]}
                    onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))}
                    placeholder={placeholder}
                    style={inputStyle}
                  />
                </div>
              ))}
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, paddingTop: 24 }}>
                <input
                  type="checkbox"
                  id="is_admin"
                  checked={form.is_admin}
                  onChange={e => setForm(f => ({ ...f, is_admin: e.target.checked }))}
                  style={{ width: 16, height: 16, cursor: 'pointer' }}
                />
                <label htmlFor="is_admin" style={{ fontSize: 13, color: 'var(--text-2)', cursor: 'pointer' }}>관리자 권한 부여</label>
              </div>
            </div>
            <p style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 16 }}>
              ※ 초기 비밀번호는 ID와 동일하며, 첫 로그인 시 변경이 필요합니다.
            </p>
            {error && <p style={{ fontSize: 12, color: '#b03030', background: '#fae8e8', borderRadius: 8, padding: '8px 12px', marginBottom: 14 }}>{error}</p>}
            <div style={{ display: 'flex', gap: 10 }}>
              <button type="submit" disabled={saving || !form.username || !form.display_name} style={{
                padding: '9px 20px', borderRadius: 9, border: 'none',
                background: '#0a6e6e', color: '#fff', fontSize: 13, fontWeight: 600,
                cursor: saving ? 'not-allowed' : 'pointer', opacity: saving ? 0.7 : 1,
              }}>
                {saving ? '저장 중...' : '저장'}
              </button>
              <button type="button" onClick={() => { setShowAdd(false); setForm(EMPTY); setError('') }} style={{
                padding: '9px 20px', borderRadius: 9, border: '1px solid var(--border)',
                background: 'var(--card)', color: 'var(--text-2)', fontSize: 13, cursor: 'pointer',
              }}>
                취소
              </button>
            </div>
          </form>
        </div>
      )}

      {/* 에러 */}
      {error && !showAdd && (
        <p style={{ fontSize: 12, color: '#b03030', background: '#fae8e8', border: '1px solid #f0c8c8', borderRadius: 8, padding: '9px 13px', marginBottom: 16 }}>{error}</p>
      )}

      {/* 테이블 */}
      <div style={{ background: 'var(--card)', border: '1px solid var(--border)', borderRadius: 14, overflow: 'hidden' }}>
        {loading ? (
          <div style={{ padding: 48, textAlign: 'center', color: 'var(--text-3)', fontSize: 14 }}>불러오는 중...</div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                {['ID', '이름', '名前', '부서', '권한', '분류', '상태', '마지막 로그인', '조작'].map(h => (
                  <th key={h} style={TH}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {users.map((u, i) => (
                <tr key={u.user_id} style={{
                  background: i % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.01)',
                  opacity: u.is_active ? 1 : 0.45,
                  borderBottom: '1px solid rgba(255,255,255,0.04)',
                }}>
                  <td style={{ ...COL, fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text-2)' }}>{u.username}</td>
                  <td style={{ ...COL, color: 'var(--text-1)', fontWeight: 500 }}>
                    {u.display_name}
                    {u.is_admin && (
                      <span style={{ marginLeft: 6, fontSize: 10, background: 'rgba(10,110,110,0.2)', color: '#5bc4c4', border: '1px solid rgba(10,110,110,0.3)', borderRadius: 4, padding: '1px 5px' }}>관리자</span>
                    )}
                  </td>
                  <td style={{ ...COL, color: 'var(--text-2)' }}>{u.display_name_ja ?? '—'}</td>
                  <td style={{ ...COL, color: 'var(--text-3)', fontSize: 12 }}>{u.department_ko ?? '—'}</td>
                  <td style={{ ...COL, color: 'var(--text-3)', fontSize: 12 }}>{u.role ?? '—'}</td>
                  <td style={{ ...COL, color: 'var(--text-3)', fontSize: 12 }}>{u.category ?? '—'}</td>
                  <td style={COL}>
                    <span style={{
                      fontSize: 11, fontWeight: 600, borderRadius: 5, padding: '2px 8px',
                      background: u.is_active ? 'rgba(10,110,110,0.15)' : 'rgba(255,255,255,0.07)',
                      color: u.is_active ? '#5bc4c4' : 'rgba(255,255,255,0.3)',
                      border: `1px solid ${u.is_active ? 'rgba(10,110,110,0.3)' : 'rgba(255,255,255,0.1)'}`,
                    }}>
                      {u.is_active ? '활성' : '비활성'}
                    </span>
                    {u.force_password_change && (
                      <span style={{ marginLeft: 5, fontSize: 10, color: '#c4622c' }}>PW변경필요</span>
                    )}
                  </td>
                  <td style={{ ...COL, fontSize: 11, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}>
                    {u.last_login_at ? new Date(u.last_login_at).toLocaleDateString('ko-KR') : '—'}
                  </td>
                  <td style={{ ...COL }}>
                    <div style={{ display: 'flex', gap: 4 }}>
                      <ActionBtn
                        title="비밀번호 초기화 (ID와 동일로 재설정)"
                        icon={<RotateCcw size={13} />}
                        loading={actionId === u.user_id}
                        onClick={() => handleResetPassword(u)}
                        color="#c4622c"
                      />
                      <ActionBtn
                        title={u.is_admin ? '관리자 해제' : '관리자 지정'}
                        icon={u.is_admin ? <ShieldOff size={13} /> : <ShieldCheck size={13} />}
                        loading={actionId === u.user_id}
                        onClick={() => handleToggleAdmin(u)}
                        color="#5bc4c4"
                      />
                      <ActionBtn
                        title={u.is_active ? '비활성화' : '활성화'}
                        icon={<span style={{ fontSize: 11, fontWeight: 700 }}>{u.is_active ? 'OFF' : 'ON'}</span>}
                        loading={actionId === u.user_id}
                        onClick={() => handleToggleActive(u)}
                        color={u.is_active ? 'rgba(255,255,255,0.3)' : '#5bc4c4'}
                      />
                      <ActionBtn
                        title="삭제"
                        icon={<Trash2 size={13} />}
                        loading={actionId === u.user_id}
                        onClick={() => setConfirmDelete(u)}
                        color="#b03030"
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <p style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 10 }}>
        총 {users.length}명 · 활성 {users.filter(u => u.is_active).length}명
      </p>

      {/* 삭제 확인 다이얼로그 */}
      {confirmDelete && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100,
        }}
          onClick={() => setConfirmDelete(null)}
        >
          <div style={{
            background: 'var(--card)', border: '1px solid var(--border)',
            borderRadius: 16, padding: '28px 32px', width: 360,
            boxShadow: '0 20px 60px rgba(0,0,0,0.4)',
          }}
            onClick={e => e.stopPropagation()}
          >
            <h3 style={{ fontSize: 16, fontWeight: 700, color: 'var(--text-1)', marginBottom: 10 }}>사용자 삭제</h3>
            <p style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.6, marginBottom: 24 }}>
              <strong>{confirmDelete.display_name}</strong> ({confirmDelete.username}) 계정을 삭제합니다.<br />
              이 작업은 되돌릴 수 없습니다.
            </p>
            <div style={{ display: 'flex', gap: 10 }}>
              <button onClick={() => handleDelete(confirmDelete)} style={{
                flex: 1, padding: '10px 0', borderRadius: 9, border: 'none',
                background: '#b03030', color: '#fff', fontSize: 13, fontWeight: 600, cursor: 'pointer',
              }}>삭제</button>
              <button onClick={() => setConfirmDelete(null)} style={{
                flex: 1, padding: '10px 0', borderRadius: 9, border: '1px solid var(--border)',
                background: 'var(--card)', color: 'var(--text-2)', fontSize: 13, cursor: 'pointer',
              }}>취소</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function ActionBtn({ title, icon, loading, onClick, color }: {
  title: string
  icon: React.ReactNode
  loading: boolean
  onClick: () => void
  color: string
}) {
  return (
    <button
      title={title}
      disabled={loading}
      onClick={onClick}
      style={{
        width: 28, height: 28, borderRadius: 7,
        background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.09)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        cursor: loading ? 'not-allowed' : 'pointer', color,
        opacity: loading ? 0.5 : 1, transition: 'all 0.12s',
      }}
    >
      {icon}
    </button>
  )
}
