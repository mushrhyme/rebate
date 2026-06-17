import { useState } from 'react'
import { NavLink, useNavigate, useLocation } from 'react-router-dom'
import {
  LayoutDashboard, Settings, FileDown,
  ChevronLeft, ChevronRight, ChevronDown, Users, LogOut, User, UserCog, BarChart2, Wand2,
} from 'lucide-react'
import { useAuth } from '../context/AuthContext'

const ADMIN_PATHS = ['/admin/users', '/admin/retail-assignment', '/admin/usage']

export function Layout({ children }: { children: React.ReactNode }) {
  const [collapsed, setCollapsed] = useState(false)
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const isAdmin = user?.is_admin || user?.username === 'admin'
  const isOnAdminPage = ADMIN_PATHS.some(p => location.pathname.startsWith(p))
  const [settingsOpen, setSettingsOpen] = useState(isOnAdminPage)

  const nav = [
    { to: '/', icon: LayoutDashboard, label: '대시보드' },
    { to: '/sap', icon: FileDown, label: 'SAP 내보내기' },
    { to: '/forms', icon: Settings, label: 'Form 관리' },
  ]

  const adminNav = [
    { to: '/dsl-studio', icon: Wand2, label: '규칙 스튜디오' },
    { to: '/admin/users', icon: Users, label: '사용자 관리' },
    { to: '/admin/retail-assignment', icon: UserCog, label: '소매처 담당자' },
    { to: '/admin/usage', icon: BarChart2, label: '사용량 모니터링' },
  ]

  async function handleLogout() {
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <div style={{ display: 'flex', height: '100vh', background: 'var(--bg)' }}>
      {/* Sidebar */}
      <aside style={{
        width: collapsed ? 56 : 220,
        minWidth: collapsed ? 56 : 220,
        background: '#1a1a1a',
        display: 'flex',
        flexDirection: 'column',
        flexShrink: 0,
        position: 'relative',
        overflow: 'hidden',
        transition: 'width 0.22s ease, min-width 0.22s ease',
      }}>
        {/* Subtle teal glow at top */}
        <div style={{
          position: 'absolute', top: -80, left: -40,
          width: 220, height: 220, borderRadius: '50%',
          background: 'radial-gradient(circle, rgba(10,110,110,0.14) 0%, transparent 70%)',
          pointerEvents: 'none',
        }} />

        {/* Logo + toggle */}
        <div style={{
          padding: collapsed ? '20px 10px 16px' : '22px 14px 18px',
          position: 'relative',
          display: 'flex',
          flexDirection: 'column',
          gap: 0,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: collapsed ? 0 : 10, justifyContent: collapsed ? 'center' : 'flex-start' }}>
            <div style={{
              width: 34, height: 34, borderRadius: 9, flexShrink: 0,
              background: '#0a6e6e',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 14, fontWeight: 800, color: '#fff',
              fontFamily: 'var(--mono)',
              boxShadow: '0 3px 12px rgba(10,110,110,0.4)',
            }}>R</div>
            {!collapsed && (
              <div style={{ overflow: 'hidden', flex: 1 }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: '#fff', letterSpacing: '-0.01em', whiteSpace: 'nowrap' }}>Rebate</div>
                <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.35)', marginTop: 1, letterSpacing: '0.04em', textTransform: 'uppercase', whiteSpace: 'nowrap' }}>
                  청구서 분석
                </div>
              </div>
            )}
            {!collapsed && (
              <button
                onClick={() => setCollapsed(true)}
                title="사이드바 접기"
                style={{
                  width: 26, height: 26, borderRadius: 7, flexShrink: 0,
                  background: 'rgba(255,255,255,0.07)', border: '1px solid rgba(255,255,255,0.1)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  cursor: 'pointer', color: 'rgba(255,255,255,0.45)',
                  transition: 'background 0.15s',
                }}
              >
                <ChevronLeft size={13} />
              </button>
            )}
          </div>
          {collapsed && (
            <button
              onClick={() => setCollapsed(false)}
              title="사이드바 펼치기"
              style={{
                marginTop: 10, width: 34, height: 26, borderRadius: 7, alignSelf: 'center',
                background: 'rgba(255,255,255,0.07)', border: '1px solid rgba(255,255,255,0.1)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                cursor: 'pointer', color: 'rgba(255,255,255,0.45)',
              }}
            >
              <ChevronRight size={13} />
            </button>
          )}
          {!collapsed && (
            <div style={{
              marginTop: 16, height: 1,
              background: 'rgba(255,255,255,0.08)',
            }} />
          )}
        </div>

        {/* Section label */}
        {!collapsed && (
          <div style={{ padding: '2px 16px 8px' }}>
            <span style={{ fontSize: 10, fontWeight: 600, color: 'rgba(255,255,255,0.22)', letterSpacing: '0.1em', textTransform: 'uppercase' }}>
              메뉴
            </span>
          </div>
        )}

        {/* Nav */}
        <nav style={{ flex: 1, padding: collapsed ? '0 8px' : '0 10px' }}>
          {nav.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              title={collapsed ? label : undefined}
              style={({ isActive }) => ({
                display: 'flex',
                alignItems: 'center',
                justifyContent: collapsed ? 'center' : 'flex-start',
                gap: 10,
                padding: collapsed ? '11px 0' : '10px 12px',
                borderRadius: 9,
                marginBottom: 3,
                fontSize: 13,
                fontWeight: isActive ? 600 : 400,
                color: isActive ? '#fff' : 'rgba(255,255,255,0.55)',
                background: isActive ? 'rgba(10,110,110,0.22)' : 'transparent',
                border: isActive ? '1px solid rgba(10,110,110,0.3)' : '1px solid transparent',
                textDecoration: 'none',
                transition: 'all 0.15s',
                position: 'relative',
              })}
            >
              {({ isActive }) => (
                <>
                  {isActive && !collapsed && (
                    <span style={{
                      position: 'absolute', left: 0, top: '50%', transform: 'translateY(-50%)',
                      width: 3, height: 18, borderRadius: '0 3px 3px 0',
                      background: '#0a6e6e',
                      boxShadow: '0 0 8px rgba(10,110,110,0.7)',
                    }} />
                  )}
                  <span style={{
                    color: isActive ? '#5bc4c4' : 'rgba(255,255,255,0.38)',
                    display: 'flex', alignItems: 'center', flexShrink: 0,
                  }}>
                    <Icon size={collapsed ? 17 : 15} />
                  </span>
                  {!collapsed && <span style={{ flex: 1, whiteSpace: 'nowrap' }}>{label}</span>}
                </>
              )}
            </NavLink>
          ))}
        </nav>

        {/* Footer — 사용자 정보 + 설정 + 로그아웃 */}
        <div style={{
          padding: collapsed ? '8px' : '8px 10px',
          borderTop: '1px solid rgba(255,255,255,0.07)',
        }}>
          {/* 사용자 정보 */}
          {!collapsed && user && (
            <div style={{
              display: 'flex', alignItems: 'center', gap: 8,
              padding: '8px 10px', marginBottom: 4,
            }}>
              <div style={{
                width: 28, height: 28, borderRadius: '50%', flexShrink: 0,
                background: 'rgba(10,110,110,0.25)',
                border: '1px solid rgba(10,110,110,0.4)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>
                <User size={13} color="#5bc4c4" />
              </div>
              <div style={{ overflow: 'hidden', flex: 1 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: '#fff', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {user.display_name_ja || user.display_name}
                </div>
                <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.35)', marginTop: 1, fontFamily: 'var(--mono)' }}>
                  {user.username}
                </div>
              </div>
            </div>
          )}
          {collapsed && user && (
            <div title={`${user.display_name_ja || user.display_name} (${user.username})`}
              style={{
                width: 34, height: 34, borderRadius: '50%', margin: '4px auto',
                background: 'rgba(10,110,110,0.25)', border: '1px solid rgba(10,110,110,0.4)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}
            >
              <User size={15} color="#5bc4c4" />
            </div>
          )}

          {/* 관리자 설정 그룹 */}
          {isAdmin && (
            <>
              <button
                onClick={() => {
                  if (collapsed) {
                    setCollapsed(false)
                    setSettingsOpen(true)
                  } else {
                    setSettingsOpen(v => !v)
                  }
                }}
                title={collapsed ? '설정' : undefined}
                style={{
                  width: '100%', display: 'flex', alignItems: 'center',
                  justifyContent: collapsed ? 'center' : 'flex-start',
                  gap: 8, padding: collapsed ? '9px 0' : '8px 10px',
                  borderRadius: 8, marginBottom: 2,
                  border: 'none', background: isOnAdminPage ? 'rgba(10,110,110,0.18)' : 'transparent',
                  color: isOnAdminPage ? '#5bc4c4' : 'rgba(255,255,255,0.4)',
                  fontSize: 13, cursor: 'pointer', fontFamily: 'inherit',
                  transition: 'all 0.15s',
                }}
              >
                <Settings size={collapsed ? 17 : 15} />
                {!collapsed && (
                  <>
                    <span style={{ flex: 1, textAlign: 'left' }}>설정</span>
                    <ChevronDown
                      size={12}
                      style={{
                        transform: settingsOpen ? 'rotate(0deg)' : 'rotate(-90deg)',
                        transition: 'transform 0.15s',
                        color: 'rgba(255,255,255,0.25)',
                      }}
                    />
                  </>
                )}
              </button>

              {!collapsed && settingsOpen && (
                <div style={{ paddingLeft: 10, marginBottom: 2 }}>
                  {adminNav.map(({ to, icon: Icon, label }) => (
                    <NavLink
                      key={to}
                      to={to}
                      title={undefined}
                      style={({ isActive }) => ({
                        display: 'flex', alignItems: 'center', gap: 8,
                        padding: '7px 10px', borderRadius: 7, marginBottom: 2,
                        textDecoration: 'none', fontSize: 12,
                        color: isActive ? '#5bc4c4' : 'rgba(255,255,255,0.38)',
                        background: isActive ? 'rgba(10,110,110,0.15)' : 'transparent',
                        transition: 'all 0.12s',
                      })}
                    >
                      <Icon size={13} />
                      <span>{label}</span>
                    </NavLink>
                  ))}
                </div>
              )}
            </>
          )}

          {/* 로그아웃 */}
          <button
            onClick={handleLogout}
            title={collapsed ? '로그아웃' : undefined}
            style={{
              width: '100%', display: 'flex', alignItems: 'center',
              justifyContent: collapsed ? 'center' : 'flex-start',
              gap: 8, padding: collapsed ? '9px 0' : '8px 10px',
              borderRadius: 8, border: 'none', background: 'transparent',
              color: 'rgba(255,255,255,0.35)', fontSize: 13,
              cursor: 'pointer', transition: 'color 0.15s',
            }}
          >
            <LogOut size={collapsed ? 17 : 15} />
            {!collapsed && <span>로그아웃</span>}
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main style={{ flex: 1, minWidth: 0, height: '100%', overflow: 'auto' }}>{children}</main>
    </div>
  )
}
