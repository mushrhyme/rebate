import { useState, Fragment } from 'react'
import { api, type FormPreview } from '../api/client'
import { aggColumns, AggHeadCells, AggDecompCells } from './aggTable'

/**
 * Form 관리 — md 수정본을 config로 반영하기 전에, 샘플 문서로 재계산한 결과를
 * 현업 언어(제품별 분해 + 지점/Cover 합계 일치)로 보여주고 승인받는 패널.
 * "확인한 것 = 반영되는 것" — 미리보기가 만든 config를 그대로 동결한다.
 */
export function FormChangePreview({ formId }: { formId: string }) {
  const [docId, setDocId] = useState('')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [pv, setPv] = useState<FormPreview | null>(null)
  const [committed, setCommitted] = useState<string | null>(null)

  async function doPreview() {
    setErr(null); setCommitted(null); setPv(null); setLoading(true)
    try {
      setPv(await api.previewForm(formId, { doc_id: docId }))
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally { setLoading(false) }
  }

  async function doCommit() {
    if (!pv) return
    setErr(null); setLoading(true)
    try {
      const r = await api.commitForm(formId, pv.new_entry)
      setCommitted(r.message)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally { setLoading(false) }
  }

  const r = pv?.result
  const pa = r?.product_aggregate
  const cols = pa ? (pa.display_columns ?? aggColumns(pa.condition_columns)) : []
  const allXvOk = (r?.xv ?? []).every(x => x.ok)

  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 16, marginTop: 16 }}>
      <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--text-1)', marginBottom: 4 }}>반영 전 결과 미리보기</div>
      <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 12 }}>
        수정한 양식대로 샘플 청구서를 다시 계산하면 어떤 결과가 나오는지 확인하고, 맞으면 반영하세요.
      </div>

      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', marginBottom: 12 }}>
        <label style={{ flex: 1 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-3)', marginBottom: 4 }}>샘플 청구서 ID</div>
          <input value={docId} onChange={e => setDocId(e.target.value)} placeholder="예: 2월日本アクセスＣＶＳ①"
                 style={{ width: '100%', padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 6, fontSize: 13, boxSizing: 'border-box' }} />
        </label>
        <button onClick={doPreview} disabled={loading || !docId}
                style={{ padding: '9px 16px', borderRadius: 6, border: 'none', background: 'var(--text-1)', color: '#fff', fontSize: 13, fontWeight: 600, cursor: loading || !docId ? 'not-allowed' : 'pointer', opacity: loading || !docId ? 0.5 : 1 }}>
          {loading ? '계산 중…' : '미리보기'}
        </button>
      </div>

      {err && <div style={{ padding: 10, background: '#fff0f0', color: '#c0392b', borderRadius: 6, fontSize: 13, whiteSpace: 'pre-wrap' }}>{err}</div>}
      {committed && <div style={{ padding: 12, background: '#eefcf1', color: '#1e7a43', borderRadius: 6, fontSize: 13, fontWeight: 600 }}>✅ {committed}</div>}

      {r && (
        <>
          {/* 지점/Cover 합계 일치 */}
          <div style={{ marginBottom: 14 }}>
            <div style={hdr}>합계 검증 {allXvOk ? '✅ 모두 일치' : '❌ 불일치 있음'}</div>
            {r.xv.map((x, i) => (
              <div key={i} style={{ fontSize: 12, marginBottom: 2, color: x.ok ? 'var(--text-2)' : '#c0392b' }}>
                {x.ok ? '✅' : '❌'} {x.label} — 청구서 {Number(x.expected).toLocaleString()} / 계산 {Number(x.actual).toLocaleString()}
              </div>
            ))}
          </div>

          {/* 제품별 분해 */}
          {pa && pa.groups.length > 0 && (
            <div style={{ marginBottom: 14 }}>
              <div style={hdr}>제품별 분해 (샘플)</div>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                <thead><tr style={{ color: 'var(--text-3)' }}><AggHeadCells columns={cols} /></tr></thead>
                <tbody>
                  {pa.groups.slice(0, 12).map((g, gi) => (
                    <Fragment key={gi}>
                      <tr style={{ borderTop: '1px solid #f1f3f5' }}>
                        <td colSpan={cols.length} style={{ padding: '5px 4px', fontWeight: 600, color: 'var(--text-1)' }}>
                          {g.jisho} · {g.product_name}
                        </td>
                      </tr>
                      {g.rows.map((row, ri) => (
                        <tr key={ri}><AggDecompCells columns={cols} row={row} /></tr>
                      ))}
                    </Fragment>
                  ))}
                </tbody>
              </table>
              {pa.groups.length > 12 && <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>… 외 {pa.groups.length - 12}개 그룹</div>}
            </div>
          )}

          {/* 설정 변경 요약 (참고) */}
          {pv!.config_changes.length > 0 && (
            <div style={{ marginBottom: 14 }}>
              <div style={hdr}>설정 변경 (참고)</div>
              {pv!.config_changes.map((c, i) => (
                <div key={i} style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-3)' }}>{c.field} 변경됨</div>
              ))}
            </div>
          )}

          <button onClick={doCommit} disabled={loading}
                  style={{ padding: '9px 18px', borderRadius: 6, border: 'none', background: 'var(--primary)', color: '#fff', fontSize: 13, fontWeight: 600, cursor: loading ? 'not-allowed' : 'pointer', opacity: loading ? 0.5 : 1 }}>
            이 결과로 반영 (config 동결)
          </button>
        </>
      )}
    </div>
  )
}

const hdr: React.CSSProperties = { fontSize: 11, fontWeight: 700, color: 'var(--text-3)', letterSpacing: '0.04em', marginBottom: 6 }
