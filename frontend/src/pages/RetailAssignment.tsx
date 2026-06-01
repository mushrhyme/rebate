import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { RefreshCw, ArrowRightLeft, Search, Users } from 'lucide-react'
import { api, type RetailRep, type RetailRetailer } from '../api/client'

const TH: React.CSSProperties = {
  padding: '9px 14px', fontSize: 11, fontWeight: 600,
  color: 'var(--text-3)', letterSpacing: '0.07em', textTransform: 'uppercase',
  background: '#ede9e1', borderBottom: '1px solid var(--border)',
  textAlign: 'left',
}
const TD: React.CSSProperties = {
  padding: '9px 14px', fontSize: 13, color: 'var(--text-1)', textAlign: 'left',
}

function repKey(r: RetailRep) {
  return r.rep_id || '__unassigned__'
}

export function RetailAssignment() {
  const [reps, setReps] = useState<RetailRep[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  // ★ selectedKey 대신 Rep 객체 직접 보관 — useMemo find() 타이밍 이슈 제거
  const [selectedRep, setSelectedRep] = useState<RetailRep | null>(null)
  const [search, setSearch] = useState('')
  const [saving, setSaving] = useState(false)
  const [bulkDialog, setBulkDialog] = useState(false)
  const [bulkTarget, setBulkTarget] = useState('')
  const [pending, setPending] = useState<Map<string, string>>(new Map())
  const loadIdRef = useRef(0)
  const tableScrollRef = useRef<HTMLDivElement>(null)

  // 담당자 전환 시 ① 스크롤 초기화 ② 이전 미저장 변경 폐기
  useEffect(() => {
    if (tableScrollRef.current) tableScrollRef.current.scrollTop = 0
    setPending(new Map())
  }, [selectedRep])

  const load = useCallback(async (resetSelection = false) => {
    const id = ++loadIdRef.current
    // 초기 로드만 전체 스피너 표시. 저장 후 reload는 패널 유지
    if (resetSelection) setLoading(true)
    setError('')
    try {
      const data = await api.getRetailAssignments()
      if (id !== loadIdRef.current) return  // stale 응답 폐기
      setReps(data.reps)
      setTotal(data.total_retailers)
      if (resetSelection) {
        setSelectedRep(data.reps[0] ?? null)
      } else {
        setSelectedRep(prev =>
          prev ? (data.reps.find(r => repKey(r) === repKey(prev)) ?? prev) : null
        )
      }
    } catch (e: any) {
      if (id !== loadIdRef.current) return
      setError(e.message ?? '불러오기 실패')
    } finally {
      if (id === loadIdRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => { load(true) }, [load])

  const filteredRetailers = useMemo(() => {
    if (!selectedRep) return []
    const q = search.trim().toLowerCase()
    if (!q) return selectedRep.retailers
    return selectedRep.retailers.filter((r: RetailRetailer) =>
      r.retailer_name.toLowerCase().includes(q) ||
      r.retailer_code.includes(q) ||
      r.dist_name.toLowerCase().includes(q),
    )
  }, [selectedRep, search])

  const pendingCount = useMemo(
    () => selectedRep?.retailers.filter(r => pending.has(r.retailer_code)).length ?? 0,
    [selectedRep, pending],
  )

  function handleRepChange(retailerCode: string, newKey: string) {
    if (!selectedRep) return
    setPending(prev => {
      const next = new Map(prev)
      if (newKey === repKey(selectedRep)) next.delete(retailerCode)
      else next.set(retailerCode, newKey)
      return next
    })
  }

  async function handleSave() {
    if (!selectedRep || pendingCount === 0) return
    const byRep = new Map<string, string[]>()
    for (const retailer of selectedRep.retailers) {
      const newKey = pending.get(retailer.retailer_code)
      if (!newKey) continue
      if (!byRep.has(newKey)) byRep.set(newKey, [])
      byRep.get(newKey)!.push(retailer.retailer_code)
    }
    setSaving(true)
    try {
      for (const [newKey, codes] of byRep) {
        const target = reps.find(r => repKey(r) === newKey)
        if (!target) continue
        await api.patchRetailAssignment({
          retailer_codes: codes,
          new_rep_id: target.rep_id,
          new_rep_name: target.rep_name,
          new_system_id: target.system_id,
        })
      }
      setPending(prev => {
        const next = new Map(prev)
        selectedRep.retailers.forEach(r => next.delete(r.retailer_code))
        return next
      })
      await load(false)
    } catch (e: any) {
      setError(e.message ?? '저장 실패')
    } finally {
      setSaving(false)
    }
  }

  async function handleBulkReassign() {
    if (!selectedRep || !bulkTarget) return
    const target = reps.find(r => repKey(r) === bulkTarget)
    if (!target) return
    setSaving(true)
    try {
      await api.patchRetailAssignment({
        retailer_codes: selectedRep.retailers.map(r => r.retailer_code),
        new_rep_id: target.rep_id,
        new_rep_name: target.rep_name,
        new_system_id: target.system_id,
      })
      setBulkDialog(false)
      setBulkTarget('')
      await load(false)
    } catch (e: any) {
      setError(e.message ?? '재배정 실패')
    } finally {
      setSaving(false)
    }
  }

  const selectedKey = selectedRep ? repKey(selectedRep) : null

  // ── 레이아웃: 외부 div height:100% → main의 height:100% 에 의존
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>

      {/* 페이지 헤더 */}
      <div style={{
        flexShrink: 0,
        padding: '20px 24px 16px',
        borderBottom: '1px solid var(--border)',
        background: 'var(--card)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 700, color: 'var(--text-1)', margin: 0 }}>
            소매처 담당자 관리
          </h1>
          <p style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 3 }}>
            retail_user.csv · 총 {total}개 소매처
          </p>
        </div>
        <button
          onClick={() => { setPending(new Map()); load(false) }}
          style={{
            display: 'flex', alignItems: 'center', gap: 6,
            padding: '7px 14px', borderRadius: 8, border: '1px solid var(--border)',
            background: 'var(--card)', color: 'var(--text-2)', fontSize: 13,
            cursor: 'pointer', fontFamily: 'inherit',
          }}
        >
          <RefreshCw size={13} /> 새로고침
        </button>
      </div>

      {error && (
        <div style={{ flexShrink: 0, padding: '8px 24px' }}>
          <p style={{
            fontSize: 12, color: 'var(--error)', background: '#fae8e8',
            border: '1px solid #f0c8c8', borderRadius: 8, padding: '8px 12px', margin: 0,
          }}>{error}</p>
        </div>
      )}

      {/* 투 패널 — 나머지 높이 전부 사용 */}
      <div style={{ flex: 1, minHeight: 0, display: 'flex' }}>

        {/* ── 왼쪽: 담당자 목록 ── */}
        <aside style={{
          width: 220, flexShrink: 0,
          borderRight: '1px solid var(--border)',
          background: 'var(--card)',
          display: 'flex', flexDirection: 'column',
          overflowY: 'auto',
        }}>
          <div style={{
            padding: '12px 14px 8px', flexShrink: 0,
            borderBottom: '1px solid var(--border)',
          }}>
            <span style={{
              fontSize: 10, fontWeight: 700, color: 'var(--text-3)',
              letterSpacing: '0.1em', textTransform: 'uppercase',
            }}>
              담당자 ({reps.length}명)
            </span>
          </div>

          <div style={{ flex: 1, padding: '8px', overflowY: 'auto' }}>
            {loading ? (
              <div style={{ padding: '20px 8px', fontSize: 13, color: 'var(--text-3)', textAlign: 'center' }}>
                불러오는 중...
              </div>
            ) : reps.map(rep => {
              const key = repKey(rep)
              const isSelected = key === selectedKey
              const repPending = rep.retailers.filter(r => pending.has(r.retailer_code)).length

              return (
                <button
                  key={key}
                  onClick={() => { setSelectedRep(rep); setSearch('') }}
                  style={{
                    width: '100%', display: 'flex', alignItems: 'center', gap: 10,
                    padding: '9px 10px', borderRadius: 9, marginBottom: 2,
                    border: `1px solid ${isSelected ? 'rgba(10,110,110,0.25)' : 'transparent'}`,
                    background: isSelected ? 'rgba(10,110,110,0.08)' : 'transparent',
                    cursor: 'pointer', textAlign: 'left', fontFamily: 'inherit',
                    transition: 'all 0.12s',
                  }}
                >
                  <div style={{
                    width: 30, height: 30, borderRadius: '50%', flexShrink: 0,
                    background: isSelected ? 'rgba(10,110,110,0.12)' : '#ede9e1',
                    border: `1px solid ${isSelected ? 'rgba(10,110,110,0.3)' : 'var(--border)'}`,
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                  }}>
                    <Users size={13} color={isSelected ? 'var(--primary)' : 'var(--text-3)'} />
                  </div>
                  <div style={{ flex: 1, overflow: 'hidden' }}>
                    <div style={{
                      fontSize: 13, fontWeight: isSelected ? 600 : 400,
                      color: isSelected ? 'var(--primary)' : 'var(--text-1)',
                      whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                    }}>
                      {rep.rep_name || '미배정'}
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 1 }}>
                      {rep.retailers.length}개 담당
                    </div>
                  </div>
                  {repPending > 0 && (
                    <span style={{
                      fontSize: 10, fontWeight: 700, flexShrink: 0,
                      background: 'rgba(196,98,44,0.1)', color: 'var(--warning)',
                      border: '1px solid rgba(196,98,44,0.25)',
                      borderRadius: 5, padding: '2px 6px',
                    }}>
                      {repPending}
                    </span>
                  )}
                </button>
              )
            })}
          </div>
        </aside>

        {/* ── 오른쪽: 소매처 테이블 ── */}
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
          {!selectedRep ? (
            <div style={{
              flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: 'var(--text-3)', fontSize: 14,
            }}>
              왼쪽에서 담당자를 선택하세요
            </div>
          ) : (
            // key 로 강제 remount — 담당자 전환 시 이전 행 누적 방지
            <div key={selectedKey} style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
              {/* 서브헤더 */}
              <div style={{
                flexShrink: 0, padding: '12px 16px',
                borderBottom: '1px solid var(--border)',
                background: 'var(--card)',
                display: 'flex', alignItems: 'center', gap: 10,
              }}>
                <div style={{ position: 'relative', width: 280 }}>
                  <Search size={13} style={{
                    position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)',
                    color: 'var(--text-3)', pointerEvents: 'none',
                  }} />
                  <input
                    value={search}
                    onChange={e => setSearch(e.target.value)}
                    placeholder="소매처명 · 코드 · 판매처 검색"
                    style={{
                      width: '100%', padding: '7px 10px 7px 30px',
                      border: '1px solid var(--border)', borderRadius: 8,
                      background: 'var(--bg)', color: 'var(--text-1)',
                      fontSize: 13, fontFamily: 'inherit', outline: 'none',
                    }}
                  />
                </div>

                <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
                  {search
                    ? `${filteredRetailers.length} / ${selectedRep.retailers.length}개`
                    : `${selectedRep.retailers.length}개`}
                </span>

                <div style={{ flex: 1 }} />

                {selectedRep.rep_id && (
                  <button
                    onClick={() => { setBulkDialog(true); setBulkTarget('') }}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 6,
                      padding: '7px 13px', borderRadius: 8,
                      border: '1px solid var(--border)',
                      background: 'var(--bg)', color: 'var(--text-2)', fontSize: 13,
                      cursor: 'pointer', fontFamily: 'inherit',
                    }}
                  >
                    <ArrowRightLeft size={13} />
                    전체 이관 ({selectedRep.retailers.length}개)
                  </button>
                )}

                {pendingCount > 0 && (
                  <button
                    onClick={handleSave}
                    disabled={saving}
                    style={{
                      padding: '7px 16px', borderRadius: 8, border: 'none',
                      background: '#0a6e6e', color: '#fff', fontSize: 13, fontWeight: 600,
                      cursor: saving ? 'not-allowed' : 'pointer',
                      fontFamily: 'inherit', opacity: saving ? 0.7 : 1,
                      boxShadow: '0 2px 8px rgba(10,110,110,0.25)',
                    }}
                  >
                    {saving ? '저장 중...' : `변경사항 저장 (${pendingCount}건)`}
                  </button>
                )}
              </div>

              {/* 테이블 — 이 영역만 스크롤 */}
              <div ref={tableScrollRef} style={{ flex: 1, overflowY: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                  <thead>
                    <tr>
                      <th style={{ ...TH, width: 110 }}>소매처코드</th>
                      <th style={TH}>소매처명 (슈퍼마켓)</th>
                      <th style={TH}>판매처 (도매상)</th>
                      <th style={{ ...TH, width: 190 }}>담당 영업사원</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredRetailers.length === 0 ? (
                      <tr>
                        <td colSpan={4} style={{
                          padding: '48px 20px', textAlign: 'center',
                          fontSize: 13, color: 'var(--text-3)',
                        }}>
                          {search ? '검색 결과 없음' : '소매처 없음'}
                        </td>
                      </tr>
                    ) : filteredRetailers.map((retailer: RetailRetailer, i: number) => {
                      const currentKey = pending.get(retailer.retailer_code) ?? selectedKey!
                      const changed = currentKey !== selectedKey

                      return (
                        <tr
                          key={retailer.retailer_code}
                          style={{
                            background: changed
                              ? 'rgba(196,98,44,0.05)'
                              : i % 2 === 0 ? 'transparent' : 'rgba(0,0,0,0.015)',
                            borderBottom: '1px solid var(--border)',
                          }}
                        >
                          <td style={{ ...TD, fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-3)' }}>
                            {retailer.retailer_code}
                          </td>
                          <td style={{ ...TD, fontWeight: 500 }}>
                            {retailer.retailer_name}
                          </td>
                          <td style={{
                            ...TD, fontSize: 12, color: 'var(--text-2)',
                            maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                          }}>
                            {retailer.dist_name || '—'}
                          </td>
                          <td style={TD}>
                            <select
                              value={currentKey}
                              onChange={e => handleRepChange(retailer.retailer_code, e.target.value)}
                              style={{
                                width: '100%',
                                border: `1.5px solid ${changed ? 'rgba(196,98,44,0.5)' : 'var(--border)'}`,
                                borderRadius: 7, padding: '5px 8px', fontSize: 12,
                                background: changed ? '#fdf0e8' : 'var(--bg)',
                                color: 'var(--text-1)', fontFamily: 'inherit',
                                cursor: 'pointer', outline: 'none',
                              }}
                            >
                              {reps.map(r => (
                                <option key={repKey(r)} value={repKey(r)}>
                                  {r.rep_name || '미배정'}
                                </option>
                              ))}
                            </select>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* 일괄 이관 다이얼로그 */}
      {bulkDialog && selectedRep && (
        <div
          style={{
            position: 'fixed', inset: 0, background: 'rgba(26,21,18,0.45)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100,
          }}
          onClick={() => setBulkDialog(false)}
        >
          <div
            style={{
              background: 'var(--card)', border: '1px solid var(--border)',
              borderRadius: 16, padding: '28px 32px', width: 400,
              boxShadow: 'var(--shadow-md)',
            }}
            onClick={e => e.stopPropagation()}
          >
            <h3 style={{ fontSize: 16, fontWeight: 700, color: 'var(--text-1)', marginBottom: 8 }}>
              전체 이관
            </h3>
            <p style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.6, marginBottom: 20 }}>
              <strong>{selectedRep.rep_name}</strong>의 소매처{' '}
              <strong>{selectedRep.retailers.length}개</strong> 전체를 이관합니다.
            </p>
            <label style={{
              display: 'block', fontSize: 11, fontWeight: 600,
              color: 'var(--text-3)', marginBottom: 8,
              textTransform: 'uppercase', letterSpacing: '0.06em',
            }}>
              새 담당자
            </label>
            <select
              value={bulkTarget}
              onChange={e => setBulkTarget(e.target.value)}
              style={{
                width: '100%', border: '1.5px solid var(--border)', borderRadius: 8,
                padding: '9px 11px', fontSize: 13, background: 'var(--bg)',
                color: 'var(--text-1)', fontFamily: 'inherit', outline: 'none',
                marginBottom: 24, boxSizing: 'border-box', cursor: 'pointer',
              }}
            >
              <option value=''>— 선택 —</option>
              {reps
                .filter(r => repKey(r) !== repKey(selectedRep))
                .map(r => (
                  <option key={repKey(r)} value={repKey(r)}>
                    {r.rep_name || '미배정'} ({r.retailers.length}개 담당 중)
                  </option>
                ))}
            </select>
            <div style={{ display: 'flex', gap: 10 }}>
              <button
                onClick={handleBulkReassign}
                disabled={!bulkTarget || saving}
                style={{
                  flex: 1, padding: '10px 0', borderRadius: 9, border: 'none',
                  background: bulkTarget ? '#0a6e6e' : '#ddd8d0',
                  color: bulkTarget ? '#fff' : 'var(--text-3)',
                  fontSize: 13, fontWeight: 600,
                  cursor: bulkTarget && !saving ? 'pointer' : 'not-allowed',
                  opacity: saving ? 0.7 : 1,
                }}
              >
                {saving ? '이관 중...' : '이관'}
              </button>
              <button
                onClick={() => setBulkDialog(false)}
                style={{
                  flex: 1, padding: '10px 0', borderRadius: 9,
                  border: '1px solid var(--border)', background: 'var(--card)',
                  color: 'var(--text-2)', fontSize: 13, cursor: 'pointer',
                }}
              >
                취소
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
