import { useState, useEffect, useCallback, Fragment } from 'react'
import { RefreshCw } from 'lucide-react'
import { api, type TokenUsagePhase } from '../api/client'

/* ── 비용 계산 ──────────────────────────────────────────────────────────────── */

const PRICING: Record<string, { input: number; output: number }> = {
  'claude-haiku-4-5-20251001': { input: 0.80,  output: 4.0  },
  'claude-sonnet-4-6':         { input: 3.0,   output: 15.0 },
}
const DEFAULT_PRICE = PRICING['claude-sonnet-4-6']

function getPrice(model: string) { return PRICING[model] ?? DEFAULT_PRICE }
function phaseCost(ph: TokenUsagePhase): number {
  const p = getPrice(ph.model)
  return (ph.input / 1_000_000) * p.input + (ph.output / 1_000_000) * p.output
}
function runCost(phases: Record<string, TokenUsagePhase>): number {
  return Object.values(phases).reduce((s, ph) => s + phaseCost(ph), 0)
}
function fmtCost(usd: number): string {
  if (usd === 0)   return '$0'
  if (usd < 0.001) return '< $0.001'
  if (usd < 0.01)  return `$${usd.toFixed(4)}`
  if (usd < 1)     return `$${usd.toFixed(3)}`
  return `$${usd.toFixed(2)}`
}
function fmtTok(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1000)      return `${(n / 1000).toFixed(1)}k`
  return String(n)
}
function fmtRunAt(iso: string): string {
  const d  = new Date(iso)        // 브라우저 로컬 시간으로 변환
  const m  = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${m}/${dd} ${hh}:${mm}`
}

/* ── 날짜 유틸 ──────────────────────────────────────────────────────────────── */

function toDateStr(d: Date): string { return d.toISOString().slice(0, 10) }
function todayStr():         string { return toDateStr(new Date()) }
function thisMonthStart():   string {
  const n = new Date()
  return toDateStr(new Date(n.getFullYear(), n.getMonth(), 1))
}

type Preset = 'this_month' | 'last_month' | 'last_3_months'
const PRESETS: { key: Preset; label: string }[] = [
  { key: 'this_month',    label: '이번 달' },
  { key: 'last_month',    label: '지난 달' },
  { key: 'last_3_months', label: '최근 3개월' },
]

function presetDates(p: Preset): { start: string; end: string } {
  const now   = new Date()
  const today = toDateStr(now)
  if (p === 'this_month') {
    return { start: toDateStr(new Date(now.getFullYear(), now.getMonth(), 1)), end: today }
  }
  if (p === 'last_month') {
    return {
      start: toDateStr(new Date(now.getFullYear(), now.getMonth() - 1, 1)),
      end:   toDateStr(new Date(now.getFullYear(), now.getMonth(), 0)),
    }
  }
  const ago = new Date(now); ago.setDate(ago.getDate() - 90)
  return { start: toDateStr(ago), end: today }
}

function rangePeriodLabel(start: string, end: string): string {
  if (!start) return '—'
  const s  = new Date(start + 'T12:00:00')
  const e  = new Date((end || start) + 'T12:00:00')
  const sy = s.getFullYear(), sm = s.getMonth() + 1
  const ey = e.getFullYear(), em = e.getMonth() + 1
  if (sy === ey && sm === em) return `${sy}년 ${sm}월`
  if (sy === ey) return `${sy}년 ${sm}월 – ${em}월`
  return `${sy}년 ${sm}월 – ${ey}년 ${em}월`
}

/* ── 타입 ───────────────────────────────────────────────────────────────────── */

interface UsageRun {
  run_id: string; doc_id: string; run_at: string; pdf_filename: string
  status: string; confirmed_at: string | null; pages_count: number | null
  uploader_username: string | null; uploader_name_ja: string | null; uploader_name: string | null
  phases: Record<string, TokenUsagePhase>
}
interface UsageResponse { runs: UsageRun[]; period: string; start: string; end: string }

const STATUS_LABELS: Record<string, string> = {
  queued: '대기중', ocr: 'OCR중', analyzing: '분석중',
  pending: '확인 대기', done: '완료', error: '오류',
}

/* ── Component ──────────────────────────────────────────────────────────────── */

export function UsageMonitor() {
  const [dateStart, setDateStart] = useState(thisMonthStart)
  const [dateEnd,   setDateEnd]   = useState(todayStr)
  const [runs,      setRuns]      = useState<UsageRun[]>([])
  const [loading,   setLoading]   = useState(false)
  const [error,     setError]     = useState<string | null>(null)
  const [hoveredDay, setHoveredDay] = useState<string | null>(null)

  const load = useCallback(async (start: string, end: string) => {
    if (!start || !end || end < start) return
    setLoading(true); setError(null)
    try {
      const res = await api.getUsage({ startDate: start, endDate: end }) as UsageResponse
      setRuns(res.runs)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '불러오기 실패')
    } finally { setLoading(false) }
  }, [])

  useEffect(() => { load(dateStart, dateEnd) }, [dateStart, dateEnd, load])

  function applyPreset(key: Preset) {
    const { start, end } = presetDates(key)
    setDateStart(start); setDateEnd(end)
  }

  const activePreset = PRESETS.find(p => {
    const d = presetDates(p.key)
    return d.start === dateStart && d.end === dateEnd
  })?.key ?? null

  /* ── 집계 ──────────────────────────────────────────────────────────────────── */

  const totalCost  = runs.reduce((s, r) => s + runCost(r.phases), 0)
  const totalPages = runs.reduce((s, r) => s + (r.pages_count ?? 0), 0)
  const totalRuns  = runs.length
  const uniqueDocs = new Set(runs.map(r => r.doc_id)).size

  // 문서별 최초 run_id (runs는 newest-first → 마지막 덮어쓰기 = oldest)
  const originalRunId: Record<string, string> = {}
  for (const r of runs) originalRunId[r.doc_id] = r.run_id
  const docRunCount: Record<string, number> = {}
  for (const r of runs) docRunCount[r.doc_id] = (docRunCount[r.doc_id] ?? 0) + 1
  const rerunCount = runs.filter(r =>
    docRunCount[r.doc_id] > 1 && r.run_id !== originalRunId[r.doc_id]
  ).length

  const modelCosts: Record<string, number> = {}
  for (const run of runs) {
    for (const ph of Object.values(run.phases)) {
      const key = ph.model.includes('haiku') ? 'Haiku'
                : ph.model.includes('sonnet') ? 'Sonnet'
                : ph.model.includes('opus') ? 'Opus' : ph.model
      modelCosts[key] = (modelCosts[key] ?? 0) + phaseCost(ph)
    }
  }

  type UserStat = { name: string; cost: number; runs: number; reruns: number }
  const userMap: Record<string, UserStat> = {}
  for (const run of runs) {
    const key     = run.uploader_username ?? '(알 수 없음)'
    const name    = run.uploader_name_ja ?? run.uploader_name ?? key
    const isRerun = docRunCount[run.doc_id] > 1 && run.run_id !== originalRunId[run.doc_id]
    if (!userMap[key]) userMap[key] = { name, cost: 0, runs: 0, reruns: 0 }
    userMap[key].cost += runCost(run.phases)
    userMap[key].runs += 1
    if (isRerun) userMap[key].reruns += 1
  }
  const users = Object.entries(userMap).sort(([, a], [, b]) => b.cost - a.cost)

  const dailyMap: Record<string, number> = {}
  for (const run of runs) {
    const day = run.run_at.slice(0, 10)
    dailyMap[day] = (dailyMap[day] ?? 0) + runCost(run.phases)
  }
  const dailyEntries = Object.entries(dailyMap).sort(([a], [b]) => a.localeCompare(b))
  const maxDailyCost = Math.max(...dailyEntries.map(([, v]) => v), 0.0000001)

  function showDayLabel(i: number, total: number) {
    if (total <= 7)  return true
    if (total <= 14) return i % 2 === 0
    if (total <= 31) return i % 5 === 0 || i === total - 1
    return i % 10 === 0 || i === total - 1
  }

  /* ── Render ─────────────────────────────────────────────────────────────────── */

  return (
    <div style={{ minHeight: '100%', background: 'var(--bg)', padding: '32px 36px' }}>
      <style>{`
        @keyframes spin { from { transform: rotate(0deg) } to { transform: rotate(360deg) } }
        .um-tr:hover td { background: var(--primary-light) !important; transition: background 0.1s; }
        .um-preset:hover { background: var(--primary-light) !important; color: var(--primary) !important; border-color: rgba(10,110,110,0.3) !important; }
        input[type="date"].um-date { padding: 6px 10px; border-radius: 7px; border: 1px solid var(--border); font-size: 12px; background: var(--card); color: var(--text-1); font-family: var(--mono); outline: none; cursor: pointer; }
        input[type="date"].um-date:focus { border-color: var(--primary); box-shadow: 0 0 0 2px rgba(10,110,110,0.12); }
        input[type="date"]::-webkit-calendar-picker-indicator { opacity: 0.45; cursor: pointer; }
      `}</style>

      <div style={{ maxWidth: 1100, margin: '0 auto' }}>

        {/* ── 헤더 ───────────────────────────────────────────────────────────────── */}
        <div style={{ display: 'flex', alignItems: 'flex-start', marginBottom: 28, gap: 16 }}>

          {/* 제목 + 기간 레이블 */}
          <div style={{ flexShrink: 0 }}>
            <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-3)', letterSpacing: '0.10em', textTransform: 'uppercase', marginBottom: 8 }}>
              사용량 모니터링
            </div>
            <div style={{ fontSize: 28, fontWeight: 800, color: 'var(--text-1)', letterSpacing: '-0.03em', lineHeight: 1 }}>
              {rangePeriodLabel(dateStart, dateEnd)}
            </div>
          </div>

          <div style={{ flex: 1 }} />

          {/* 날짜 범위 컨트롤 */}
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 8, paddingTop: 4 }}>

            {/* 프리셋 버튼 */}
            <div style={{ display: 'flex', gap: 4 }}>
              {PRESETS.map(p => {
                const active = p.key === activePreset
                return (
                  <button
                    key={p.key}
                    className={active ? '' : 'um-preset'}
                    onClick={() => applyPreset(p.key)}
                    style={{
                      padding: '4px 12px', borderRadius: 20,
                      border: `1px solid ${active ? 'var(--primary)' : 'var(--border)'}`,
                      fontSize: 12, fontWeight: active ? 600 : 400, cursor: 'pointer',
                      background: active ? 'var(--primary)' : 'transparent',
                      color: active ? '#fff' : 'var(--text-2)',
                      transition: 'all 0.12s',
                    }}
                  >
                    {p.label}
                  </button>
                )
              })}
            </div>

            {/* 날짜 직접 입력 */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <input
                type="date" className="um-date" value={dateStart}
                onChange={e => setDateStart(e.target.value)}
              />
              <span style={{ color: 'var(--text-3)', fontSize: 14, fontWeight: 300 }}>–</span>
              <input
                type="date" className="um-date" value={dateEnd}
                max={todayStr()}
                onChange={e => setDateEnd(e.target.value)}
              />
              <button
                onClick={() => load(dateStart, dateEnd)} disabled={loading}
                style={{
                  display: 'flex', alignItems: 'center', gap: 5,
                  padding: '6px 13px', borderRadius: 8,
                  border: '1px solid var(--border)', background: 'var(--card)',
                  fontSize: 12, fontWeight: 500, color: 'var(--text-2)',
                  cursor: loading ? 'not-allowed' : 'pointer', opacity: loading ? 0.6 : 1,
                  boxShadow: 'var(--shadow-sm)',
                }}
              >
                <RefreshCw size={11} style={{ animation: loading ? 'spin 1s linear infinite' : 'none' }} />
                새로고침
              </button>
            </div>
          </div>
        </div>

        {error && (
          <div style={{ padding: '11px 16px', background: '#fae8e8', color: 'var(--error)', borderRadius: 9, marginBottom: 20, fontSize: 13, border: '1px solid rgba(176,48,48,0.2)' }}>
            {error}
          </div>
        )}

        {/* ── KPI 카드 ────────────────────────────────────────────────────────────── */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 14, marginBottom: 14 }}>

          {/* 총 비용 — primary gradient */}
          <div style={{
            background: 'linear-gradient(135deg, #085858 0%, #0a6e6e 100%)',
            borderRadius: 14, padding: '22px 24px',
            boxShadow: '0 4px 18px rgba(10,110,110,0.28)',
          }}>
            <div style={{ fontSize: 10, fontWeight: 700, color: 'rgba(255,255,255,0.5)', letterSpacing: '0.10em', textTransform: 'uppercase', marginBottom: 12 }}>
              총 비용
            </div>
            <div style={{ fontSize: 36, fontWeight: 800, color: '#fff', letterSpacing: '-0.04em', lineHeight: 1, fontFamily: 'var(--mono)' }}>
              {loading ? <span style={{ opacity: 0.35 }}>—</span> : fmtCost(totalCost)}
            </div>
            {!loading && totalPages > 0 && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 10 }}>
                <span style={{ fontSize: 12, color: 'rgba(255,255,255,0.65)' }}>
                  총 <strong style={{ color: '#fff', fontWeight: 700 }}>{totalPages.toLocaleString()}</strong>페이지
                </span>
                <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.3)' }}>·</span>
                <span style={{ fontSize: 12, color: 'rgba(255,255,255,0.65)' }}>
                  장당 <strong style={{ color: '#fff', fontWeight: 700, fontFamily: 'var(--mono)' }}>{fmtCost(totalCost / totalPages)}</strong>
                </span>
              </div>
            )}
            {!loading && Object.keys(modelCosts).length > 0 && (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 16px', marginTop: 14, paddingTop: 12, borderTop: '1px solid rgba(255,255,255,0.15)' }}>
                {Object.entries(modelCosts).sort(([a],[b]) => a.localeCompare(b)).map(([model, cost]) => (
                  <div key={model} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <div style={{ width: 5, height: 5, borderRadius: '50%', background: model === 'Haiku' ? '#5bc4c4' : 'rgba(255,255,255,0.5)' }} />
                    <span style={{ fontSize: 11, color: 'rgba(255,255,255,0.55)' }}>{model}</span>
                    <span style={{ fontSize: 11, color: '#fff', fontFamily: 'var(--mono)', fontWeight: 600 }}>{fmtCost(cost)}</span>
                    {totalCost > 0 && (
                      <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.35)' }}>
                        {((cost / totalCost) * 100).toFixed(0)}%
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* 분석 실행 */}
          <div style={{ background: 'var(--card)', borderRadius: 14, padding: '22px 24px', border: '1px solid var(--border)', boxShadow: 'var(--shadow-sm)' }}>
            <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-3)', letterSpacing: '0.10em', textTransform: 'uppercase', marginBottom: 12 }}>
              분석 실행
            </div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 3 }}>
              <span style={{ fontSize: 36, fontWeight: 800, color: 'var(--text-1)', letterSpacing: '-0.04em', lineHeight: 1 }}>
                {loading ? <span style={{ color: 'var(--border)' }}>—</span> : totalRuns}
              </span>
              <span style={{ fontSize: 15, fontWeight: 500, color: 'var(--text-3)' }}>회</span>
            </div>
            <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--border)', fontSize: 12, color: 'var(--text-3)' }}>
              {loading ? ' ' : `문서 ${uniqueDocs}개`}
            </div>
          </div>

          {/* 재분석 */}
          <div style={{
            background: rerunCount > 0 ? '#fdf0e8' : 'var(--card)',
            borderRadius: 14, padding: '22px 24px',
            border: `1px solid ${rerunCount > 0 ? 'rgba(196,98,44,0.25)' : 'var(--border)'}`,
            boxShadow: rerunCount > 0 ? '0 2px 10px rgba(196,98,44,0.12)' : 'var(--shadow-sm)',
          }}>
            <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: '0.10em', textTransform: 'uppercase', marginBottom: 12, color: rerunCount > 0 ? 'var(--warning)' : 'var(--text-3)' }}>
              재분석 발생
            </div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 3 }}>
              <span style={{ fontSize: 36, fontWeight: 800, letterSpacing: '-0.04em', lineHeight: 1, color: rerunCount > 0 ? 'var(--warning)' : 'var(--border)' }}>
                {loading ? <span style={{ color: 'var(--border)' }}>—</span> : rerunCount}
              </span>
              {!loading && (
                <span style={{ fontSize: 15, fontWeight: 500, color: rerunCount > 0 ? 'var(--warning)' : 'var(--text-3)' }}>건</span>
              )}
            </div>
            <div style={{ marginTop: 12, paddingTop: 12, borderTop: `1px solid ${rerunCount > 0 ? 'rgba(196,98,44,0.18)' : 'var(--border)'}`, fontSize: 12, color: rerunCount > 0 ? 'var(--warning)' : 'var(--text-3)' }}>
              {loading ? ' ' : rerunCount === 0 ? '재분석 없음' : `전체 실행의 ${((rerunCount / totalRuns) * 100).toFixed(0)}%`}
            </div>
          </div>

        </div>

        {/* ── 일별 추이 ────────────────────────────────────────────────────────────── */}
        {(dailyEntries.length > 0 || loading) && (
          <div style={{ background: 'var(--card)', borderRadius: 14, padding: '20px 24px', marginBottom: 14, border: '1px solid var(--border)', boxShadow: 'var(--shadow-sm)' }}>
            <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-3)', letterSpacing: '0.10em', textTransform: 'uppercase', marginBottom: 18 }}>
              일별 비용 추이
            </div>
            {loading ? (
              <div style={{ height: 96, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--border)', fontSize: 13 }}>로딩 중…</div>
            ) : (
              <>
                <div style={{ display: 'flex', alignItems: 'flex-end', gap: 3, height: 96 }}>
                  {dailyEntries.map(([day, cost]) => {
                    const pct     = (cost / maxDailyCost) * 100
                    const hovered = hoveredDay === day
                    return (
                      <div
                        key={day}
                        style={{ flex: 1, minWidth: 6, display: 'flex', flexDirection: 'column', alignItems: 'center', position: 'relative', cursor: 'default' }}
                        onMouseEnter={() => setHoveredDay(day)}
                        onMouseLeave={() => setHoveredDay(null)}
                      >
                        {hovered && (
                          <div style={{
                            position: 'absolute', bottom: '100%', left: '50%', transform: 'translateX(-50%)',
                            background: 'var(--text-1)', color: '#fff',
                            fontSize: 11, padding: '5px 10px', borderRadius: 6,
                            whiteSpace: 'nowrap', marginBottom: 6, zIndex: 10, pointerEvents: 'none',
                            boxShadow: 'var(--shadow-md)',
                          }}>
                            {day.slice(5).replace('-', '/')} &nbsp;·&nbsp; {fmtCost(cost)}
                          </div>
                        )}
                        <div style={{
                          width: '100%', height: `${Math.max(pct, 4)}%`,
                          background: hovered ? 'var(--primary)' : 'var(--primary-light)',
                          borderRadius: '4px 4px 0 0', transition: 'background 0.12s',
                        }} />
                      </div>
                    )
                  })}
                </div>
                <div style={{ display: 'flex', gap: 3, marginTop: 7 }}>
                  {dailyEntries.map(([day], i) => (
                    <div key={day} style={{ flex: 1, textAlign: 'center', fontSize: 9, color: 'var(--text-3)', minWidth: 6 }}>
                      {showDayLabel(i, dailyEntries.length) ? day.slice(5).replace('-', '/') : ''}
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}

        {/* ── 사용자별 ─────────────────────────────────────────────────────────────── */}
        <div style={{ background: 'var(--card)', borderRadius: 14, overflow: 'hidden', marginBottom: 14, border: '1px solid var(--border)', boxShadow: 'var(--shadow-sm)' }}>
          <div style={{ padding: '13px 24px', borderBottom: '1px solid var(--border)' }}>
            <span style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-3)', letterSpacing: '0.10em', textTransform: 'uppercase' }}>사용자별</span>
          </div>
          {loading ? (
            <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-3)', fontSize: 13 }}>로딩 중…</div>
          ) : users.length === 0 ? (
            <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-3)', fontSize: 13 }}>데이터 없음</div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--border)', background: 'rgba(0,0,0,0.015)' }}>
                  {[
                    { label: '이름',        align: 'left'  },
                    { label: '비용',        align: 'right' },
                    { label: '실행',        align: 'right' },
                    { label: '재분석',      align: 'right' },
                    { label: '실행당 평균', align: 'right' },
                  ].map(h => (
                    <th key={h.label} style={{ padding: '10px 24px', textAlign: h.align as 'left'|'right', fontSize: 10, fontWeight: 700, color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '0.07em' }}>
                      {h.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {users.map(([key, stat]) => (
                  <tr key={key} className="um-tr" style={{ borderBottom: '1px solid var(--border)' }}>
                    <td style={{ padding: '12px 24px', color: 'var(--text-1)', fontWeight: 500 }}>{stat.name}</td>
                    <td style={{ padding: '12px 24px', textAlign: 'right', color: 'var(--primary)', fontFamily: 'var(--mono)', fontWeight: 700 }}>
                      {fmtCost(stat.cost)}
                    </td>
                    <td style={{ padding: '12px 24px', textAlign: 'right', color: 'var(--text-2)' }}>{stat.runs}회</td>
                    <td style={{ padding: '12px 24px', textAlign: 'right', fontWeight: stat.reruns > 0 ? 600 : 400, color: stat.reruns > 0 ? 'var(--warning)' : 'var(--border)' }}>
                      {stat.reruns > 0 ? `${stat.reruns}건` : '—'}
                    </td>
                    <td style={{ padding: '12px 24px', textAlign: 'right', color: 'var(--text-3)', fontFamily: 'var(--mono)' }}>
                      {fmtCost(stat.cost / stat.runs)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* ── 실행 이력 ─────────────────────────────────────────────────────────────── */}
        <div style={{ background: 'var(--card)', borderRadius: 14, overflow: 'hidden', border: '1px solid var(--border)', boxShadow: 'var(--shadow-sm)' }}>
          <div style={{ padding: '13px 24px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-3)', letterSpacing: '0.10em', textTransform: 'uppercase' }}>실행 이력</span>
            {!loading && <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{totalRuns}회 · {uniqueDocs}개 문서</span>}
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--border)', background: 'rgba(0,0,0,0.015)' }}>
                  {[
                    { label: '문서명',     align: 'left'   },
                    { label: '실행 일시',  align: 'left'   },
                    { label: '업로더',     align: 'left'   },
                    { label: 'Phase 1',   align: 'right'  },
                    { label: 'Phase 2',   align: 'right'  },
                    { label: 'Phase 3',   align: 'right'  },
                    { label: '페이지',    align: 'right'  },
                    { label: '합계 비용', align: 'right'  },
                    { label: '상태',      align: 'center' },
                  ].map(h => (
                    <th key={h.label} style={{ padding: '10px 16px', textAlign: h.align as 'left'|'right'|'center', fontSize: 10, fontWeight: 700, color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '0.07em', whiteSpace: 'nowrap' }}>
                      {h.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr><td colSpan={9} style={{ padding: 40, textAlign: 'center', color: 'var(--text-3)', fontSize: 13 }}>로딩 중…</td></tr>
                ) : runs.length === 0 ? (
                  <tr><td colSpan={9} style={{ padding: 40, textAlign: 'center', color: 'var(--text-3)', fontSize: 13 }}>해당 기간에 기록된 실행이 없습니다</td></tr>
                ) : runs.map(run => {
                  const cost        = runCost(run.phases)
                  const isConfirmed = !!run.confirmed_at
                  const isRerun     = docRunCount[run.doc_id] > 1 && run.run_id !== originalRunId[run.doc_id]
                  const p1 = run.phases['phase1']
                  const p2 = run.phases['phase2']
                  const p3 = run.phases['phase3']
                  const runAt = run.run_at ? fmtRunAt(run.run_at) : '—'
                  // pdf_filename이 없으면 (삭제된 문서 등) doc_id를 대신 표시
                  const displayName = run.pdf_filename || run.doc_id

                  return (
                    <Fragment key={run.run_id}>
                      <tr className="um-tr" style={{ borderBottom: '1px solid var(--border)' }}>

                        {/* 문서명 */}
                        <td style={{ padding: '11px 16px', maxWidth: 220 }} title={displayName}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
                            <span style={{ color: run.pdf_filename ? 'var(--text-1)' : 'var(--text-3)', fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {displayName}
                            </span>
                            {isRerun && (
                              <span style={{
                                flexShrink: 0, padding: '2px 7px', borderRadius: 4,
                                fontSize: 9, fontWeight: 700, letterSpacing: '0.02em',
                                background: '#fdf0e8', color: 'var(--warning)', border: '1px solid rgba(196,98,44,0.25)',
                              }}>재분석</span>
                            )}
                          </div>
                        </td>

                        {/* 실행 일시 */}
                        <td style={{ padding: '11px 16px', color: 'var(--text-3)', fontFamily: 'var(--mono)', fontSize: 11, whiteSpace: 'nowrap' }}>
                          {runAt}
                        </td>

                        {/* 업로더 */}
                        <td style={{ padding: '11px 16px', color: 'var(--text-2)', fontSize: 12, whiteSpace: 'nowrap' }}>
                          {run.uploader_name_ja ?? run.uploader_name ?? <span style={{ color: 'var(--border)' }}>—</span>}
                        </td>

                        {/* Phase 1/2/3 토큰 */}
                        {[p1, p2, p3].map((ph, i) => (
                          <td key={i} style={{ padding: '11px 16px', fontFamily: 'var(--mono)', fontSize: 11, textAlign: 'right', whiteSpace: 'nowrap' }}>
                            {ph ? (
                              <>
                                <span style={{ color: 'var(--text-2)' }}>{fmtTok(ph.input)}</span>
                                <span style={{ color: 'var(--border)', margin: '0 2px' }}>/</span>
                                <span style={{ color: 'var(--text-3)' }}>{fmtTok(ph.output)}</span>
                              </>
                            ) : (
                              <span style={{ color: 'var(--border)' }}>—</span>
                            )}
                          </td>
                        ))}

                        {/* 페이지 수 + 장당 비용 */}
                        <td style={{ padding: '11px 16px', textAlign: 'right', whiteSpace: 'nowrap' }}>
                          {run.pages_count != null && run.pages_count > 0 ? (
                            <>
                              <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-2)' }}>
                                {run.pages_count}p
                              </div>
                              <div style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--mono)', marginTop: 1 }}>
                                {fmtCost(cost / run.pages_count)}/p
                              </div>
                            </>
                          ) : (
                            <span style={{ color: 'var(--border)' }}>—</span>
                          )}
                        </td>

                        {/* 합계 비용 */}
                        <td style={{ padding: '11px 16px', textAlign: 'right', fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 700, color: 'var(--primary)', whiteSpace: 'nowrap' }}>
                          {fmtCost(cost)}
                        </td>

                        {/* 상태 */}
                        <td style={{ padding: '11px 16px', textAlign: 'center' }}>
                          <span style={{
                            display: 'inline-flex', padding: '3px 10px', borderRadius: 20,
                            fontSize: 10, fontWeight: 600, whiteSpace: 'nowrap', letterSpacing: '0.02em',
                            ...(isConfirmed
                              ? { background: '#e8f4ee', color: 'var(--success)', border: '1px solid rgba(45,125,74,0.22)' }
                              : run.status === 'error'
                              ? { background: '#fae8e8', color: 'var(--error)', border: '1px solid rgba(176,48,48,0.22)' }
                              : { background: 'var(--bg)', color: 'var(--text-3)', border: '1px solid var(--border)' }
                            ),
                          }}>
                            {isConfirmed ? '확정' : (STATUS_LABELS[run.status] ?? run.status)}
                          </span>
                        </td>

                      </tr>
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>

      </div>
    </div>
  )
}
