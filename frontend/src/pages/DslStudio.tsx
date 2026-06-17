import { useState } from 'react'
import { api, type DslPreview } from '../api/client'

/**
 * 규칙 스튜디오 (P3 UI) — 현업이 자연어 규칙을 입력하면 LLM이 DSL 설정으로
 * 컴파일하고, 검증 게이트를 통과한 결과(승인 요약)를 보여준다. 승인하면 동결.
 * LLM은 설정만 작성, 계산은 결정적 코드. 동결 전 사람이 반드시 확인.
 */
export function DslStudio() {
  const [formId, setFormId] = useState('form_04')
  const [docId, setDocId] = useState('')
  const [rule, setRule] = useState(
    '제품 단위로 定番 물량에서 原価引き·導入 추가조건 물량을 빼서 이중계산 없이 분해해줘. 기준은 定番条件, 수량·금액은 표 컬럼 사용.'
  )
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [pv, setPv] = useState<DslPreview | null>(null)
  const [confirmDisplay, setConfirmDisplay] = useState(false)
  const [applied, setApplied] = useState<string | null>(null)

  async function doPreview() {
    setErr(null); setApplied(null); setPv(null); setLoading(true)
    try {
      const r = await api.dslPreview({ form_id: formId, doc_id: docId, rule })
      setPv(r); setConfirmDisplay(false)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally { setLoading(false) }
  }

  async function doApply() {
    if (!pv) return
    setErr(null); setLoading(true)
    try {
      const r = await api.dslApply({
        form_id: formId, doc_id: docId, rule,
        config: pv.config, confirm_display: confirmDisplay,
      })
      setApplied(r.message + ` (백업: ${r.backup})`)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally { setLoading(false) }
  }

  const canApply = !!pv && pv.allok && (!pv.has_review || confirmDisplay)

  return (
    <div style={{ padding: 24, maxWidth: 920, margin: '0 auto' }}>
      <h1 style={{ fontSize: 20, fontWeight: 700, color: 'var(--text-1)', marginBottom: 4 }}>규칙 스튜디오</h1>
      <p style={{ fontSize: 13, color: 'var(--text-3)', marginBottom: 20 }}>
        자연어 규칙 → LLM이 설정으로 컴파일 → 검증 게이트 → 승인 시 동결. 계산은 결정적 코드가 합니다.
      </p>

      <div style={{ display: 'flex', gap: 10, marginBottom: 12 }}>
        <label style={{ flex: '0 0 160px' }}>
          <div style={lbl}>양식 ID</div>
          <input value={formId} onChange={e => setFormId(e.target.value)} style={inp} />
        </label>
        <label style={{ flex: 1 }}>
          <div style={lbl}>샘플 문서 ID (검증용)</div>
          <input value={docId} onChange={e => setDocId(e.target.value)} placeholder="예: 2월日本アクセスＣＶＳ①" style={inp} />
        </label>
      </div>

      <div style={{ marginBottom: 12 }}>
        <div style={lbl}>자연어 규칙</div>
        <textarea value={rule} onChange={e => setRule(e.target.value)} rows={3} style={{ ...inp, resize: 'vertical', fontFamily: 'inherit' }} />
      </div>

      <button onClick={doPreview} disabled={loading || !docId} style={btn(false)}>
        {loading ? '처리 중…' : '검증 (미리보기)'}
      </button>

      {err && <div style={{ marginTop: 14, padding: 10, background: '#fff0f0', color: '#c0392b', borderRadius: 6, fontSize: 13, whiteSpace: 'pre-wrap' }}>{err}</div>}
      {applied && <div style={{ marginTop: 14, padding: 12, background: '#eefcf1', color: '#1e7a43', borderRadius: 6, fontSize: 13, fontWeight: 600 }}>✅ {applied}<br /><span style={{ fontWeight: 400, color: 'var(--text-2)' }}>이제 해당 문서를 재분석하면 동결된 설정으로 계산됩니다.</span></div>}

      {pv && (
        <div style={{ marginTop: 20, border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
          <Section title="① 컴파일 결과">
            <pre style={pre}>{JSON.stringify(pv.config, null, 2)}</pre>
            {pv.reasoning && <div style={{ fontSize: 12, color: 'var(--text-3)' }}>근거: {pv.reasoning}</div>}
          </Section>

          <Section title="② 검증 게이트">
            {pv.gates.map(g => (
              <div key={g.name} style={{ fontSize: 13, marginBottom: 3 }}>
                <span style={{ fontWeight: 700, color: g.ok ? '#1e7a43' : '#c0392b' }}>{g.ok ? 'PASS' : 'FAIL'}</span>
                {' '}<span style={{ color: 'var(--text-2)' }}>{g.name}</span>
                <span style={{ color: 'var(--text-3)' }}> — {g.msg}</span>
              </div>
            ))}
          </Section>

          <Section title="③ 설정 변경">
            {pv.diff.length === 0
              ? <div style={{ fontSize: 13, color: 'var(--text-3)' }}>(값 변화 없음 — 동일 설정)</div>
              : pv.diff.map(d => (
                  <div key={d.field} style={{ fontSize: 13, marginBottom: 3, color: d.validated ? 'var(--text-2)' : '#b9770e' }}>
                    {d.validated ? '' : '⚠ '}<b>{d.field}</b>: {String(d.from)} → {String(d.to)}
                    <span style={{ fontSize: 11, color: 'var(--text-3)' }}> {d.validated ? '(게이트 검증됨)' : '(게이트 비검증 — 사람 확인 필요)'}</span>
                  </div>
                ))}
          </Section>

          <Section title="④ 표본 분해 결과">
            {pv.sample.map((s, i) => (
              <div key={i} style={{ fontSize: 12, marginBottom: 3, color: 'var(--text-2)' }}>
                <b>{s.jisho}</b> · {s.product}: {s.rows.map(r => `${r.qty.toLocaleString()}@${r.amount.toLocaleString()}`).join(' / ')}
                <span style={{ color: 'var(--text-3)' }}> (합 {s.total_amount.toLocaleString()})</span>
              </div>
            ))}
          </Section>

          <div style={{ padding: 14, background: 'var(--card)' }}>
            {pv.has_review && (
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, color: '#b9770e', marginBottom: 10 }}>
                <input type="checkbox" checked={confirmDisplay} onChange={e => setConfirmDisplay(e.target.checked)} />
                ⚠ 게이트가 검증하지 못한 필드(표시 전용) 변경을 확인했습니다.
              </label>
            )}
            <button onClick={doApply} disabled={loading || !canApply} style={btn(true, !canApply)}>
              {pv.allok ? '승인 · 동결' : '게이트 미통과 — 동결 불가'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ padding: 14, borderBottom: '1px solid var(--border)' }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-3)', letterSpacing: '0.04em', marginBottom: 8 }}>{title}</div>
      {children}
    </div>
  )
}

const lbl: React.CSSProperties = { fontSize: 11, fontWeight: 600, color: 'var(--text-3)', marginBottom: 4 }
const inp: React.CSSProperties = { width: '100%', padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 6, fontSize: 13, boxSizing: 'border-box' }
const pre: React.CSSProperties = { fontFamily: 'var(--mono)', fontSize: 12, background: 'var(--bg-2, #f8f9fa)', padding: 8, borderRadius: 6, margin: '0 0 6px', whiteSpace: 'pre-wrap' }
function btn(primary: boolean, disabled = false): React.CSSProperties {
  return {
    padding: '9px 18px', borderRadius: 6, border: 'none', fontSize: 13, fontWeight: 600,
    cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.5 : 1,
    background: primary ? 'var(--primary)' : 'var(--text-1)', color: '#fff',
  }
}
