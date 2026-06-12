import { useState, useEffect, useRef } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { CheckCircle2, ChevronLeft, ChevronRight, Check, Search, X, Loader2, AlertCircle } from 'lucide-react'
import { PdfViewer } from '../components/PdfViewer'
import { api, type Mapping, type MappingCandidate, type ProductResult, type RetailerResult } from '../api/client'

// ── 검색 모달 ──────────────────────────────────────────────────────────────────

function SearchModal({
  type, initialQuery, onSelect, onClose,
}: {
  type: 'retailer' | 'product'
  initialQuery: string
  onSelect: (code: string, name: string) => void
  onClose: () => void
}) {
  const [q, setQ] = useState(initialQuery)
  const [results, setResults] = useState<(ProductResult | RetailerResult)[]>([])
  const [searching, setSearching] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { inputRef.current?.focus() }, [])

  useEffect(() => {
    if (!q.trim()) { setResults([]); return }
    setSearching(true)
    const timer = setTimeout(async () => {
      try {
        const data = type === 'product'
          ? await api.searchProduct(q)
          : await api.searchRetailer(q)
        setResults(data)
      } finally {
        setSearching(false)
      }
    }, 300)
    return () => clearTimeout(timer)
  }, [q, type])

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 200,
        background: 'rgba(26,21,18,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        backdropFilter: 'blur(2px)',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          width: 540, maxHeight: '80vh',
          background: 'var(--card)', borderRadius: 16,
          border: '1px solid var(--border)',
          boxShadow: '0 20px 60px rgba(26,21,18,0.2)',
          display: 'flex', flexDirection: 'column', overflow: 'hidden',
        }}
      >
        <div style={{
          padding: '16px 20px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-1)' }}>
            {type === 'product' ? '제품 검색' : '소매처 검색'}
          </span>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-3)', padding: 4 }}>
            <X size={15} />
          </button>
        </div>

        <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border)', position: 'relative' }}>
          <Search size={15} style={{
            position: 'absolute', left: 34, top: '50%', transform: 'translateY(-50%)',
            color: 'var(--text-3)', pointerEvents: 'none',
          }} />
          <input
            ref={inputRef}
            value={q}
            onChange={e => setQ(e.target.value)}
            placeholder={type === 'product' ? '제품명 또는 JAN코드' : '소매처명'}
            style={{
              width: '100%', boxSizing: 'border-box',
              padding: '9px 12px 9px 36px',
              border: '1px solid var(--border)', borderRadius: 9,
              fontSize: 13, outline: 'none', background: 'var(--bg)',
              color: 'var(--text-1)',
            }}
          />
          {searching && (
            <Loader2 size={14} style={{
              position: 'absolute', right: 34, top: '50%', transform: 'translateY(-50%)',
              color: 'var(--text-3)', animation: 'spin 0.8s linear infinite',
            }} />
          )}
        </div>

        <div style={{ flex: 1, overflowY: 'auto' }}>
          {results.length === 0 && q.trim() && !searching && (
            <p style={{ padding: '24px', textAlign: 'center', fontSize: 13, color: 'var(--text-3)' }}>
              검색 결과가 없습니다
            </p>
          )}
          {results.length === 0 && !q.trim() && (
            <p style={{ padding: '24px', textAlign: 'center', fontSize: 12, color: 'var(--text-3)' }}>
              {type === 'product' ? '제품명 또는 JAN코드로 검색하세요' : '소매처명으로 검색하세요'}
            </p>
          )}
          {results.map((r, i) => (
            <button
              key={r.code}
              onClick={() => { onSelect(r.code, r.name); onClose() }}
              style={{
                width: '100%', textAlign: 'left',
                padding: '12px 20px',
                borderBottom: i < results.length - 1 ? '1px solid var(--border)' : 'none',
                background: 'transparent', border: 'none', cursor: 'pointer',
                display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                gap: 12,
              }}
              onMouseEnter={e => (e.currentTarget.style.background = '#f5ede0')}
              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <p style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-1)', marginBottom: 2 }}>
                  {r.name}
                </p>
                {'volume' in r && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '3px 10px', marginTop: 3 }}>
                    {r.volume    && <span style={{ fontSize: 10, color: 'var(--text-3)' }}><span style={{ opacity: 0.6 }}>용량 </span>{r.volume}</span>}
                    {r.spec      && <span style={{ fontSize: 10, color: 'var(--text-3)' }}><span style={{ opacity: 0.6 }}>규격 </span>{r.spec}</span>}
                    {r.sikiri   != null && <span style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}><span style={{ fontFamily: 'inherit', opacity: 0.6 }}>仕切 </span>{r.sikiri.toLocaleString()}</span>}
                    {r.honbucho != null && <span style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}><span style={{ fontFamily: 'inherit', opacity: 0.6 }}>本部長 </span>{r.honbucho.toLocaleString()}</span>}
                    {r.jan       && <span style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}><span style={{ fontFamily: 'inherit', opacity: 0.6 }}>JAN </span>{r.jan}</span>}
                  </div>
                )}
              </div>
              <span style={{
                fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-3)',
                background: '#ede9e1', borderRadius: 6, padding: '3px 8px', flexShrink: 0,
              }}>
                {r.code}
              </span>
            </button>
          ))}
        </div>
      </div>
      <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
    </div>
  )
}

// ── 후보 카드 ──────────────────────────────────────────────────────────────────

function CandidateCard({
  mapping, index, total, onPrev, onNext, onSave, savedEntry,
}: {
  mapping: Mapping; index: number; total: number
  onPrev: () => void; onNext: () => void
  onSave: (code: string, name: string) => Promise<void>
  savedEntry: { code: string; name: string } | null
}) {
  const getInitialSelected = (): MappingCandidate | null => {
    if (savedEntry) {
      return mapping.candidates.find(c => c.code === savedEntry.code) ?? { code: savedEntry.code, name: savedEntry.name }
    }
    return mapping.candidates[0] ?? null
  }

  const [selected, setSelected] = useState<MappingCandidate | null>(getInitialSelected)
  const [showSearch, setShowSearch] = useState(() => {
    if (!savedEntry) return false
    return !mapping.candidates.find(c => c.code === savedEntry.code)
  })
  const [saving, setSaving] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)

  const [autoResults, setAutoResults] = useState<MappingCandidate[]>([])
  const [autoLoading, setAutoLoading] = useState(
    mapping.candidates.length === 0 && mapping.mapping_type !== 'dist'
  )

  useEffect(() => {
    if (mapping.candidates.length > 0 || mapping.mapping_type === 'dist') {
      setAutoLoading(false)
      return
    }
    setAutoLoading(true)
    setAutoResults([])
    const run = mapping.mapping_type === 'product'
      ? api.searchProduct(mapping.ocr_name).then((res) =>
          (res as ProductResult[]).slice(0, 5).map((r): MappingCandidate => ({
            code: r.code, name: r.name,
            volume: r.volume,
            case_qty: r.spec || undefined,
            shikiri: r.sikiri ?? undefined,
            honbucho: r.honbucho ?? undefined,
          }))
        )
      : api.searchRetailer(mapping.ocr_name).then((res) =>
          (res as RetailerResult[]).slice(0, 5).map((r): MappingCandidate => ({
            code: r.code, name: r.name,
          }))
        )
    run.then(setAutoResults).catch(() => {}).finally(() => setAutoLoading(false))
  }, [mapping.id])

  const isSaved = savedEntry !== null
  const isUnchanged = isSaved && selected?.code === savedEntry?.code
  const canSave = selected !== null && !isUnchanged

  async function handleSave() {
    if (!selected) return
    setSaving(true)
    try {
      await onSave(selected.code, selected.name)
    } finally {
      setSaving(false)
    }
  }

  const candidateList = mapping.candidates.length > 0 ? mapping.candidates : autoResults

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* 스텝 인디케이터 */}
      <div style={{
        padding: '16px 20px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-3)', letterSpacing: '0.05em', textTransform: 'uppercase' }}>
            {mapping.mapping_type === 'retailer' ? '소매처' : mapping.mapping_type === 'dist' ? '판매처' : '제품'} 매핑 {index + 1}/{total}
          </span>
          {isSaved && (
            <span style={{
              fontSize: 10, fontWeight: 700, padding: '2px 8px', borderRadius: 20,
              background: '#eaf4ee', color: '#2d7d4a',
            }}>
              저장됨
            </span>
          )}
        </div>
        <div style={{ display: 'flex', gap: 4 }}>
          {([{ icon: ChevronLeft, fn: onPrev, dis: index === 0 }, { icon: ChevronRight, fn: onNext, dis: index === total - 1 }] as const).map(({ icon: Icon, fn, dis }, i) => (
            <button key={i} onClick={fn} disabled={dis} style={{
              width: 28, height: 28, borderRadius: 7, border: '1px solid var(--border)',
              background: 'var(--card)', cursor: dis ? 'not-allowed' : 'pointer',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: dis ? 'var(--text-3)' : 'var(--text-2)',
            }}>
              <Icon size={14} />
            </button>
          ))}
        </div>
      </div>

      {/* OCR 원문 */}
      <div style={{ padding: '14px 20px', background: '#f0f7f7', borderBottom: '1px solid var(--border)' }}>
        {mapping.mapping_type === 'dist' ? (
          <>
            <p style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-3)', letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: 4 }}>
              소매처
            </p>
            <p style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-1)', fontFamily: 'var(--mono)', marginBottom: 6 }}>
              {mapping.ocr_name}
            </p>
            <p style={{ fontSize: 10, fontWeight: 600, color: 'var(--primary)', letterSpacing: '0.06em', textTransform: 'uppercase' }}>
              ↓ 담당 판매처 선택
            </p>
          </>
        ) : (
          <>
            <p style={{ fontSize: 10, fontWeight: 600, color: 'var(--primary)', letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: 6 }}>
              OCR 원문
            </p>
            <p style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-1)', fontFamily: 'var(--mono)' }}>
              {mapping.ocr_name}
            </p>
          </>
        )}
      </div>

      {/* 후보 목록 */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '12px 20px', display: 'flex', flexDirection: 'column', gap: 8 }}>
        {autoLoading && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '12px 0', color: 'var(--text-3)', fontSize: 12 }}>
            <Loader2 size={13} style={{ animation: 'spin 0.8s linear infinite' }} />
            OCR 명칭으로 검색 중...
          </div>
        )}

        {!autoLoading && mapping.candidates.length === 0 && autoResults.length === 0 && !showSearch && (
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, padding: '12px 14px', borderRadius: 10, background: '#fdf0e8', border: '1px solid #dbb590' }}>
            <AlertCircle size={14} color="#c4622c" style={{ marginTop: 1, flexShrink: 0 }} />
            <p style={{ fontSize: 12, color: '#c4622c', lineHeight: 1.5 }}>
              일치하는 항목이 없습니다.<br />아래 직접 검색으로 찾아주세요.
            </p>
          </div>
        )}

        {!autoLoading && mapping.candidates.length === 0 && autoResults.length > 0 && (
          <p style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-3)', letterSpacing: '0.06em', textTransform: 'uppercase', paddingBottom: 2 }}>
            유사 검색 결과
          </p>
        )}

        {candidateList.map(c => {
          const isSelected = !showSearch && selected?.code === c.code
          return (
            <label key={c.code} onClick={() => {
              setShowSearch(false)
              setSelected(prev => prev?.code === c.code ? null : c)
            }} style={{
              display: 'flex', alignItems: 'flex-start', gap: 12,
              padding: '12px 14px', borderRadius: 10, cursor: 'pointer',
              border: `1.5px solid ${isSelected ? 'var(--primary)' : 'var(--border)'}`,
              background: isSelected ? 'var(--primary-light)' : 'var(--card)',
              transition: 'all 0.12s',
            }}>
              <div style={{
                width: 18, height: 18, borderRadius: '50%', marginTop: 1, flexShrink: 0,
                border: `2px solid ${isSelected ? 'var(--primary)' : 'var(--border)'}`,
                background: isSelected ? 'var(--primary)' : 'var(--card)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>
                {isSelected && <Check size={10} color="#fff" strokeWidth={3} />}
              </div>
              <div style={{ flex: 1 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-1)' }}>{c.name}</span>
                  {c.score != null && (
                    <span style={{
                      fontSize: 10, fontWeight: 600, padding: '2px 7px', borderRadius: 20, fontFamily: 'var(--mono)',
                      background: c.score >= 90 ? '#eaf4ee' : c.score >= 70 ? '#fdf0e8' : '#ede9e1',
                      color: c.score >= 90 ? '#2d7d4a' : c.score >= 70 ? '#c4622c' : 'var(--text-3)',
                    }}>{c.score}%</span>
                  )}
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '3px 10px', marginTop: 3 }}>
                  <span style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}>{c.code}</span>
                  {c.volume    && <span style={{ fontSize: 10, color: 'var(--text-3)' }}><span style={{ opacity: 0.6 }}>용량 </span>{c.volume}g</span>}
                  {c.case_qty  && <span style={{ fontSize: 10, color: 'var(--text-3)' }}><span style={{ opacity: 0.6 }}>규격 </span>{c.case_qty}</span>}
                  {c.shikiri   != null && c.shikiri > 0 && <span style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}><span style={{ fontFamily: 'inherit', opacity: 0.6 }}>仕切 </span>{c.shikiri.toLocaleString()}</span>}
                  {c.honbucho != null && c.honbucho > 0 && <span style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}><span style={{ fontFamily: 'inherit', opacity: 0.6 }}>本部長 </span>{c.honbucho.toLocaleString()}</span>}
                </div>
              </div>
            </label>
          )
        })}

        {/* 직접 검색으로 선택한 항목 */}
        {showSearch && selected && (
          <div style={{
            padding: '12px 14px', borderRadius: 10,
            border: '1.5px solid var(--primary)', background: 'var(--primary-light)',
            display: 'flex', alignItems: 'center', gap: 10,
          }}>
            <Check size={14} color="var(--primary)" />
            <div style={{ flex: 1 }}>
              <p style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-1)' }}>{selected.name}</p>
              <p style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}>{selected.code}</p>
            </div>
            <button
              onClick={() => setSearchOpen(true)}
              style={{ fontSize: 11, color: 'var(--primary)', background: 'none', border: 'none', cursor: 'pointer', padding: '2px 6px' }}
            >
              변경
            </button>
          </div>
        )}

        {mapping.mapping_type !== 'dist' && (
          <button
            onClick={() => setSearchOpen(true)}
            style={{
              display: 'flex', alignItems: 'center', gap: 8,
              padding: '11px 14px', borderRadius: 10, cursor: 'pointer',
              border: `1.5px solid ${showSearch && !selected ? 'var(--primary)' : 'var(--border)'}`,
              background: 'var(--card)', color: 'var(--text-2)',
              fontSize: 13, fontWeight: 500,
            }}
          >
            <Search size={14} />
            직접 검색
          </button>
        )}
      </div>

      {/* 저장 버튼 */}
      <div style={{ padding: '14px 20px', borderTop: '1px solid var(--border)' }}>
        <button
          onClick={handleSave}
          disabled={!canSave || saving}
          style={{
            width: '100%', padding: '12px', borderRadius: 10, border: 'none',
            background: canSave && !saving ? 'var(--primary)' : '#b0c4c4',
            color: '#fff',
            fontSize: 13, fontWeight: 700, cursor: canSave && !saving ? 'pointer' : 'not-allowed',
            display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
            transition: 'background 0.15s',
          }}
        >
          {saving
            ? <><Loader2 size={14} style={{ animation: 'spin 0.8s linear infinite' }} /> 저장 중...</>
            : isSaved && !isUnchanged
              ? <><CheckCircle2 size={15} /> 변경 저장</>
              : <><CheckCircle2 size={15} /> 저장하기</>
          }
        </button>
      </div>

      {searchOpen && (
        <SearchModal
          type={mapping.mapping_type as 'retailer' | 'product'}
          initialQuery={mapping.ocr_name}
          onSelect={(code, name) => { setSelected({ code, name }); setShowSearch(true) }}
          onClose={() => setSearchOpen(false)}
        />
      )}
    </div>
  )
}

// ── 메인 컴포넌트 ──────────────────────────────────────────────────────────────

export function MappingReview() {
  const navigate = useNavigate()
  const { id: docId } = useParams<{ id: string }>()
  const [mappings, setMappings] = useState<Mapping[]>([])
  const [localSaved, setLocalSaved] = useState<Record<number, { code: string; name: string }>>({})
  const [pendingDocs, setPendingDocs] = useState<{ doc_id: string; pending_count: number }[]>([])
  const [itemIdx, setItemIdx] = useState(0)
  const [loading, setLoading] = useState(true)
  const [startingPhase4, setStartingPhase4] = useState(false)
  const [waitingPhase4, setWaitingPhase4] = useState(false)
  const [phase4Error, setPhase4Error] = useState(false)
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(0)

  useEffect(() => {
    if (!docId) return
    setLoading(true)
    setItemIdx(0)
    setLocalSaved({})

    Promise.all([
      api.getMappings(docId),
      api.listDocuments(),
      api.getDocument(docId),
    ]).then(([maps, docs, doc]) => {
      if (doc.status === 'done') {
        navigate(`/results/${docId}`, { replace: true })
        return
      }
      if (doc.status === 'analyzing') {
        setWaitingPhase4(true)
      }

      setMappings(maps)

      // 이미 확정된 항목을 localSaved 초기값으로
      const initialSaved: Record<number, { code: string; name: string }> = {}
      for (const m of maps) {
        if (m.confirmed_code && m.confirmed_name) {
          initialSaved[m.id] = { code: m.confirmed_code, name: m.confirmed_name }
        }
      }
      setLocalSaved(initialSaved)
      setTotalPages(doc.pages_count ?? 0)

      if (maps.length > 0 && maps[0].page_number) {
        setPage(maps[0].page_number)
      }

      setPendingDocs(
        docs.filter(d => d.status === 'pending').map(d => ({ doc_id: d.doc_id, pending_count: d.pending_count }))
      )
    }).catch(() => {
      navigate('/', { replace: true })
    }).finally(() => setLoading(false))
  }, [docId, navigate])

  // 항목 이동 시 해당 페이지로
  useEffect(() => {
    const pg = mappings[itemIdx]?.page_number
    if (pg) setPage(pg)
  }, [itemIdx, mappings])

  // Phase 4 완료 대기 — SSE
  useEffect(() => {
    if (!waitingPhase4 || !docId) return
    const es = api.streamStatus(docId)
    es.onmessage = (e) => {
      const data = JSON.parse(e.data)
      if (data.status === 'done') {
        es.close()
        navigate(`/results/${docId}`)
      } else if (data.status === 'error') {
        es.close()
        setWaitingPhase4(false)
        setPhase4Error(true)
      }
    }
    return () => es.close()
  }, [waitingPhase4, docId, navigate])

  async function handleSave(code: string, name: string) {
    if (!docId) return
    const m = mappings[itemIdx]
    await api.confirmMapping(docId, m.id, code, name)
    setLocalSaved(prev => ({ ...prev, [m.id]: { code, name } }))
  }

  async function handleStartPhase4() {
    if (!docId) return
    setStartingPhase4(true)
    setPhase4Error(false)
    try {
      await api.confirmAllMappings(docId)
      setWaitingPhase4(true)
    } catch {
      setPhase4Error(true)
    } finally {
      setStartingPhase4(false)
    }
  }

  const savedCount = Object.keys(localSaved).length
  const allSaved = mappings.length === 0 || savedCount === mappings.length

  return (
    <div style={{ display: 'flex', height: '100%', gap: 0 }}>
      {/* 좌: PDF */}
      <div style={{ flex: 1, padding: 20 }}>
        <PdfViewer
          docId={docId}
          page={page}
          totalPages={totalPages}
          highlightText={mappings[itemIdx]?.ocr_name}
          mappingType={mappings[itemIdx]?.mapping_type}
          onPageChange={setPage}
        />
      </div>

      {/* 우: 패널 */}
      <div style={{
        width: 400, borderLeft: '1px solid var(--border)',
        background: 'var(--card)', display: 'flex', flexDirection: 'column',
      }}>
        {/* 문서 탭 */}
        <div style={{ display: 'flex', borderBottom: '1px solid var(--border)', overflowX: 'auto' }}>
          {pendingDocs.map(d => (
            <button
              key={d.doc_id}
              onClick={() => navigate(`/mapping/${d.doc_id}`, { replace: true })}
              style={{
                padding: '12px 16px', fontSize: 12,
                fontWeight: d.doc_id === docId ? 700 : 400,
                color: d.doc_id === docId ? 'var(--primary)' : 'var(--text-2)',
                borderBottom: `2px solid ${d.doc_id === docId ? 'var(--primary)' : 'transparent'}`,
                background: 'transparent', border: 'none', cursor: 'pointer', whiteSpace: 'nowrap',
                display: 'flex', alignItems: 'center', gap: 6,
              }}
            >
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>{d.doc_id}</span>
              <span style={{
                background: '#fdf0e8', color: '#c4622c',
                fontSize: 10, fontWeight: 700, borderRadius: 20, padding: '1px 6px', fontFamily: 'var(--mono)',
              }}>{d.pending_count}</span>
            </button>
          ))}
        </div>

        {/* 진행률 */}
        {mappings.length > 0 && (
          <div style={{ padding: '12px 20px', background: '#f5ede0', borderBottom: '1px solid var(--border)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
              <span style={{ fontSize: 11, color: 'var(--text-2)', fontWeight: 500 }}>저장 진행</span>
              <span style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-1)', fontFamily: 'var(--mono)' }}>
                {savedCount} / {mappings.length}
              </span>
            </div>
            <div style={{ height: 4, background: 'var(--border)', borderRadius: 4, overflow: 'hidden' }}>
              <div style={{
                height: '100%', borderRadius: 4, background: 'var(--primary)',
                width: `${(savedCount / mappings.length) * 100}%`,
                transition: 'width 0.3s',
              }} />
            </div>
          </div>
        )}

        {/* 콘텐츠 */}
        <div style={{ flex: 1, overflow: 'hidden' }}>
          {loading && (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 10, color: 'var(--text-3)' }}>
              <Loader2 size={18} style={{ animation: 'spin 0.8s linear infinite' }} />
              <span style={{ fontSize: 13 }}>로딩 중...</span>
            </div>
          )}

          {!loading && waitingPhase4 && (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 16, padding: 24 }}>
              <Loader2 size={28} style={{ animation: 'spin 0.8s linear infinite', color: 'var(--primary)' }} />
              <div style={{ textAlign: 'center' }}>
                <p style={{ fontSize: 15, fontWeight: 700, color: 'var(--text-1)', marginBottom: 6 }}>Phase 4 NET 계산 중...</p>
                <p style={{ fontSize: 12, color: 'var(--text-2)' }}>완료되면 결과 화면으로 이동합니다</p>
              </div>
            </div>
          )}

          {!loading && !waitingPhase4 && phase4Error && (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 16, padding: 24 }}>
              <AlertCircle size={28} color="#c4622c" />
              <div style={{ textAlign: 'center' }}>
                <p style={{ fontSize: 15, fontWeight: 700, color: 'var(--text-1)', marginBottom: 6 }}>Phase 4 실행 실패</p>
                <p style={{ fontSize: 12, color: 'var(--text-2)', marginBottom: 16 }}>서버 오류가 발생했습니다. 다시 시도해주세요.</p>
              </div>
            </div>
          )}

          {!loading && !waitingPhase4 && !phase4Error && mappings.length === 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 12, padding: 24 }}>
              <CheckCircle2 size={28} color="var(--success)" />
              <p style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-1)', textAlign: 'center' }}>
                검토할 매핑 항목이 없습니다
              </p>
              <p style={{ fontSize: 12, color: 'var(--text-2)', textAlign: 'center' }}>
                아래 버튼으로 Phase 4를 실행하세요
              </p>
            </div>
          )}

          {!loading && !waitingPhase4 && !phase4Error && mappings.length > 0 && (
            <CandidateCard
              key={mappings[itemIdx]?.id}
              mapping={mappings[itemIdx]}
              index={itemIdx}
              total={mappings.length}
              onPrev={() => setItemIdx(i => Math.max(0, i - 1))}
              onNext={() => setItemIdx(i => Math.min(mappings.length - 1, i + 1))}
              onSave={handleSave}
              savedEntry={localSaved[mappings[itemIdx]?.id] ?? null}
            />
          )}
        </div>

        {/* Phase 4 실행 푸터 */}
        {!loading && !waitingPhase4 && (
          <div style={{
            padding: '14px 20px',
            borderTop: `2px solid ${allSaved && !phase4Error ? 'var(--primary)' : 'var(--border)'}`,
            background: allSaved && !phase4Error ? '#eaf4ee' : 'var(--card)',
            display: 'flex', alignItems: 'center', gap: 12,
          }}>
            <div style={{ flex: 1 }}>
              {phase4Error ? (
                <div style={{ fontSize: 11, fontWeight: 600, color: '#c4622c' }}>Phase 4 오류 — 다시 시도하세요</div>
              ) : allSaved ? (
                <div style={{ fontSize: 11, fontWeight: 600, color: '#2d7d4a' }}>모두 저장됨 — Phase 4 실행 가능</div>
              ) : (
                <>
                  <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-2)' }}>
                    {savedCount} / {mappings.length} 저장됨
                  </div>
                  <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 2 }}>
                    미저장 항목이 있으면 Phase 4를 시작할 수 없습니다
                  </div>
                </>
              )}
            </div>
            <button
              onClick={handleStartPhase4}
              disabled={!allSaved || startingPhase4}
              style={{
                padding: '10px 16px', borderRadius: 10, border: 'none',
                background: (allSaved || phase4Error) && !startingPhase4 ? 'var(--primary)' : '#b0c4c4',
                color: '#fff', fontSize: 12, fontWeight: 700,
                cursor: (allSaved || phase4Error) && !startingPhase4 ? 'pointer' : 'not-allowed',
                display: 'flex', alignItems: 'center', gap: 6,
                whiteSpace: 'nowrap',
              }}
            >
              {startingPhase4
                ? <><Loader2 size={13} style={{ animation: 'spin 0.8s linear infinite' }} /> 실행 중...</>
                : phase4Error ? '재시도' : 'Phase 4 실행'
              }
            </button>
          </div>
        )}
      </div>

      <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
    </div>
  )
}
