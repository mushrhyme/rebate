import { useState, useEffect, useCallback } from 'react'
import { Download, Loader2, FileSpreadsheet, CheckSquare, Square, ChevronDown } from 'lucide-react'
import { api, type ConfirmedDoc, type SapPreview } from '../api/client'

function fmt(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'number') return v.toLocaleString()
  return String(v)
}

const NOW = new Date()

export function Sap() {
  const [year,  setYear]  = useState(NOW.getFullYear())
  const [month, setMonth] = useState(NOW.getMonth() + 1)
  const [docs,  setDocs]  = useState<ConfirmedDoc[]>([])
  const [loadingDocs, setLoadingDocs] = useState(false)

  const [selected, setSelected] = useState<Set<string>>(new Set())

  const [preview,        setPreview]        = useState<SapPreview | null>(null)
  const [loadingPreview, setLoadingPreview] = useState(false)

  const [downloading, setDownloading] = useState(false)
  const [error,       setError]       = useState<string | null>(null)

  // 연월 변경 시 문서 목록 새로고침
  useEffect(() => {
    setSelected(new Set())
    setPreview(null)
    setLoadingDocs(true)
    api.listConfirmedDocs(year, month)
      .then(setDocs)
      .catch(e => setError(e.message))
      .finally(() => setLoadingDocs(false))
  }, [year, month])

  // 선택 변경 시 미리보기 자동 갱신
  useEffect(() => {
    if (selected.size === 0) { setPreview(null); return }
    setLoadingPreview(true)
    setError(null)
    api.previewSap(Array.from(selected))
      .then(setPreview)
      .catch(e => setError(e.message))
      .finally(() => setLoadingPreview(false))
  }, [selected])

  const toggleDoc = useCallback((docId: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(docId) ? next.delete(docId) : next.add(docId)
      return next
    })
  }, [])

  const toggleAll = useCallback(() => {
    setSelected(prev =>
      prev.size === docs.length ? new Set() : new Set(docs.map(d => d.doc_id))
    )
  }, [docs])

  async function handleDownload() {
    if (selected.size === 0) return
    setDownloading(true)
    setError(null)
    try {
      await api.downloadSap(Array.from(selected))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '다운로드 실패')
    } finally {
      setDownloading(false)
    }
  }

  const allSelected = docs.length > 0 && selected.size === docs.length

  // 연도 옵션: 현재 연도 기준 ±2년
  const yearOptions = Array.from({ length: 5 }, (_, i) => NOW.getFullYear() - 2 + i)
  const monthOptions = Array.from({ length: 12 }, (_, i) => i + 1)

  return (
    <div style={{ padding: '28px 32px', maxWidth: 1400, margin: '0 auto' }}>
      {/* 페이지 헤더 */}
      <div style={{ marginBottom: 24, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 700, color: 'var(--text-1)', margin: 0 }}>
            SAP 엑셀 내보내기
          </h1>
          <p style={{ fontSize: 12, color: 'var(--text-3)', margin: '4px 0 0' }}>
            확정된 문서를 선택해 하나의 엑셀 파일로 합산 내보내기합니다.
          </p>
        </div>

        <button
          onClick={handleDownload}
          disabled={selected.size === 0 || downloading}
          style={{
            display: 'flex', alignItems: 'center', gap: 7,
            padding: '10px 20px', borderRadius: 10,
            background: selected.size === 0 ? '#e0e0e0' : 'var(--primary)',
            color: selected.size === 0 ? '#aaa' : '#fff',
            border: 'none', cursor: selected.size === 0 ? 'not-allowed' : 'pointer',
            fontSize: 13, fontWeight: 600,
            transition: 'background 0.15s',
          }}
        >
          {downloading
            ? <Loader2 size={15} style={{ animation: 'spin 0.8s linear infinite' }} />
            : <Download size={15} />}
          엑셀 다운로드
          {selected.size > 0 && <span style={{ fontSize: 11, opacity: 0.85 }}>({selected.size}건)</span>}
        </button>
      </div>

      {/* 연월 필터 + 문서 목록 */}
      <div style={{
        background: 'var(--card)', borderRadius: 14,
        border: '1px solid var(--border)', marginBottom: 20,
        overflow: 'hidden',
      }}>
        {/* 필터 바 */}
        <div style={{
          padding: '14px 20px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', gap: 12,
        }}>
          <SelectBox
            value={year}
            options={yearOptions.map(y => ({ value: y, label: `${y}년` }))}
            onChange={setYear}
          />
          <SelectBox
            value={month}
            options={monthOptions.map(m => ({ value: m, label: `${m}월` }))}
            onChange={setMonth}
          />
          <span style={{ fontSize: 12, color: 'var(--text-3)', marginLeft: 4 }}>
            확정 기준
          </span>
        </div>

        {/* 전체 선택 행 */}
        {docs.length > 0 && (
          <div style={{
            padding: '10px 20px', borderBottom: '1px solid var(--border)',
            display: 'flex', alignItems: 'center', gap: 10,
            background: '#fafaf8',
          }}>
            <button
              onClick={toggleAll}
              style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--primary)', display: 'flex', alignItems: 'center', gap: 6 }}
            >
              {allSelected
                ? <CheckSquare size={15} />
                : <Square size={15} />}
              <span style={{ fontSize: 12, fontWeight: 600 }}>
                {allSelected ? '전체 해제' : '전체 선택'}
              </span>
            </button>
            <span style={{ fontSize: 11, color: 'var(--text-3)' }}>
              {docs.length}개 문서 · {selected.size}개 선택됨
            </span>
          </div>
        )}

        {/* 문서 목록 */}
        {loadingDocs ? (
          <div style={{ padding: 40, textAlign: 'center' }}>
            <Loader2 size={20} style={{ animation: 'spin 0.8s linear infinite', color: 'var(--text-3)' }} />
          </div>
        ) : docs.length === 0 ? (
          <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-3)', fontSize: 13 }}>
            해당 연월에 확정된 문서가 없습니다
          </div>
        ) : (
          <div>
            {docs.map(doc => {
              const isSelected = selected.has(doc.doc_id)
              const confirmedDate = new Date(doc.confirmed_at).toLocaleDateString('ko-KR', {
                year: 'numeric', month: '2-digit', day: '2-digit',
              })
              return (
                <div
                  key={doc.doc_id}
                  onClick={() => toggleDoc(doc.doc_id)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 14,
                    padding: '12px 20px', cursor: 'pointer',
                    borderBottom: '1px solid var(--border)',
                    background: isSelected ? '#f0fafa' : 'transparent',
                    transition: 'background 0.1s',
                  }}
                  onMouseEnter={e => {
                    if (!isSelected) (e.currentTarget as HTMLDivElement).style.background = '#f8f5f0'
                  }}
                  onMouseLeave={e => {
                    (e.currentTarget as HTMLDivElement).style.background = isSelected ? '#f0fafa' : 'transparent'
                  }}
                >
                  <div style={{ color: isSelected ? 'var(--primary)' : 'var(--text-3)', flexShrink: 0 }}>
                    {isSelected ? <CheckSquare size={16} /> : <Square size={16} />}
                  </div>
                  <FileSpreadsheet size={15} style={{ color: 'var(--text-3)', flexShrink: 0 }} />
                  <div style={{ flex: 1, overflow: 'hidden' }}>
                    <div style={{
                      fontSize: 13, fontWeight: 500, color: 'var(--text-1)',
                      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>
                      {doc.pdf_filename}
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>
                      확정일 {confirmedDate}
                    </div>
                  </div>
                  {isSelected && (
                    <span style={{
                      fontSize: 10, fontWeight: 600, padding: '2px 8px',
                      borderRadius: 20, background: '#e0f0f0', color: 'var(--primary)',
                      flexShrink: 0,
                    }}>
                      선택됨
                    </span>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* 오류 */}
      {error && (
        <div style={{
          marginBottom: 16, padding: '10px 16px', borderRadius: 8,
          background: '#fff5f5', border: '1px solid #ffc9c9',
          color: '#c92a2a', fontSize: 13,
        }}>
          {error}
        </div>
      )}

      {/* 미리보기 그리드 */}
      {loadingPreview && (
        <div style={{ textAlign: 'center', padding: 30 }}>
          <Loader2 size={20} style={{ animation: 'spin 0.8s linear infinite', color: 'var(--text-3)' }} />
          <p style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 8 }}>미리보기 로딩 중…</p>
        </div>
      )}

      {preview && !loadingPreview && (
        <div style={{
          background: 'var(--card)', borderRadius: 14,
          border: '1px solid var(--border)', overflow: 'hidden',
        }}>
          <div style={{
            padding: '12px 20px', borderBottom: '1px solid var(--border)',
            display: 'flex', alignItems: 'center', gap: 8,
          }}>
            <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-1)' }}>
              미리보기
            </span>
            <span style={{ fontSize: 11, color: 'var(--text-3)' }}>
              {preview.rows.length}행 · {preview.columns.length}열 (B~BB)
            </span>
          </div>
          <div style={{ overflowX: 'auto', maxHeight: 480, overflowY: 'auto' }}>
            <table style={{
              borderCollapse: 'collapse', fontSize: 11,
              fontFamily: 'var(--mono)', whiteSpace: 'nowrap',
            }}>
              <thead>
                <tr>
                  {preview.columns.map((col, i) => (
                    <th key={i} style={{
                      padding: '6px 10px',
                      background: '#e0f0f0', color: 'var(--primary)',
                      fontWeight: 700, textAlign: 'center',
                      border: '1px solid var(--border)',
                      position: 'sticky', top: 0, zIndex: 2,
                      fontSize: 10,
                    }}>
                      {col}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {preview.rows.map((row, ri) => (
                  <tr key={ri} style={{ background: ri % 2 === 0 ? 'var(--card)' : '#fafaf8' }}>
                    {preview.columns.map((col, ci) => {
                      const v = row[col]
                      const isNum = typeof v === 'number'
                      return (
                        <td key={ci} style={{
                          padding: '5px 10px',
                          border: '1px solid var(--border)',
                          textAlign: isNum ? 'right' : 'left',
                          color: isNum ? 'var(--text-1)' : 'var(--text-2)',
                        }}>
                          {fmt(v)}
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

// ── 셀렉트 박스 헬퍼 ───────────────────────────────────────────────────────────
function SelectBox({
  value, options, onChange,
}: {
  value: number
  options: { value: number; label: string }[]
  onChange: (v: number) => void
}) {
  return (
    <div style={{ position: 'relative', display: 'inline-flex', alignItems: 'center' }}>
      <select
        value={value}
        onChange={e => onChange(Number(e.target.value))}
        style={{
          appearance: 'none', padding: '6px 30px 6px 12px',
          borderRadius: 8, border: '1px solid var(--border)',
          background: 'var(--card)', color: 'var(--text-1)',
          fontSize: 13, fontWeight: 600, cursor: 'pointer',
          outline: 'none',
        }}
      >
        {options.map(o => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
      <ChevronDown size={13} style={{
        position: 'absolute', right: 9, pointerEvents: 'none',
        color: 'var(--text-3)',
      }} />
    </div>
  )
}
