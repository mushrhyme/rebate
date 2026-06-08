import { useEffect, useState, useMemo } from 'react'
import { FileText, ChevronLeft, ChevronRight, ZoomIn, ZoomOut } from 'lucide-react'

const BASE = import.meta.env.VITE_API_URL ?? ''

interface PdfViewerProps {
  docId?: string
  page: number
  totalPages: number
  highlightText?: string
  mappingType?: 'retailer' | 'product' | 'dist'
  onPageChange: (page: number) => void
}

interface BboxLine {
  text: string
  bbox: number[]
}

interface PageBbox {
  page: number
  width: number
  height: number
  lines: BboxLine[]
}

function normText(s: string) {
  return s
    .replace(/[！-～]/g, c => String.fromCharCode(c.charCodeAt(0) - 0xFEE0))
    .replace(/　/g, ' ')
    .replace(/\s+/g, '')
    .toLowerCase()
}

// CJK·히라가나·가타카나 포함 여부 — 숫자코드보다 텍ス트 라인 우선 선택용
function hasCJK(s: string): boolean {
  return /[぀-鿿豈-﫿]/.test(s)
}

const ZOOM_STEPS = [50, 75, 100, 125, 150, 200]

export function PdfViewer({ docId, page, totalPages, highlightText, mappingType, onPageChange }: PdfViewerProps) {
  const sid = localStorage.getItem('session_id')
  const [bboxData, setBboxData] = useState<PageBbox | null>(null)
  const [zoomIdx, setZoomIdx] = useState(2) // 100%
  const zoom = ZOOM_STEPS[zoomIdx]
  const [pageInput, setPageInput] = useState(String(page))

  useEffect(() => { setPageInput(String(page)) }, [page])

  const commitPage = (val: string) => {
    const n = parseInt(val, 10)
    if (!isNaN(n) && totalPages) onPageChange(Math.max(1, Math.min(totalPages, n)))
    setPageInput(String(page))
  }

  const pageImageUrl = docId && page
    ? `${BASE}/api/v3/documents/${docId}/page-image?page=${page}${sid ? `&sid=${encodeURIComponent(sid)}` : ''}`
    : null

  // bbox JSON 로드 (page 또는 docId 변경 시)
  useEffect(() => {
    if (!docId || !page) { setBboxData(null); return }
    const headers: Record<string, string> = {}
    if (sid) headers['X-Session-ID'] = sid
    fetch(`${BASE}/api/v3/documents/${docId}/page-bbox?page=${page}`, { headers })
      .then(r => r.ok ? r.json() : null)
      .then(setBboxData)
      .catch(() => setBboxData(null))
  }, [docId, page])

  // highlightText가 포함된 line의 bbox를 % 단위로 계산
  const highlight = useMemo(() => {
    if (!bboxData || !highlightText) return null
    const target = normText(highlightText)
    if (!target) return null

    // 동률 후보가 여러 개일 때 x1 좌표로 컬럼 선택
    // product → 오른쪽 컬럼(x1 큰 쪽), retailer/dist → 왼쪽 컬럼(x1 작은 쪽)
    const pickByColumn = (candidates: BboxLine[]): BboxLine | undefined => {
      if (candidates.length === 0) return undefined
      if (candidates.length === 1) return candidates[0]
      return mappingType === 'product'
        ? candidates.reduce((a, b) => b.bbox[0] > a.bbox[0] ? b : a)
        : candidates.reduce((a, b) => b.bbox[0] < a.bbox[0] ? b : a)
    }

    // 1순위: 정확 일치
    const exactMatches = bboxData.lines.filter(l => normText(l.text) === target)
    let line: BboxLine | undefined = pickByColumn(exactMatches)

    if (!line) {
      // 2순위: bbox 텍스트가 target을 포함
      const containsMatches = bboxData.lines.filter(l => normText(l.text).includes(target))
      line = pickByColumn(containsMatches)
    }
    if (!line) {
      // 3순위: target이 bbox 텍스트를 포함 — 가장 긴 것 우선, 동률이면 컬럼으로 구분
      // CJK 포함 후보를 우선 풀로 사용 — 숫자코드(13369041 등)가 글자 수로 이기는 오류 방지
      const allPartials = bboxData.lines.filter(l => {
        const t = normText(l.text)
        return t.length >= 4 && target.includes(t)
      })
      const pool = allPartials.filter(l => hasCJK(l.text))
      const finalPool = pool.length > 0 ? pool : allPartials
      const maxLen = Math.max(0, ...finalPool.map(l => normText(l.text).length))
      const partials = finalPool.filter(l => normText(l.text).length === maxLen)
      line = pickByColumn(partials)
    }

    if (!line || line.bbox.length < 4) return null
    const [x1, y1, x2, y2] = line.bbox
    const padX = bboxData.width  * 0.008  // 가로 여백 0.8%
    const padY = bboxData.height * 0.004  // 세로 여백 0.4%
    return {
      left:   `${((x1 - padX) / bboxData.width)  * 100}%`,
      top:    `${((y1 - padY) / bboxData.height) * 100}%`,
      width:  `${((x2 - x1 + padX * 2) / bboxData.width)  * 100}%`,
      height: `${((y2 - y1 + padY * 2) / bboxData.height) * 100}%`,
    }
  }, [bboxData, highlightText])

  return (
    <div style={{
      height: '100%', display: 'flex', flexDirection: 'column',
      background: 'var(--card)', borderRadius: 14,
      border: '1px solid var(--border)', boxShadow: 'var(--shadow-sm)',
      overflow: 'hidden',
    }}>
      {/* Toolbar */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '10px 16px', borderBottom: '1px solid var(--border)',
        background: '#ede9e1', flexShrink: 0,
      }}>
        <FileText size={14} color="var(--text-3)" />
        <span style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-2)', flex: 1, fontFamily: 'var(--mono)' }}>
          {docId ?? '문서'}.pdf
        </span>
        {highlightText && (
          <span style={{
            fontSize: 11, background: '#fdf0e8', color: '#c4622c',
            border: '1px solid #dbb590', borderRadius: 5,
            padding: '2px 8px', fontFamily: 'var(--mono)', maxWidth: 180,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>✦ {highlightText}</span>
        )}
        {/* 줌 */}
        <div style={{ display: 'flex', gap: 2, alignItems: 'center' }}>
          <button
            onClick={() => setZoomIdx(i => Math.max(0, i - 1))}
            disabled={zoomIdx === 0}
            title="축소"
            style={{
              width: 26, height: 26, borderRadius: 5, border: '1px solid var(--border)',
              background: 'var(--card)', display: 'flex', alignItems: 'center', justifyContent: 'center',
              cursor: zoomIdx === 0 ? 'not-allowed' : 'pointer',
              color: zoomIdx === 0 ? 'var(--text-3)' : 'var(--text-2)',
            }}>
            <ZoomOut size={12} />
          </button>
          <span style={{
            fontSize: 10, color: 'var(--text-2)', fontFamily: 'var(--mono)',
            minWidth: 36, textAlign: 'center',
          }}>
            {zoom}%
          </span>
          <button
            onClick={() => setZoomIdx(i => Math.min(ZOOM_STEPS.length - 1, i + 1))}
            disabled={zoomIdx === ZOOM_STEPS.length - 1}
            title="확대"
            style={{
              width: 26, height: 26, borderRadius: 5, border: '1px solid var(--border)',
              background: 'var(--card)', display: 'flex', alignItems: 'center', justifyContent: 'center',
              cursor: zoomIdx === ZOOM_STEPS.length - 1 ? 'not-allowed' : 'pointer',
              color: zoomIdx === ZOOM_STEPS.length - 1 ? 'var(--text-3)' : 'var(--text-2)',
            }}>
            <ZoomIn size={12} />
          </button>
        </div>
        {/* 페이지 */}
        <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
          <button
            onClick={() => onPageChange(Math.max(1, page - 1))}
            disabled={page <= 1}
            style={{
              width: 28, height: 28, borderRadius: 6, border: '1px solid var(--border)',
              background: 'var(--card)', display: 'flex', alignItems: 'center', justifyContent: 'center',
              cursor: page <= 1 ? 'not-allowed' : 'pointer',
              color: page <= 1 ? 'var(--text-3)' : 'var(--text-2)',
            }}>
            <ChevronLeft size={13} />
          </button>
          <input
            value={pageInput}
            onChange={e => setPageInput(e.target.value)}
            onBlur={e => commitPage(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') { commitPage(pageInput); e.currentTarget.blur() } }}
            style={{
              width: 32, textAlign: 'center', fontFamily: 'var(--mono)',
              fontSize: 11, color: 'var(--text-1)',
              border: '1px solid var(--border)', borderRadius: 5,
              background: 'var(--card)', padding: '3px 4px', outline: 'none',
            }}
          />
          <span style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}>
            / {totalPages || '—'}
          </span>
          <button
            onClick={() => onPageChange(Math.min(totalPages, page + 1))}
            disabled={!totalPages || page >= totalPages}
            style={{
              width: 28, height: 28, borderRadius: 6, border: '1px solid var(--border)',
              background: 'var(--card)', display: 'flex', alignItems: 'center', justifyContent: 'center',
              cursor: (!totalPages || page >= totalPages) ? 'not-allowed' : 'pointer',
              color: (!totalPages || page >= totalPages) ? 'var(--text-3)' : 'var(--text-2)',
            }}>
            <ChevronRight size={13} />
          </button>
        </div>
      </div>

      {/* 스크롤 가능한 이미지 영역 */}
      <div style={{ flex: 1, overflow: 'auto', background: '#e8e2d9' }}>
        {pageImageUrl ? (
          // position:relative인 inner div가 img를 감싸서 오버레이 기준점이 됨
          <div style={{ position: 'relative', width: `${zoom}%`, minWidth: zoom <= 100 ? '100%' : undefined, margin: '0 auto' }}>
            <img
              src={pageImageUrl}
              key={pageImageUrl}
              alt={`page ${page}`}
              style={{ width: '100%', display: 'block' }}
            />
            {highlight && (
              <div style={{
                position: 'absolute',
                left: highlight.left, top: highlight.top,
                width: highlight.width, height: highlight.height,
                border: '2.5px solid #e07b39',
                background: 'rgba(224,123,57,0.18)',
                borderRadius: 3,
                pointerEvents: 'none',
              }} />
            )}
          </div>
        ) : (
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            height: '100%', fontSize: 13, color: 'var(--text-3)',
          }}>
            문서를 선택하세요
          </div>
        )}
      </div>
    </div>
  )
}
