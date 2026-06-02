import { useState, useEffect, useRef } from 'react'
import { useParams } from 'react-router-dom'
import { CheckCircle2, AlertTriangle, Download, Loader2, ArrowLeft, ArrowRight, Pencil, Search, X } from 'lucide-react'
import { PdfViewer } from '../components/PdfViewer'
import { api, type Phase4Result, type Phase4Row, type RateSummary, type User, type ReviewRecord, type RetailerResult, type DistResult, type ProductResult, type BundleInfo, type BundleXv } from '../api/client'

function fmt(v: number | null | undefined): string {
  if (v == null) return '—'
  return v.toLocaleString()
}

function HoverTooltip({ text, style }: { text: string; style?: React.CSSProperties }) {
  const ref = useRef<HTMLDivElement>(null)
  const [tip, setTip] = useState<{ x: number; y: number } | null>(null)
  return (
    <>
      <div
        ref={ref}
        style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', ...style }}
        onMouseEnter={e => {
          const el = ref.current
          if (el && el.scrollWidth > el.clientWidth) setTip({ x: e.clientX, y: e.clientY })
        }}
        onMouseMove={e => tip && setTip({ x: e.clientX, y: e.clientY })}
        onMouseLeave={() => setTip(null)}
      >
        {text}
      </div>
      {tip && (
        <div style={{
          position: 'fixed', left: tip.x + 10, top: tip.y - 34,
          background: '#1a1a1a', color: '#fff',
          padding: '5px 10px', borderRadius: 5, fontSize: 12,
          whiteSpace: 'pre-wrap', maxWidth: 400, zIndex: 9999,
          pointerEvents: 'none', boxShadow: '0 2px 8px rgba(0,0,0,0.3)',
        }}>
          {text}
        </div>
      )}
    </>
  )
}

function SummaryBar({ s }: { s: RateSummary }) {
  const entries = Object.entries(s.by_rate).sort((a, b) => a[0].localeCompare(b[0]))
  return (
    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
      {entries.map(([rate, amount]) => (
        <div key={rate} style={{
          display: 'flex', alignItems: 'center', gap: 5,
          background: '#f0f4ff', borderRadius: 7, padding: '5px 10px',
          fontSize: 11, fontWeight: 600, color: 'var(--text-2)',
        }}>
          <span>{rate}対象</span>
          <span style={{ fontFamily: 'var(--mono)', color: 'var(--primary)' }}>
            {amount.toLocaleString()}
          </span>
        </div>
      ))}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 5,
        background: 'var(--border)', borderRadius: 7, padding: '5px 10px',
        fontSize: 11, fontWeight: 600, color: 'var(--text-2)',
      }}>
        <span>税抜合計</span>
        <span style={{ fontFamily: 'var(--mono)' }}>{s.total_ex.toLocaleString()}</span>
      </div>
    </div>
  )
}

const typeColor = (t: string) => {
  if (t === '条件')        return { bg: '#ede9e1', color: 'var(--text-2)' }
  if (t.startsWith('CF')) return { bg: '#e8f4f4', color: 'var(--primary)' }
  return { bg: '#eaf4ee', color: 'var(--success)' }
}

function ReviewPill({ label, review, canClick, onClick }: {
  label: string
  review: ReviewRecord | undefined
  canClick: boolean
  onClick: (e: React.MouseEvent) => void
}) {
  const checked = !!review
  const dateStr = review
    ? new Date(review.reviewed_at).toLocaleDateString('ko-KR', { month: '2-digit', day: '2-digit' })
    : ''
  const reviewerDisplay = review
    ? `${review.reviewer_name_ja ?? review.reviewer_name ?? ''} (${review.reviewer_username ?? ''})`
    : ''
  return (
    <button
      onClick={(e) => { e.stopPropagation(); if (canClick) onClick(e) }}
      title={review ? `${reviewerDisplay} · ${dateStr}` : `${label} 미확인`}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 4,
        fontSize: 10, fontWeight: 600,
        padding: '3px 9px', borderRadius: 5,
        cursor: canClick ? 'pointer' : 'default',
        border: checked ? '1px solid #b2f2bb' : '1px solid #dee2e6',
        background: checked ? '#ebfbee' : '#f8f9fa',
        color: checked ? '#2f9e44' : '#adb5bd',
        opacity: !canClick && !checked ? 0.55 : 1,
      }}
    >
      {checked
        ? <CheckCircle2 size={10} />
        : <span style={{ width: 8, height: 8, borderRadius: '50%', border: '1.5px solid #ced4da', display: 'inline-block', flexShrink: 0 }} />
      }
      <span>{label}</span>
      {checked && (
        <span style={{ fontWeight: 400, color: '#51cf66' }}>
          {reviewerDisplay} {dateStr}
        </span>
      )}
    </button>
  )
}

// ── 소매처 검색 모달 ────────────────────────────────────────────────────────────

function RetailerSearchModal({
  initialQuery, onSelect, onClose,
}: {
  initialQuery: string
  onSelect: (code: string, name: string) => void
  onClose: () => void
}) {
  const [q, setQ] = useState(initialQuery)
  const [results, setResults] = useState<RetailerResult[]>([])
  const [searching, setSearching] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { inputRef.current?.focus() }, [])

  useEffect(() => {
    if (!q.trim()) { setResults([]); return }
    setSearching(true)
    const timer = setTimeout(async () => {
      try { setResults(await api.searchRetailer(q)) }
      finally { setSearching(false) }
    }, 300)
    return () => clearTimeout(timer)
  }, [q])

  return (
    <div
      onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}
      style={{
        position: 'fixed', inset: 0, zIndex: 300,
        background: 'rgba(26,21,18,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        backdropFilter: 'blur(2px)',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        onMouseDown={e => e.stopPropagation()}
        style={{
          width: 520, maxHeight: '75vh',
          background: 'var(--card)', borderRadius: 16,
          border: '1px solid var(--border)',
          boxShadow: '0 20px 60px rgba(26,21,18,0.2)',
          display: 'flex', flexDirection: 'column', overflow: 'hidden',
        }}
      >
        <div style={{
          padding: '14px 18px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div>
            <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-1)' }}>소매처 매핑 수정</span>
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2, fontFamily: 'var(--mono)' }}>{initialQuery}</div>
          </div>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-3)', padding: 4 }}>
            <X size={15} />
          </button>
        </div>
        <div style={{ padding: '12px 18px', borderBottom: '1px solid var(--border)', position: 'relative' }}>
          <Search size={14} style={{
            position: 'absolute', left: 32, top: '50%', transform: 'translateY(-50%)',
            color: 'var(--text-3)', pointerEvents: 'none',
          }} />
          <input
            ref={inputRef}
            value={q}
            onChange={e => setQ(e.target.value)}
            placeholder="소매처명"
            style={{
              width: '100%', boxSizing: 'border-box',
              padding: '8px 12px 8px 34px',
              border: '1px solid var(--border)', borderRadius: 8,
              fontSize: 13, outline: 'none',
              background: 'var(--bg)', color: 'var(--text-1)',
            }}
          />
          {searching && (
            <Loader2 size={13} style={{
              position: 'absolute', right: 32, top: '50%', transform: 'translateY(-50%)',
              color: 'var(--text-3)', animation: 'spin 0.8s linear infinite',
            }} />
          )}
        </div>
        <div style={{ flex: 1, overflowY: 'auto' }}>
          {results.length === 0 && !q.trim() && (
            <p style={{ padding: 20, textAlign: 'center', fontSize: 12, color: 'var(--text-3)' }}>소매처명으로 검색하세요</p>
          )}
          {results.length === 0 && q.trim() && !searching && (
            <p style={{ padding: 20, textAlign: 'center', fontSize: 12, color: 'var(--text-3)' }}>검색 결과가 없습니다</p>
          )}
          {results.map((r, i) => (
            <button
              key={r.code}
              onClick={() => { onSelect(r.code, r.name); onClose() }}
              style={{
                width: '100%', textAlign: 'left', padding: '11px 18px',
                borderBottom: i < results.length - 1 ? '1px solid var(--border)' : 'none',
                background: 'transparent', border: 'none', cursor: 'pointer',
                display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12,
              }}
              onMouseEnter={e => (e.currentTarget.style.background = '#f5ede0')}
              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
            >
              <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-1)' }}>{r.name}</span>
              <span style={{
                fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-3)',
                background: '#ede9e1', borderRadius: 6, padding: '3px 8px', flexShrink: 0,
              }}>{r.code}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

function DistSearchModal({
  ocrName, retailerCode, onSelect, onClose,
}: {
  ocrName: string
  retailerCode: string
  onSelect: (code: string, name: string) => void
  onClose: () => void
}) {
  const [q, setQ] = useState('')
  const [candidates, setCandidates] = useState<DistResult[]>([])
  const [loading, setLoading] = useState(true)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { inputRef.current?.focus() }, [])

  useEffect(() => {
    setLoading(true)
    api.getDistCandidates(retailerCode)
      .then(setCandidates)
      .finally(() => setLoading(false))
  }, [retailerCode])

  const filtered = q.trim()
    ? candidates.filter(c => c.name.toLowerCase().includes(q.toLowerCase()) || c.code.includes(q))
    : candidates

  return (
    <div
      onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}
      style={{
        position: 'fixed', inset: 0, zIndex: 300,
        background: 'rgba(26,21,18,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        backdropFilter: 'blur(2px)',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        onMouseDown={e => e.stopPropagation()}
        style={{
          width: 520, maxHeight: '75vh',
          background: 'var(--card)', borderRadius: 16,
          border: '1px solid var(--border)',
          boxShadow: '0 20px 60px rgba(26,21,18,0.2)',
          display: 'flex', flexDirection: 'column', overflow: 'hidden',
        }}
      >
        <div style={{
          padding: '14px 18px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div>
            <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-1)' }}>판매처 매핑 수정</span>
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2, fontFamily: 'var(--mono)' }}>{ocrName}</div>
          </div>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-3)', padding: 4 }}>
            <X size={15} />
          </button>
        </div>
        {candidates.length > 4 && (
          <div style={{ padding: '12px 18px', borderBottom: '1px solid var(--border)', position: 'relative' }}>
            <Search size={14} style={{ position: 'absolute', left: 32, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-3)', pointerEvents: 'none' }} />
            <input
              ref={inputRef}
              value={q}
              onChange={e => setQ(e.target.value)}
              placeholder="판매처명 또는 코드"
              style={{
                width: '100%', boxSizing: 'border-box',
                padding: '8px 12px 8px 34px',
                border: '1px solid var(--border)', borderRadius: 8,
                fontSize: 13, outline: 'none',
                background: 'var(--bg)', color: 'var(--text-1)',
              }}
            />
          </div>
        )}
        <div style={{ flex: 1, overflowY: 'auto' }}>
          {loading && (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24, gap: 8, color: 'var(--text-3)' }}>
              <Loader2 size={14} style={{ animation: 'spin 0.8s linear infinite' }} />
              <span style={{ fontSize: 12 }}>로딩 중...</span>
            </div>
          )}
          {!loading && filtered.length === 0 && (
            <p style={{ padding: 20, textAlign: 'center', fontSize: 12, color: 'var(--text-3)' }}>후보가 없습니다</p>
          )}
          {filtered.map((c, i) => (
            <button
              key={c.code}
              onClick={() => { onSelect(c.code, c.name); onClose() }}
              style={{
                width: '100%', textAlign: 'left', padding: '11px 18px',
                borderBottom: i < filtered.length - 1 ? '1px solid var(--border)' : 'none',
                background: 'transparent', border: 'none', cursor: 'pointer',
                display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12,
              }}
              onMouseEnter={e => (e.currentTarget.style.background = '#f5ede0')}
              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
            >
              <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-1)' }}>{c.name}</span>
              <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-3)', background: '#ede9e1', borderRadius: 6, padding: '3px 8px', flexShrink: 0 }}>{c.code}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

function ProductSearchModal({
  productOcr, onSelect, onClose,
}: {
  productOcr: string
  onSelect: (code: string, name: string) => void
  onClose: () => void
}) {
  const [q, setQ] = useState(productOcr)
  const [results, setResults] = useState<ProductResult[]>([])
  const [searching, setSearching] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { inputRef.current?.focus() }, [])

  useEffect(() => {
    if (!q.trim()) { setResults([]); return }
    setSearching(true)
    const timer = setTimeout(async () => {
      try { setResults(await api.searchProduct(q)) }
      finally { setSearching(false) }
    }, 300)
    return () => clearTimeout(timer)
  }, [q])

  return (
    <div
      onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}
      style={{
        position: 'fixed', inset: 0, zIndex: 300,
        background: 'rgba(26,21,18,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        backdropFilter: 'blur(2px)',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        onMouseDown={e => e.stopPropagation()}
        style={{
          width: 560, maxHeight: '75vh',
          background: 'var(--card)', borderRadius: 16,
          border: '1px solid var(--border)',
          boxShadow: '0 20px 60px rgba(26,21,18,0.2)',
          display: 'flex', flexDirection: 'column', overflow: 'hidden',
        }}
      >
        <div style={{
          padding: '14px 18px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div>
            <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-1)' }}>제품 매핑 수정</span>
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2, fontFamily: 'var(--mono)' }}>{productOcr}</div>
          </div>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-3)', padding: 4 }}>
            <X size={15} />
          </button>
        </div>
        <div style={{ padding: '12px 18px', borderBottom: '1px solid var(--border)', position: 'relative' }}>
          <Search size={14} style={{ position: 'absolute', left: 32, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-3)', pointerEvents: 'none' }} />
          <input
            ref={inputRef}
            value={q}
            onChange={e => setQ(e.target.value)}
            placeholder="제품명 또는 JAN코드"
            style={{
              width: '100%', boxSizing: 'border-box',
              padding: '8px 12px 8px 34px',
              border: '1px solid var(--border)', borderRadius: 8,
              fontSize: 13, outline: 'none',
              background: 'var(--bg)', color: 'var(--text-1)',
            }}
          />
          {searching && (
            <Loader2 size={13} style={{ position: 'absolute', right: 32, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-3)', animation: 'spin 0.8s linear infinite' }} />
          )}
        </div>
        <div style={{ flex: 1, overflowY: 'auto' }}>
          {results.length === 0 && !q.trim() && (
            <p style={{ padding: 20, textAlign: 'center', fontSize: 12, color: 'var(--text-3)' }}>제품명으로 검색하세요</p>
          )}
          {results.length === 0 && q.trim() && !searching && (
            <p style={{ padding: 20, textAlign: 'center', fontSize: 12, color: 'var(--text-3)' }}>검색 결과가 없습니다</p>
          )}
          {results.map((r, i) => (
            <button
              key={r.code}
              onClick={() => { onSelect(r.code, r.name); onClose() }}
              style={{
                width: '100%', textAlign: 'left', padding: '11px 18px',
                borderBottom: i < results.length - 1 ? '1px solid var(--border)' : 'none',
                background: 'transparent', border: 'none', cursor: 'pointer',
              }}
              onMouseEnter={e => (e.currentTarget.style.background = '#f5ede0')}
              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
                <HoverTooltip text={r.name} style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-1)' }} />
                <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-3)', background: '#ede9e1', borderRadius: 6, padding: '3px 8px', flexShrink: 0 }}>{r.code}</span>
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '3px 10px', marginTop: 4 }}>
                {r.volume && <span style={{ fontSize: 10, color: 'var(--text-3)' }}><span style={{ opacity: 0.6 }}>용량 </span>{r.volume}</span>}
                {r.spec   && <span style={{ fontSize: 10, color: 'var(--text-3)' }}><span style={{ opacity: 0.6 }}>규격 </span>{r.spec}</span>}
                {r.jan    && <span style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}><span style={{ fontFamily: 'inherit', opacity: 0.6 }}>JAN </span>{r.jan}</span>}
              </div>
              {(r.sikiri != null || r.honbucho != null) && (
                <div style={{ display: 'flex', gap: 16, marginTop: 4 }}>
                  {r.sikiri   != null && (
                    <span style={{ fontSize: 11, color: 'var(--text-2)', fontFamily: 'var(--mono)', display: 'flex', gap: 4, alignItems: 'baseline' }}>
                      <span style={{ fontFamily: 'inherit', fontSize: 10, opacity: 0.55, fontWeight: 600 }}>仕切</span>
                      <span>{r.sikiri.toLocaleString()}</span>
                    </span>
                  )}
                  {r.honbucho != null && (
                    <span style={{ fontSize: 11, color: 'var(--text-2)', fontFamily: 'var(--mono)', display: 'flex', gap: 4, alignItems: 'baseline' }}>
                      <span style={{ fontFamily: 'inherit', fontSize: 10, opacity: 0.55, fontWeight: 600 }}>本部長</span>
                      <span>{r.honbucho.toLocaleString()}</span>
                    </span>
                  )}
                </div>
              )}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

function groupByJishoThenCustomerThenProduct(rows: Phase4Row[]) {
  const jishoMap = new Map<string, Phase4Row[]>()
  for (const row of rows) {
    const jk = row.jisho || '—'
    if (!jishoMap.has(jk)) jishoMap.set(jk, [])
    jishoMap.get(jk)!.push(row)
  }
  return Array.from(jishoMap.entries()).map(([jisho, jishoRows]) => {
    const custMap = new Map<string, Phase4Row[]>()
    for (const row of jishoRows) {
      const key = row.customer_ocr || '—'
      if (!custMap.has(key)) custMap.set(key, [])
      custMap.get(key)!.push(row)
    }
    const customers = Array.from(custMap.entries()).map(([custName, custRows]) => {
      const prodMap = new Map<string, Phase4Row[]>()
      for (const row of custRows) {
        const pk = row.商品コード || row.product_ocr || '—'
        if (!prodMap.has(pk)) prodMap.set(pk, [])
        prodMap.get(pk)!.push(row)
      }
      return {
        custName,
        custSubtotal: custRows.reduce((s, r) => s + (r['未収金額合計'] ?? 0), 0),
        products: Array.from(prodMap.entries()).map(([pk, pr]) => {
          const condMap = new Map<string, { qty: number; amount: number; hasWarn: boolean }>()
          for (const row of pr) {
            const ct = row.condition_type || '—'
            const ex = condMap.get(ct) ?? { qty: 0, amount: 0, hasWarn: false }
            condMap.set(ct, {
              qty: ex.qty + (row.個数計 ?? 0),
              amount: ex.amount + (row['未収金額合計'] ?? 0),
              hasWarn: ex.hasWarn || !!(row.unconfirmed || row.net_lt_honbu),
            })
          }
          return {
            pk,
            productCode: pr[0].商品コード || '',
            productOcr:  pr[0].product_ocr || '',
            productName: pr[0].商品名 || '',
            prodSubtotal: pr.reduce((s, r) => s + (r['未収金額合計'] ?? 0), 0),
            hasWarn: pr.some(r => r.unconfirmed || r.net_lt_honbu),
            conditionGroups: Array.from(condMap.entries()).map(([ct, v]) => ({ conditionType: ct, ...v })),
          }
        }),
      }
    })
    return {
      jisho,
      jishoSubtotal: jishoRows.reduce((s, r) => s + (r['未収金額合計'] ?? 0), 0),
      customers,
    }
  })
}

// 代表スーパー(소매처코드)가 있으면 그걸로 그룹핑 → OCR 표기 흔들림 방지
function groupRows(rows: Phase4Row[]): Map<string, Phase4Row[]> {
  const map = new Map<string, Phase4Row[]>()
  for (const r of rows) {
    const key = r.代表スーパー || r.customer_ocr || r.スーパー || '—'
    if (!map.has(key)) map.set(key, [])
    map.get(key)!.push(r)
  }
  return map
}

export function Results() {
  const { id: docId } = useParams<{ id: string }>()
  const [result, setResult] = useState<Phase4Result | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [stage, setStage] = useState<1 | 2>(1)
  const [selectedCustomer, setSelectedCustomer] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(0)
  const [user, setUser] = useState<User | null>(null)
  const [reviews, setReviews] = useState<ReviewRecord[]>([])
  const [confirmed, setConfirmed] = useState(false)
  const [filterAssigneeId, setFilterAssigneeId] = useState<string | null>(null)
  const [detailTab, setDetailTab] = useState<'detail' | 'aggregate'>('detail')
  const [remapTarget, setRemapTarget] = useState<{ ocrName: string } | null>(null)
  const [distRemapTarget, setDistRemapTarget] = useState<{ ocrName: string; retailerCode: string } | null>(null)
  const [productRemapTarget, setProductRemapTarget] = useState<{ productOcr: string } | null>(null)
  const [remapping, setRemapping] = useState(false)
  const [selectedBundle, setSelectedBundle] = useState<number | null>(null)
  const [rightWidth, setRightWidth] = useState(560)
  const isDragging = useRef(false)
  const dragStartX = useRef(0)
  const dragStartWidth = useRef(0)

  useEffect(() => {
    const onMouseMove = (e: MouseEvent) => {
      if (!isDragging.current) return
      const delta = dragStartX.current - e.clientX
      setRightWidth(Math.max(400, Math.min(1000, dragStartWidth.current + delta)))
    }
    const onMouseUp = () => { isDragging.current = false; document.body.style.cursor = '' }
    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseup', onMouseUp)
    return () => {
      window.removeEventListener('mousemove', onMouseMove)
      window.removeEventListener('mouseup', onMouseUp)
    }
  }, [])

  useEffect(() => {
    if (!docId) return
    setLoading(true)
    Promise.all([
      api.getResults(docId),
      api.getDocument(docId),
      api.me(),
      api.getReviews(docId),
      api.recheckConfirm(docId),
    ]).then(([res, doc, me, revs, recheckResult]) => {
      setResult(res)
      setTotalPages(doc.pages_count ?? 0)
      setUser(me)
      setReviews(revs)
      setConfirmed(!!(doc.confirmed_at || recheckResult.doc_confirmed))
      if (me.role === '영업사원') {
        setFilterAssigneeId(me.username)
      }
    }).catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [docId])

  if (loading) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 10, color: 'var(--text-3)' }}>
        <Loader2 size={20} style={{ animation: 'spin 0.8s linear infinite' }} />
        <span>결과 로딩 중...</span>
        <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
      </div>
    )
  }

  if (error || !result) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', flexDirection: 'column', gap: 12 }}>
        <AlertTriangle size={24} color="#c4622c" />
        <p style={{ fontSize: 14, color: 'var(--text-2)' }}>{error ?? '결과를 불러올 수 없습니다'}</p>
      </div>
    )
  }

  const bundles = result.bundles ?? []
  const activeRows = selectedBundle != null && bundles.length > 1
    ? result.rows.filter(r => {
        const b = bundles[selectedBundle]
        return r.page_number != null && r.page_number >= b.page_range[0] && r.page_number <= b.page_range[1]
      })
    : result.rows

  const showSections = new Set(result.show_sections ?? ['rate_summary', 'xv', 'retailer'])
  const aggregateLabel = result.aggregate_label ?? '소매처별 집계'

  const activeXv: typeof result.xv =
    selectedBundle != null && result.bundle_xv
      ? (result.bundle_xv.find((b: BundleXv) => b.bundle_idx === selectedBundle)?.xv ?? result.xv)
      : result.xv

  const grouped     = groupRows(activeRows)
  const totalAmount = activeRows.reduce((s, r) => s + (r['未収金額合計'] ?? 0), 0)
  const warnCount   = result.xv.filter(v => !v.ok).length
  const hasWarning  = warnCount > 0

  const handleRemap = async (ocrName: string, retailerCode: string, retailerName: string) => {
    if (!docId) return
    setRemapping(true)
    try {
      await api.remapRetailer(docId, ocrName, retailerCode, retailerName)
      const [res, revs] = await Promise.all([api.getResults(docId), api.getReviews(docId)])
      setResult(res)
      setReviews(revs)
    } catch (e: unknown) {
      alert(`소매처 수정 실패: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setRemapping(false)
    }
  }

  const handleRemapDist = async (ocrName: string, distCode: string, distName: string) => {
    if (!docId) return
    setRemapping(true)
    try {
      await api.remapDist(docId, ocrName, distCode, distName)
      const [res, revs] = await Promise.all([api.getResults(docId), api.getReviews(docId)])
      setResult(res)
      setReviews(revs)
    } catch (e: unknown) {
      alert(`판매처 수정 실패: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setRemapping(false)
    }
  }

  const handleRemapProduct = async (productOcr: string, productCode: string, productName: string) => {
    if (!docId) return
    setRemapping(true)
    try {
      await api.remapProduct(docId, productOcr, productCode, productName)
      const [res, revs] = await Promise.all([api.getResults(docId), api.getReviews(docId)])
      setResult(res)
      setReviews(revs)
    } catch (e: unknown) {
      alert(`제품 수정 실패: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setRemapping(false)
    }
  }

  const toggleReview = async (e: React.MouseEvent, retailerCode: string, reviewType: '1차' | '2차') => {
    e.stopPropagation()
    if (!docId || !retailerCode) return
    const existing = reviews.find(r => r.retailer_code === retailerCode && r.review_type === reviewType)
    try {
      if (existing) {
        await api.unmarkReviewed(docId, retailerCode, reviewType)
        setReviews(prev => prev.filter(r => !(r.retailer_code === retailerCode && r.review_type === reviewType)))
      } else {
        const newR = await api.markReviewed(docId, retailerCode, reviewType)
        setReviews(prev => [...prev, newR])
        if (newR.doc_confirmed) setConfirmed(true)
      }
    } catch { /* ignore */ }
  }

  const allEntries = Array.from(grouped.entries())

  // 담당자 드롭다운 옵션 — 담당者ID/担当者 쌍 (중복 제거)
  const assignees: { id: string; name: string }[] = []
  if (result) {
    const seen = new Set<string>()
    for (const row of result.rows) {
      const id = row['担当者ID'] || ''
      if (id && !seen.has(id)) {
        seen.add(id)
        assignees.push({ id, name: row['担当者'] || id })
      }
    }
  }

  const filteredEntries = filterAssigneeId
    ? allEntries.filter(([, rows]) =>
        (rows[0]?.['担当者ID'] ?? '').split('·').includes(filterAssigneeId)
      )
    : allEntries

  const filteredFlatRows = filterAssigneeId
    ? activeRows.filter(r => (r['担当者ID'] ?? '').split('·').includes(filterAssigneeId))
    : activeRows

  const hasJisho = filteredFlatRows.some(r => !!r.jisho)

  // 소매처별 소계 (Stage 1 집계용)
  const retailerTotals = Array.from(grouped.entries()).map(([groupKey, rows]) => ({
    groupKey,
    label:    rows[0]?.代表スーパー ? (rows[0]?.スーパー ?? groupKey) : groupKey,
    code:     rows[0]?.代表スーパー ?? '',
    subtotal: rows.reduce((s, r) => s + (r['未収金額合計'] ?? 0), 0),
    hasWarn:  rows.some(r => r.unconfirmed || r.net_lt_honbu),
  }))

  return (
    <div style={{ display: 'flex', height: '100%', overflow: 'hidden' }}>

      {/* 좌: PDF — 항상 표시 */}
      <div style={{ flex: 1, minWidth: 280, padding: 20, overflow: 'hidden' }}>
        <PdfViewer
          docId={docId}
          page={page}
          totalPages={totalPages}
          highlightText={selectedCustomer ?? undefined}
          onPageChange={setPage}
        />
      </div>

      {/* 드래그 핸들 */}
      <div
        onMouseDown={e => {
          isDragging.current = true
          dragStartX.current = e.clientX
          dragStartWidth.current = rightWidth
          document.body.style.cursor = 'col-resize'
          e.preventDefault()
        }}
        style={{
          width: 5, flexShrink: 0, cursor: 'col-resize',
          background: 'var(--border)', transition: 'background 0.15s',
        }}
        onMouseEnter={e => (e.currentTarget.style.background = 'var(--primary)')}
        onMouseLeave={e => (e.currentTarget.style.background = 'var(--border)')}
      />

      {/* 우: 패널 (Stage에 따라 내용 전환) */}
      <div style={{
        width: rightWidth, flexShrink: 0,
        background: 'var(--card)', display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>

        {/* ── Stage 1: 요약 ─────────────────────────────────── */}
        {stage === 1 && <>
          {/* 헤더 */}
          <div style={{ padding: '16px 24px', borderBottom: '1px solid var(--border)' }}>
            <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 8 }}>
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}>
                  <h2 style={{ fontSize: 14, fontWeight: 700, color: 'var(--text-1)', margin: 0 }}>분석 결과</h2>
                  {confirmed && (
                    <span style={{
                      display: 'inline-flex', alignItems: 'center', gap: 5,
                      background: '#e0f0f0', color: 'var(--primary)',
                      borderRadius: 20, padding: '3px 9px', fontSize: 11, fontWeight: 600,
                    }}>
                      <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--primary)', flexShrink: 0 }} />
                      확정
                    </span>
                  )}
                </div>
                <p style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--mono)', margin: 0 }}>
                  {docId} · {result.form_id}
                </p>
              </div>
              <div style={{ display: 'flex', gap: 7, flexShrink: 0 }}>
                <button style={{
                  display: 'flex', alignItems: 'center', gap: 5,
                  fontSize: 11, color: 'var(--text-2)', border: '1px solid var(--border)',
                  background: 'var(--card)', borderRadius: 7, padding: '5px 11px', cursor: 'pointer',
                }}>
                  <Download size={12} />
                  내보내기
                </button>
                <button
                  onClick={() => setStage(2)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 5,
                    fontSize: 11, fontWeight: 600, color: '#fff',
                    border: 'none', background: 'var(--primary)',
                    borderRadius: 7, padding: '5px 12px', cursor: 'pointer',
                  }}
                >
                  상세 보기
                  <ArrowRight size={12} />
                </button>
              </div>
            </div>
          </div>

          {/* 번들 탭 스트립 — 다중 번들 문서만 표시 */}
          {bundles.length > 1 && (
            <div style={{
              display: 'flex', borderBottom: '1px solid var(--border)',
              background: 'var(--card)', flexShrink: 0, overflowX: 'auto',
            }}>
              <button
                onClick={() => { setSelectedBundle(null) }}
                style={{
                  padding: '7px 14px', fontSize: 11, fontWeight: 600, flexShrink: 0,
                  border: 'none', background: 'transparent', cursor: 'pointer', whiteSpace: 'nowrap',
                  borderBottom: selectedBundle === null ? '2px solid var(--primary)' : '2px solid transparent',
                  color: selectedBundle === null ? 'var(--primary)' : 'var(--text-3)',
                }}
              >
                전체
              </button>
              {bundles.map((b: BundleInfo, idx: number) => {
                const bundleRows = result.rows.filter(r =>
                  r.page_number != null && r.page_number >= b.page_range[0] && r.page_number <= b.page_range[1]
                )
                const jishoLabel = bundleRows[0]?.jisho ?? `묶음 ${idx + 1}`
                return (
                  <button
                    key={idx}
                    onClick={() => {
                      setSelectedBundle(idx)
                      setPage(b.cover_page)
                    }}
                    style={{
                      padding: '7px 14px', fontSize: 11, fontWeight: 600, flexShrink: 0,
                      border: 'none', background: 'transparent', cursor: 'pointer', whiteSpace: 'nowrap',
                      borderBottom: selectedBundle === idx ? '2px solid var(--primary)' : '2px solid transparent',
                      color: selectedBundle === idx ? 'var(--primary)' : 'var(--text-3)',
                    }}
                  >
                    {jishoLabel}
                  </button>
                )
              })}
            </div>
          )}

          <div style={{ flex: 1, overflowY: 'auto', padding: '16px 24px', display: 'flex', flexDirection: 'column', gap: 16 }}>

            {/* 세율별 집계 */}
            {showSections.has('rate_summary') && result.summary && (
              <section>
                <p style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-3)', margin: '0 0 8px', letterSpacing: '0.05em', textTransform: 'uppercase' }}>
                  세율별 집계
                </p>
                <SummaryBar s={result.summary} />
              </section>
            )}

            {/* 교차검증 */}
            {showSections.has('xv') && <section>
              <p style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-3)', margin: '0 0 8px', letterSpacing: '0.05em', textTransform: 'uppercase' }}>
                교차검증
              </p>
              {activeXv.length === 0 ? (
                result.xv_error ? (
                  <div style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    background: '#fdf0e8', border: '1px solid #dbb590',
                    borderRadius: 8, padding: '7px 12px',
                    fontSize: 11, fontWeight: 600, color: '#c4622c',
                  }}>
                    <AlertTriangle size={13} />
                    <span>교차검증 파싱 오류 — 수동 확인 필요</span>
                  </div>
                ) : (
                  <div style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    background: '#eaf4ee', borderRadius: 8, padding: '7px 12px',
                    fontSize: 11, fontWeight: 600, color: '#2d7d4a',
                  }}>
                    <CheckCircle2 size={13} />
                    <span>합계 {totalAmount.toLocaleString()}¥</span>
                  </div>
                )
              ) : (
                <div style={{ border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
                  {activeXv.map((v, i) => (
                    <div key={i} style={{
                      display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12,
                      padding: '7px 12px',
                      background: v.ok ? '#f6fbf7' : '#fdf0e8',
                      borderBottom: i < activeXv.length - 1 ? '1px solid var(--border)' : 'none',
                    }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
                        {v.ok
                          ? <CheckCircle2 size={12} color="#2d7d4a" style={{ flexShrink: 0 }} />
                          : <AlertTriangle size={12} color="#c4622c" style={{ flexShrink: 0 }} />
                        }
                        <span style={{
                          fontSize: 11, fontWeight: 600,
                          color: v.ok ? '#2d7d4a' : '#c4622c',
                          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                        }}>{v.label}</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0, fontFamily: 'var(--mono)', fontSize: 11 }}>
                        <span style={{ color: 'var(--text-1)', fontWeight: 700 }}>
                          {v.actual?.toLocaleString() ?? '—'}
                        </span>
                        {v.expected != null && (
                          <>
                            <span style={{ color: 'var(--text-3)', fontSize: 10 }}>vs</span>
                            <span style={{ color: v.ok ? 'var(--text-3)' : '#c4622c', fontWeight: v.ok ? 400 : 700 }}>
                              {v.expected.toLocaleString()}
                            </span>
                          </>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>}

            {/* 소매처별 집계 */}
            {showSections.has('retailer') && <section>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                <p style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-3)', margin: 0, letterSpacing: '0.05em', textTransform: 'uppercase' }}>
                  {aggregateLabel}
                </p>
                <span style={{ fontSize: 10, color: 'var(--text-3)' }}>
                  {grouped.size}개 · {result.rows.length}건
                </span>
              </div>
              <div style={{ border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
                {retailerTotals.map(({ groupKey, label, code, subtotal, hasWarn }, i) => (
                  <div
                    key={groupKey}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 8,
                      padding: '10px 14px',
                      borderBottom: i < retailerTotals.length - 1 ? '1px solid var(--border)' : 'none',
                      background: hasWarn ? '#fdf5ee' : 'var(--card)',
                    }}
                  >
                    {hasWarn && <AlertTriangle size={12} color="#c4622c" style={{ flexShrink: 0 }} />}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {label}
                      </div>
                      {code && (
                        <div style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}>{code}</div>
                      )}
                    </div>
                    <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--primary)', fontFamily: 'var(--mono)', flexShrink: 0 }}>
                      {subtotal.toLocaleString()}¥
                    </span>
                  </div>
                ))}
              </div>
            </section>}

          </div>
        </>}

        {/* ── Stage 2: 상세 ─────────────────────────────────── */}
        {stage === 2 && <>
          {/* 헤더 */}
          <div style={{
            padding: '12px 20px 12px 16px', borderBottom: '1px solid var(--border)',
            display: 'flex', alignItems: 'center', gap: 10,
          }}>
            <button
              onClick={() => setStage(1)}
              style={{
                display: 'flex', alignItems: 'center', gap: 4,
                fontSize: 11, color: 'var(--text-3)', border: '1px solid var(--border)',
                background: 'var(--card)', borderRadius: 6, padding: '4px 10px',
                cursor: 'pointer', flexShrink: 0,
              }}
            >
              <ArrowLeft size={11} />
              요약
            </button>
            <div style={{ flex: 1, minWidth: 0 }}>
              <p style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-1)', margin: 0 }}>상세 내역</p>
              <p style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--mono)', margin: 0 }}>
                소매처 {filteredEntries.length}개 · {activeRows.length}건
              </p>
            </div>
            {assignees.length > 0 && (
              <select
                value={filterAssigneeId ?? ''}
                onChange={e => setFilterAssigneeId(e.target.value || null)}
                style={{
                  fontSize: 11, padding: '4px 8px', borderRadius: 6,
                  border: '1px solid var(--border)', cursor: 'pointer',
                  background: filterAssigneeId ? 'var(--primary)' : 'var(--card)',
                  color: filterAssigneeId ? '#fff' : 'var(--text-2)',
                  flexShrink: 0,
                }}
              >
                <option value="">전체</option>
                {assignees.map(a => (
                  <option key={a.id} value={a.id}>{a.name}</option>
                ))}
              </select>
            )}
            {hasWarning && (
              <div style={{
                display: 'flex', alignItems: 'center', gap: 4,
                fontSize: 11, color: '#c4622c', fontWeight: 600, flexShrink: 0,
              }}>
                <AlertTriangle size={12} />
                {warnCount}건
              </div>
            )}
          </div>

          {/* 번들 탭 스트립 (Stage 2) */}
          {bundles.length > 1 && (
            <div style={{
              display: 'flex', borderBottom: '1px solid var(--border)',
              background: 'var(--card)', flexShrink: 0, overflowX: 'auto',
            }}>
              <button
                onClick={() => setSelectedBundle(null)}
                style={{
                  padding: '7px 14px', fontSize: 11, fontWeight: 600, flexShrink: 0,
                  border: 'none', background: 'transparent', cursor: 'pointer', whiteSpace: 'nowrap',
                  borderBottom: selectedBundle === null ? '2px solid var(--primary)' : '2px solid transparent',
                  color: selectedBundle === null ? 'var(--primary)' : 'var(--text-3)',
                }}
              >
                전체
              </button>
              {bundles.map((b: BundleInfo, idx: number) => {
                const bundleRows = result.rows.filter(r =>
                  r.page_number != null && r.page_number >= b.page_range[0] && r.page_number <= b.page_range[1]
                )
                const jishoLabel = bundleRows[0]?.jisho ?? `묶음 ${idx + 1}`
                return (
                  <button
                    key={idx}
                    onClick={() => {
                      setSelectedBundle(idx)
                      setPage(b.cover_page)
                    }}
                    style={{
                      padding: '7px 14px', fontSize: 11, fontWeight: 600, flexShrink: 0,
                      border: 'none', background: 'transparent', cursor: 'pointer', whiteSpace: 'nowrap',
                      borderBottom: selectedBundle === idx ? '2px solid var(--primary)' : '2px solid transparent',
                      color: selectedBundle === idx ? 'var(--primary)' : 'var(--text-3)',
                    }}
                  >
                    {jishoLabel}
                  </button>
                )
              })}
            </div>
          )}

          {/* 탭 스트립 */}
          <div style={{
            display: 'flex', borderBottom: '1px solid var(--border)',
            background: 'var(--card)', flexShrink: 0,
          }}>
            {(['detail', 'aggregate'] as const).map(tab => (
              <button
                key={tab}
                onClick={() => setDetailTab(tab)}
                style={{
                  padding: '7px 16px', fontSize: 11, fontWeight: 600,
                  border: 'none', background: 'transparent', cursor: 'pointer',
                  borderBottom: detailTab === tab ? '2px solid var(--primary)' : '2px solid transparent',
                  color: detailTab === tab ? 'var(--primary)' : 'var(--text-3)',
                }}
              >
                {tab === 'detail' ? '행별 상세' : '제품별 집계'}
              </button>
            ))}
          </div>

          {/* 집계 탭 — CVS 체인 → 제품 → 조건 3단 구조 */}
          {detailTab === 'aggregate' && (
            <div style={{ flex: 1, overflowY: 'auto' }}>
              {filteredEntries.map(([groupKey, rows]) => {
                const subtotal     = rows.reduce((s, r) => s + (r['未収金額合計'] ?? 0), 0)
                const hasWarn      = rows.some(r => r.unconfirmed || r.net_lt_honbu)
                const retailer     = rows[0]?.スーパー ?? '—'
                const retailerCode = rows[0]?.代表スーパー ?? ''
                const dist         = rows[0]?.受注先 ?? '—'
                const distCode     = rows[0]?.受注先コード ?? ''
                const ocrName      = rows[0]?.customer_ocr || groupKey
                const review1      = reviews.find(r => r.retailer_code === retailerCode && r.review_type === '1차')
                const review2      = reviews.find(r => r.retailer_code === retailerCode && r.review_type === '2차')
                const jishoGroups  = groupByJishoThenCustomerThenProduct(rows)
                const firstPage    = rows.reduce<number | null>((min, r) => {
                  const p = r.page_number
                  return (p != null && (min === null || p < min)) ? p : min
                }, null)

                return (
                  <div key={groupKey} style={{ borderBottom: '1px solid var(--border)', background: hasWarn ? '#fdf5ee' : 'var(--card)' }}>
                    {/* 소매처 헤더 (detail 탭과 동일) */}
                    <div
                      onClick={() => {
                        const isSelecting = selectedCustomer !== ocrName
                        setSelectedCustomer(prev => prev === ocrName ? null : ocrName)
                        if (isSelecting && firstPage != null) setPage(firstPage)
                      }}
                      style={{ cursor: 'pointer', padding: '14px 24px', borderBottom: '1px solid var(--border)' }}
                    >
                      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 8 }}>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
                            {hasWarn && <AlertTriangle size={13} color="#c4622c" style={{ flexShrink: 0 }} />}
                            <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-1)' }}>{ocrName}</span>
                          </div>
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                              <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#339af0', flexShrink: 0 }} />
                              <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-3)' }}>{retailerCode || '—'}</span>
                              <span style={{ fontSize: 11, color: 'var(--text-2)' }}>{retailer}</span>
                              <button onClick={e => { e.stopPropagation(); setRemapTarget({ ocrName }) }} title="소매처 수정" style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '0 1px', color: 'var(--text-3)', display: 'inline-flex', alignItems: 'center' }}>
                                <Pencil size={10} />
                              </button>
                            </div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                              <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#fd7e14', flexShrink: 0 }} />
                              <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-3)' }}>{distCode || '—'}</span>
                              <span style={{ fontSize: 11, color: 'var(--text-2)' }}>{dist}</span>
                              <button onClick={e => { e.stopPropagation(); setDistRemapTarget({ ocrName, retailerCode }) }} title="판매처 수정" style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '0 1px', color: 'var(--text-3)', display: 'inline-flex', alignItems: 'center' }}>
                                <Pencil size={10} />
                              </button>
                            </div>
                          </div>
                        </div>
                        <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--primary)', fontFamily: 'var(--mono)', flexShrink: 0 }}>
                          {subtotal.toLocaleString()}¥
                        </span>
                      </div>
                      {retailerCode && (
                        <div style={{ display: 'flex', gap: 6, marginTop: 10 }} onClick={e => e.stopPropagation()}>
                          <ReviewPill label="1차" review={review1} canClick={!confirmed && (!review1 || review1.reviewer_id === user?.user_id)} onClick={e => toggleReview(e, retailerCode, '1차')} />
                          <ReviewPill label="2차" review={review2} canClick={!confirmed && (!review2 || review2.reviewer_id === user?.user_id)} onClick={e => toggleReview(e, retailerCode, '2차')} />
                        </div>
                      )}
                    </div>

                    {/* 入出荷支店 → 得意先 → 제품 → 조건 */}
                    {jishoGroups.map(({ jisho, jishoSubtotal, customers }) => (
                      <div key={jisho}>
                        <div style={{
                          padding: '4px 24px', background: '#f1f3f5',
                          borderBottom: '1px solid var(--border)',
                          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                        }}>
                          <span style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-3)', letterSpacing: '0.03em' }}>{jisho}</span>
                          <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-3)' }}>
                            {jishoSubtotal.toLocaleString()}
                          </span>
                        </div>
                        {customers.map(({ custName, custSubtotal, products }) => (
                        <div key={custName}>
                        <div style={{
                          padding: '5px 24px 5px 32px', background: '#f8f9fa',
                          borderBottom: '1px solid #f1f3f5',
                          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                        }}>
                          <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-2)' }}>{custName}</span>
                          <span style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--primary)', fontWeight: 600 }}>
                            {custSubtotal.toLocaleString()}
                          </span>
                        </div>
                        <div style={{ padding: '6px 24px 10px' }}>
                          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                            <thead>
                              <tr style={{ color: 'var(--text-3)' }}>
                                {(['조건', '수량', '금액'] as const).map((h, i) => (
                                  <th key={h} style={{
                                    padding: i === 0 ? '4px 4px' : '4px 8px',
                                    fontWeight: 600, fontSize: 10,
                                    textAlign: i === 0 ? 'left' : 'right',
                                    letterSpacing: '0.04em',
                                    borderBottom: '1px solid var(--border)',
                                    whiteSpace: 'nowrap',
                                  }}>{h}</th>
                                ))}
                              </tr>
                            </thead>
                            <tbody>
                              {products.map(({ pk, productCode, productOcr, productName, prodSubtotal, hasWarn, conditionGroups }) => (
                                <>
                                  {/* 제품 헤더 행 */}
                                  <tr key={`${pk}_h`} style={{ borderTop: '1px solid #f1f3f5' }}>
                                    <td colSpan={2} style={{ padding: '5px 4px', fontWeight: 600 }}>
                                      {hasWarn && <AlertTriangle size={10} color="#c4622c" style={{ marginRight: 4, verticalAlign: 'middle' }} />}
                                      <span style={{ color: 'var(--text-1)' }}>{productOcr || productName || '—'}</span>
                                      {productCode && <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-3)', marginLeft: 6 }}>{productCode}</span>}
                                      {productName && productOcr !== productName && <span style={{ fontSize: 10, color: 'var(--text-3)', marginLeft: 4 }}>{productName}</span>}
                                      <button onClick={e => { e.stopPropagation(); setProductRemapTarget({ productOcr: productOcr || productName || '' }) }} title="제품 매핑 수정" style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '0 2px', color: 'var(--text-3)', display: 'inline-flex', alignItems: 'center', verticalAlign: 'middle' }}>
                                        <Pencil size={9} />
                                      </button>
                                    </td>
                                    <td style={{ padding: '5px 0', textAlign: 'right', fontWeight: 700, fontFamily: 'var(--mono)', color: 'var(--primary)', fontSize: 12 }}>
                                      {prodSubtotal.toLocaleString()}
                                    </td>
                                  </tr>
                                  {/* condition_type별 집계 행 */}
                                  {conditionGroups.map(({ conditionType, qty, amount, hasWarn: cWarn }) => (
                                    <tr key={conditionType} style={{ background: cWarn ? '#fff8e1' : 'transparent' }}>
                                      <td style={{ padding: '3px 4px 3px 12px', color: 'var(--text-2)' }}>{conditionType}</td>
                                      <td style={{ padding: '3px 8px', textAlign: 'right', fontFamily: 'var(--mono)', color: 'var(--text-2)' }}>{qty.toLocaleString()}</td>
                                      <td style={{ padding: '3px 0', textAlign: 'right', fontFamily: 'var(--mono)', color: 'var(--text-1)' }}>{amount.toLocaleString()}</td>
                                    </tr>
                                  ))}
                                </>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </div>
                        ))}
                      </div>
                    ))}
                  </div>
                )
              })}
            </div>
          )}

          {/* 행별 상세 탭 — flat grid */}
          {detailTab === 'detail' && (
            <div style={{ flex: 1, overflowY: 'auto' }}>
              <div style={{ padding: '0 24px 14px', overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                  <thead>
                    <tr style={{ color: 'var(--text-3)' }}>
                      {(['', '得意先', ...(hasJisho ? ['入荷支店'] : []), '商品名', 'タイプ', '条件', '仕切', 'NET', '수량', '금액'] as string[]).map((h, i) => {
                        const leftColCount = hasJisho ? 4 : 3
                        return (
                          <th key={i} style={{
                            padding: i >= leftColCount ? '10px 0 8px 16px' : '10px 12px 8px 0',
                            fontWeight: 600, fontSize: 10,
                            textAlign: i < leftColCount ? 'left' : 'right',
                            letterSpacing: '0.04em', textTransform: 'uppercase',
                            borderBottom: '1px solid var(--border)',
                            whiteSpace: 'nowrap',
                            position: 'sticky', top: 0,
                            background: 'var(--card)', zIndex: 1,
                          }}>{h}</th>
                        )
                      })}
                    </tr>
                  </thead>
                  <tbody>
                    {filteredFlatRows.map((row, i) => {
                      const tc          = typeColor(row.タイプ ?? '')
                      const cond        = row['条件1（パック）'] ?? row['条件1（ケース）']
                      const ocrName     = row.customer_ocr || '—'
                      const retailerCode = row.代表スーパー ?? ''
                      const retailer    = row.スーパー ?? '—'
                      const distCode    = row.受注先コード ?? ''
                      const dist        = row.受注先 ?? '—'
                      const highlightKey = row.jisho || row.product_ocr || row.customer_ocr || null
                      const warnBg = (row.unconfirmed || row.net_lt_honbu)
                      return (
                        <tr key={i}
                          onClick={() => {
                            if (row.page_number != null) setPage(row.page_number)
                            setSelectedCustomer(prev => prev === highlightKey ? null : highlightKey)
                          }}
                          onMouseEnter={e => (e.currentTarget.style.background = warnBg ? '#fff0c2' : '#f5ede0')}
                          onMouseLeave={e => (e.currentTarget.style.background = warnBg ? '#fff8e1' : 'transparent')}
                          style={{
                            borderBottom: '1px solid #f1f3f5',
                            background: warnBg ? '#fff8e1' : 'transparent',
                            cursor: 'pointer',
                          }}>
                          {/* 경고 */}
                          <td style={{ padding: '8px 4px 8px 0', width: 20, verticalAlign: 'top' }}>
                            {row.unconfirmed && (
                              <span title="매핑 미확정" style={{ display: 'block', width: 14, height: 14, borderRadius: '50%', background: '#fab005', color: '#fff', fontSize: 9, fontWeight: 800, lineHeight: '14px', textAlign: 'center' }}>?</span>
                            )}
                            {row.net_lt_honbu && (
                              <span title="NET 본부장 미달" style={{ display: 'block', fontSize: 11, lineHeight: 1, color: '#e03131', fontWeight: 800 }}>↓</span>
                            )}
                          </td>
                          {/* 得意先 */}
                          <td style={{ padding: '8px 12px 8px 0', maxWidth: 200, verticalAlign: 'top' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                              <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 150 }}>{ocrName}</span>
                              <button onClick={e => { e.stopPropagation(); setRemapTarget({ ocrName }) }} title="소매처 수정" style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '0 1px', color: 'var(--text-3)', display: 'inline-flex', alignItems: 'center', flexShrink: 0 }}>
                                <Pencil size={9} />
                              </button>
                            </div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 3, marginTop: 2 }}>
                              <span style={{ width: 5, height: 5, borderRadius: '50%', background: '#339af0', flexShrink: 0 }} />
                              <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-3)' }}>{retailerCode || '—'}</span>
                              <span style={{ fontSize: 10, color: 'var(--text-3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 110 }}>{retailer}</span>
                            </div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 3, marginTop: 1 }}>
                              <span style={{ width: 5, height: 5, borderRadius: '50%', background: '#fd7e14', flexShrink: 0 }} />
                              <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-3)' }}>{distCode || '—'}</span>
                              <span style={{ fontSize: 10, color: 'var(--text-3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 110 }}>{dist}</span>
                              <button onClick={e => { e.stopPropagation(); setDistRemapTarget({ ocrName, retailerCode }) }} title="판매처 수정" style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '0 1px', color: 'var(--text-3)', display: 'inline-flex', alignItems: 'center', flexShrink: 0 }}>
                                <Pencil size={9} />
                              </button>
                            </div>
                          </td>
                          {/* 入荷支店 — jisho データあり時のみ */}
                          {hasJisho && (
                            <td style={{ padding: '8px 12px 8px 0', fontSize: 11, color: 'var(--text-2)', whiteSpace: 'nowrap', verticalAlign: 'top' }}>
                              {row.jisho || '—'}
                            </td>
                          )}
                          {/* 商品名 */}
                          <td style={{ padding: '8px 8px 8px 0', maxWidth: 160, verticalAlign: 'top' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                              <HoverTooltip text={row.product_ocr || row.商品名 || '—'} style={{ fontSize: 12, color: 'var(--text-1)', fontWeight: 500 }} />
                              <button onClick={e => { e.stopPropagation(); setProductRemapTarget({ productOcr: row.product_ocr || row.商品名 || '' }) }} title="제품 매핑 수정" style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '0 1px', color: 'var(--text-3)', display: 'inline-flex', alignItems: 'center', flexShrink: 0 }}>
                                <Pencil size={9} />
                              </button>
                            </div>
                            {(row.商品コード || row.商品名) && (
                              <div style={{ display: 'flex', gap: 4, marginTop: 2, alignItems: 'baseline' }}>
                                {row.商品コード && <span style={{ fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-3)' }}>{row.商品コード}</span>}
                                {row.商品名 && <HoverTooltip text={row.商品名} style={{ fontSize: 10, color: 'var(--text-3)' }} />}
                              </div>
                            )}
                          </td>
                          {/* タイプ */}
                          <td style={{ padding: '8px 0 8px 16px', textAlign: 'right', verticalAlign: 'top' }}>
                            <span style={{ background: tc.bg, color: tc.color, borderRadius: 5, padding: '2px 8px', fontWeight: 600, fontSize: 11, whiteSpace: 'nowrap', display: 'inline-block' }}>
                              {row.タイプ ?? '—'}
                            </span>
                          </td>
                          {/* 条件 */}
                          <td style={{ padding: '8px 0 8px 16px', textAlign: 'right', color: 'var(--text-2)', fontFamily: 'var(--mono)', verticalAlign: 'top' }}>
                            {fmt(cond)}
                          </td>
                          {/* 仕切 */}
                          <td style={{ padding: '8px 0 8px 16px', textAlign: 'right', color: 'var(--text-2)', fontFamily: 'var(--mono)', verticalAlign: 'top' }}>
                            {fmt(row.仕切)}
                          </td>
                          {/* NET */}
                          <td style={{ padding: '8px 0 8px 16px', textAlign: 'right', fontWeight: 700, fontFamily: 'var(--mono)', color: row.net_lt_honbu ? '#e03131' : 'var(--text-1)', verticalAlign: 'top' }}>
                            {fmt(row.NET)}
                          </td>
                          {/* 수량 */}
                          <td style={{ padding: '8px 0 8px 16px', textAlign: 'right', color: 'var(--text-2)', fontFamily: 'var(--mono)', verticalAlign: 'top' }}>
                            {fmt(row.個数計)}
                          </td>
                          {/* 금액 */}
                          <td style={{ padding: '8px 0 8px 16px', textAlign: 'right', fontWeight: 600, color: 'var(--text-1)', fontFamily: 'var(--mono)', verticalAlign: 'top' }}>
                            {fmt(row['未収金額合計'])}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}

        </>}

      </div>

      <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>

      {remapTarget && (
        <RetailerSearchModal
          initialQuery={remapTarget.ocrName}
          onSelect={(code, name) => handleRemap(remapTarget.ocrName, code, name)}
          onClose={() => setRemapTarget(null)}
        />
      )}
      {distRemapTarget && (
        <DistSearchModal
          ocrName={distRemapTarget.ocrName}
          retailerCode={distRemapTarget.retailerCode}
          onSelect={(code, name) => handleRemapDist(distRemapTarget.ocrName, code, name)}
          onClose={() => setDistRemapTarget(null)}
        />
      )}
      {productRemapTarget && (
        <ProductSearchModal
          productOcr={productRemapTarget.productOcr}
          onSelect={(code, name) => handleRemapProduct(productRemapTarget.productOcr, code, name)}
          onClose={() => setProductRemapTarget(null)}
        />
      )}
      {remapping && (
        <div style={{
          position: 'fixed', inset: 0, zIndex: 400,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: 'rgba(26,21,18,0.3)',
        }}>
          <div style={{
            background: 'var(--card)', borderRadius: 12, padding: '16px 24px',
            display: 'flex', alignItems: 'center', gap: 10,
            fontSize: 13, color: 'var(--text-1)', fontWeight: 600,
            boxShadow: '0 8px 32px rgba(0,0,0,0.15)',
          }}>
            <Loader2 size={16} style={{ animation: 'spin 0.8s linear infinite' }} />
            매핑 수정 중...
          </div>
        </div>
      )}
    </div>
  )
}
