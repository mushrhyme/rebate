import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { Upload, ArrowRight, AlertCircle, X, RotateCcw, CheckCircle2, LockOpen, Trash2, StopCircle } from 'lucide-react'
import { api, type Document } from '../api/client'
import { useAuth } from '../context/AuthContext'
import { useForms } from '../context/FormsContext'

type Status = Document['status']
type StatusFilter = 'all' | 'pending' | 'in_progress' | 'done' | 'confirmed' | 'error'

// ── 진행 단계 표시 ──────────────────────────────────────────────────────────

const PHASE_ORDER = ['phase1', 'phase2', 'phase3', 'phase4_xv'] as const

function PhaseIndicator({ doc }: { doc: Document }) {
  const tu = doc.token_usage
  const s = doc.status

  // phase3_tool_use는 phase3의 별칭 — tool use 경로가 활성화된 경우 이 키로 저장됨
  const hasPhase = (key: string) =>
    !!tu?.[key] || (key === 'phase3' && !!tu?.['phase3_tool_use'])

  function getState(key: string): 'done' | 'active' | 'waiting' | 'error' {
    if (key === 'ocr') {
      if (s === 'queued') return 'waiting'
      if (s === 'ocr') return 'active'
      return 'done'
    }
    if (s === 'done') return 'done'
    if (s === 'error') {
      const errIdx = doc.error_phase ? PHASE_ORDER.indexOf(doc.error_phase as typeof PHASE_ORDER[number]) : -1
      const thisIdx = PHASE_ORDER.indexOf(key as typeof PHASE_ORDER[number])
      if (errIdx === -1) return hasPhase(key) ? 'done' : 'waiting'
      if (thisIdx < errIdx) return 'done'
      if (thisIdx === errIdx) return 'error'
      return 'waiting'
    }
    if (s === 'queued' || s === 'ocr') return 'waiting'
    // pending = phase3까지 완료, phase4 미시작
    if (s === 'pending') return key === 'phase4_xv' ? 'waiting' : 'done'
    // status가 특정 phase를 직접 가리키면 token_usage 대신 status 기준으로 판단
    // (재실행 시 이전 phase token_usage가 남아 있어도 올바른 단계를 표시)
    const PHASE_STATUS_MAP: Partial<Record<string, string>> = {
      phase1: 'phase1', phase2: 'phase2', phase3: 'phase3', phase4: 'phase4_xv',
    }
    const activePhaseKey = PHASE_STATUS_MAP[s]
    if (activePhaseKey) {
      const thisIdx = PHASE_ORDER.indexOf(key as typeof PHASE_ORDER[number])
      const activeIdx = PHASE_ORDER.indexOf(activePhaseKey as typeof PHASE_ORDER[number])
      if (thisIdx < activeIdx) return 'done'
      if (thisIdx === activeIdx) return 'active'
      return 'waiting'
    }
    if (hasPhase(key)) return 'done'
    const active = PHASE_ORDER.find(p => !hasPhase(p)) ?? 'phase1'
    return key === active ? 'active' : 'waiting'
  }

  const steps = [
    { key: 'ocr',       label: 'OCR' },
    { key: 'phase1',    label: '1' },
    { key: 'phase2',    label: '2' },
    { key: 'phase3',    label: '3' },
    { key: 'phase4_xv', label: '4' },
  ]

  return (
    <span style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
      {steps.map(({ key, label }) => {
        const state = getState(key)
        return (
          <span
            key={key}
            title={{ ocr: 'OCR', phase1: 'Phase 1', phase2: 'Phase 2', phase3: 'Phase 3', phase4_xv: 'Phase 4' }[key]}
            style={{
              fontSize: 10, fontWeight: 700, fontFamily: 'var(--mono)',
              padding: '2px 5px', borderRadius: 4, whiteSpace: 'nowrap',
              background: state === 'done'    ? '#e0f0f0'
                        : state === 'active'  ? '#fdf0e8'
                        : state === 'error'   ? '#fae8e8'
                        : 'var(--bg)',
              color: state === 'done'    ? 'var(--primary)'
                   : state === 'active'  ? '#c4622c'
                   : state === 'error'   ? '#b03030'
                   : 'var(--text-3)',
              border: `1px solid ${
                state === 'done'   ? 'rgba(10,110,110,0.18)'
              : state === 'active' ? 'rgba(196,98,44,0.3)'
              : state === 'error'  ? '#f0c8c8'
              : 'var(--border)'}`,
              animation: state === 'active' ? 'phasePulse 1.6s ease-in-out infinite' : undefined,
            }}
          >
            {label}
          </span>
        )
      })}
    </span>
  )
}

const statusConfig: Record<Status, { label: string; bg: string; color: string; dot: string }> = {
  uploaded:  { label: '업로드됨',  bg: '#f0f0f4', color: '#666680', dot: '#888899' },
  queued:    { label: '대기중',    bg: '#f0f0f4', color: '#666680', dot: '#888899' },
  ocr:       { label: 'OCR중',    bg: '#e8eef4', color: '#3a6b8a', dot: '#3a6b8a' },
  analyzing: { label: '분석중',   bg: '#e8eef4', color: '#3a6b8a', dot: '#3a6b8a' },
  phase1:    { label: 'Phase 1',  bg: '#e8eef4', color: '#3a6b8a', dot: '#3a6b8a' },
  phase2:    { label: 'Phase 2',  bg: '#e8eef4', color: '#3a6b8a', dot: '#3a6b8a' },
  phase3:    { label: 'Phase 3',  bg: '#e8eef4', color: '#3a6b8a', dot: '#3a6b8a' },
  phase4:    { label: 'Phase 4',  bg: '#e8eef4', color: '#3a6b8a', dot: '#3a6b8a' },
  pending:   { label: '확인 대기', bg: '#fdf0e8', color: '#c4622c', dot: '#e07840' },
  done:      { label: '완료',     bg: '#eaf4ee', color: '#2d7d4a', dot: '#3a9960' },
  error:      { label: '오류',     bg: '#fae8e8', color: '#b03030', dot: '#cc4040' },
  xv_warning: { label: '완료', bg: '#e8f0ef', color: 'var(--primary)', dot: 'var(--primary)' },
}

const card = {
  background: 'var(--card)',
  borderRadius: 14,
  border: '1px solid var(--border)',
  boxShadow: 'var(--shadow-sm)',
}

function fmtDate(iso: string): string {
  const d = new Date(iso)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}.${m}.${day}`
}

function fmtTime(iso: string): string {
  const d = new Date(iso)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function fmtYM(iso: string): string {
  return iso.slice(0, 7)
}

function fmtDuration(ms: number): string {
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

// 청구연월 우선, 없으면 업로드일에서 추출. 결과는 항상 "YYYY.MM"
function docYM(doc: Document): string {
  if (doc.hatsu_month) return doc.hatsu_month
  if (!doc.created_at) return '----.-'
  return fmtYM(doc.created_at).replace('-', '.')
}

// ── 재분석 모달 (2-step: pending → 방식 선택 → 확인 / 그 외 → 바로 확인) ────

type RetryMode = 'cache_remap' | 'full_retry'

function RetryModal({ doc, onClose, onConfirm }: {
  doc: Document
  onClose: () => void
  onConfirm: (mode: RetryMode) => Promise<void>
}) {
  const isPending = doc.status === 'pending' || doc.status === 'xv_warning' || doc.status === 'done'
  const [step, setStep] = useState<1 | 2>(isPending ? 1 : 2)
  const [mode, setMode] = useState<RetryMode>('cache_remap')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  async function handleConfirm() {
    setLoading(true)
    setErr(null)
    try {
      await onConfirm(mode)
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '실행 실패. 잠시 후 다시 시도하세요.')
    } finally {
      setLoading(false)
    }
  }

  const backdrop: React.CSSProperties = {
    position: 'fixed', inset: 0, zIndex: 100,
    background: 'rgba(26,21,18,0.45)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    backdropFilter: 'blur(2px)',
  }
  const modalBox: React.CSSProperties = {
    width: 420, background: 'var(--card)', borderRadius: 16,
    border: '1px solid var(--border)', boxShadow: '0 20px 60px rgba(26,21,18,0.18)',
    overflow: 'hidden',
  }

  // ── Step 1: 방식 선택 (pending 전용) ──────────────────────────────────────
  if (step === 1) {
    return (
      <div onClick={onClose} style={backdrop}>
        <div onClick={e => e.stopPropagation()} style={modalBox}>
          <div style={{
            padding: '18px 22px', borderBottom: '1px solid var(--border)',
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          }}>
            <div>
              <p style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-1)' }}>재분석 방식 선택</p>
              <p style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'var(--mono)', marginTop: 2 }}>{doc.doc_id}</p>
            </div>
            <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-3)', padding: 4 }}>
              <X size={16} />
            </button>
          </div>

          <div style={{ padding: '16px 22px', display: 'flex', flexDirection: 'column', gap: 10 }}>
            {(['cache_remap', 'full_retry'] as RetryMode[]).map(m => {
              const selected = mode === m
              const isCR = m === 'cache_remap'
              const accent = isCR ? 'var(--primary)' : '#c4622c'
              return (
                <div
                  key={m}
                  onClick={() => setMode(m)}
                  style={{
                    borderRadius: 10, padding: '12px 14px', cursor: 'pointer',
                    border: selected ? `2px solid ${accent}` : '1px solid var(--border)',
                    background: selected ? (isCR ? 'var(--primary-light)' : '#fdf0e8') : 'var(--card)',
                    transition: 'all 0.12s',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5 }}>
                    <span style={{
                      width: 14, height: 14, borderRadius: '50%', flexShrink: 0,
                      border: `2px solid ${selected ? accent : 'var(--border)'}`,
                      background: selected ? accent : 'transparent',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                    }}>
                      {selected && <span style={{ width: 5, height: 5, borderRadius: '50%', background: '#fff' }} />}
                    </span>
                    <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-1)' }}>
                      {isCR ? 'Phase 3 재실행' : '전체 재분석'}
                    </span>
                    {isCR && (
                      <span style={{
                        fontSize: 10, fontWeight: 700, color: 'var(--primary)',
                        background: 'var(--primary-light)', border: '1px solid rgba(10,110,110,0.2)',
                        borderRadius: 10, padding: '1px 7px',
                      }}>추천</span>
                    )}
                  </div>
                  <p style={{ fontSize: 11, color: 'var(--text-2)', lineHeight: 1.6, paddingLeft: 22 }}>
                    {isCR
                      ? 'Phase 2 추출 결과를 유지하고, 매핑(Phase 3)만 재실행합니다. 미매핑 항목은 Claude가 재판단합니다.'
                      : 'OCR을 제외한 Phase 2~4 전체를 재실행합니다. 기존 매핑 확인 내역이 초기화됩니다.'}
                  </p>
                </div>
              )
            })}
          </div>

          <div style={{ padding: '12px 22px 20px', display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <button
              onClick={onClose}
              style={{
                padding: '9px 18px', borderRadius: 9, fontSize: 12, fontWeight: 600,
                background: 'var(--card)', border: '1px solid var(--border)',
                color: 'var(--text-2)', cursor: 'pointer',
              }}
            >취소</button>
            <button
              onClick={() => setStep(2)}
              style={{
                padding: '9px 18px', borderRadius: 9, fontSize: 12, fontWeight: 700,
                background: mode === 'cache_remap' ? 'var(--primary)' : '#c4622c',
                color: '#fff', border: 'none', cursor: 'pointer',
              }}
            >다음 →</button>
          </div>
        </div>
      </div>
    )
  }

  // ── Step 2: 확인 ──────────────────────────────────────────────────────────
  const isCacheRemap = mode === 'cache_remap'
  return (
    <div onClick={onClose} style={backdrop}>
      <div onClick={e => e.stopPropagation()} style={modalBox}>
        <div style={{
          padding: '18px 22px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div>
            <p style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-1)' }}>
              {isCacheRemap ? 'Phase 3 재실행' : '재분석'}
            </p>
            <p style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'var(--mono)', marginTop: 2 }}>{doc.doc_id}</p>
          </div>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-3)', padding: 4 }}>
            <X size={16} />
          </button>
        </div>

        <div style={{ padding: '18px 22px' }}>
          <div style={{
            borderRadius: 10, padding: '12px 14px', fontSize: 12, lineHeight: 1.7,
            background: isCacheRemap ? 'var(--primary-light)' : '#fdf0e8',
            border: `1px solid ${isCacheRemap ? 'rgba(10,110,110,0.2)' : '#dbb590'}`,
            color: isCacheRemap ? 'var(--primary)' : '#8a4a20',
          }}>
            {isCacheRemap ? (
              <>
                최신 캐시 CSV를 기준으로 Phase 3만 재실행합니다.<br />
                · Phase 2 추출 결과 유지 (Azure·Claude 재호출 없음)<br />
                · 캐시에 추가된 항목은 자동 완료 → Phase 4 진행<br />
                · 여전히 미매핑인 항목은 다시 대기 상태로 남음
              </>
            ) : (
              <>
                <strong>초기화되는 항목:</strong><br />
                · 기존 매핑 확인 내용 (소매처·제품 코드)<br />
                · Phase 2~4 분석 산출물<br /><br />
                OCR 결과와 Phase 1 MD는 유지됩니다 (Azure 재과금 없음).
              </>
            )}
          </div>

          {err && (
            <div style={{
              marginTop: 10, padding: '10px 14px', borderRadius: 9,
              background: '#fae8e8', border: '1px solid #f0c8c8',
              fontSize: 12, color: '#8a2020', lineHeight: 1.6,
            }}>
              {err}
            </div>
          )}
        </div>

        <div style={{ padding: '12px 22px 20px', display: 'flex', gap: 8, alignItems: 'center' }}>
          {isPending && (
            <button
              onClick={() => { setStep(1); setErr(null) }}
              style={{
                padding: '9px 14px', borderRadius: 9, fontSize: 12, fontWeight: 600,
                background: 'var(--card)', border: '1px solid var(--border)',
                color: 'var(--text-3)', cursor: 'pointer', marginRight: 'auto',
              }}
            >← 이전</button>
          )}
          <button
            onClick={onClose}
            style={{
              padding: '9px 18px', borderRadius: 9, fontSize: 12, fontWeight: 600,
              background: 'var(--card)', border: '1px solid var(--border)',
              color: 'var(--text-2)', cursor: 'pointer',
              marginLeft: isPending ? undefined : 'auto',
            }}
          >취소</button>
          <button
            onClick={handleConfirm}
            disabled={loading}
            style={{
              display: 'flex', alignItems: 'center', gap: 6,
              padding: '9px 18px', borderRadius: 9, fontSize: 12, fontWeight: 700,
              background: isCacheRemap ? 'var(--primary)' : '#c4622c',
              color: '#fff', border: 'none',
              cursor: loading ? 'not-allowed' : 'pointer', opacity: loading ? 0.6 : 1,
            }}
          >
            <RotateCcw size={13} style={loading ? { animation: 'spin 0.8s linear infinite' } : undefined} />
            {loading ? '실행 중...' : isCacheRemap ? 'Phase 3 재실행' : '재분석'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── 오류 모달 ────────────────────────────────────────────────────────────────

function ErrorModal({ doc, onClose, onColdStart, onRetry }: {
  doc: Document; onClose: () => void; onColdStart: () => void; onRetry: () => Promise<void>
}) {
  const [retrying, setRetrying] = useState(false)
  const [retryError, setRetryError] = useState<string | null>(null)
  const isUnknownForm = doc.error_type === 'unknown_form'

  async function handleRetry() {
    setRetrying(true)
    setRetryError(null)
    try {
      await onRetry()
      onClose()
    } catch (e) {
      setRetryError(e instanceof Error ? e.message : '재시도 실패')
    } finally {
      setRetrying(false)
    }
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 100,
        background: 'rgba(26,21,18,0.45)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        backdropFilter: 'blur(2px)',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          width: 440, background: 'var(--card)', borderRadius: 16,
          border: '1px solid var(--border)', boxShadow: '0 20px 60px rgba(26,21,18,0.18)',
          overflow: 'hidden',
        }}
      >
        <div style={{
          padding: '18px 20px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div>
            <p style={{ fontSize: 12, fontWeight: 700, color: 'var(--text-1)', fontFamily: 'var(--mono)' }}>
              {doc.pdf_filename}
            </p>
            <p style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>
              {doc.form_id ?? '미인식'} · {fmtDate(doc.created_at)}
            </p>
          </div>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-3)', padding: 4 }}>
            <X size={16} />
          </button>
        </div>

        <div style={{ padding: '20px 20px 8px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
            <span style={{
              fontSize: 10, fontWeight: 700, color: '#b03030',
              background: '#fae8e8', borderRadius: 20, padding: '3px 9px', fontFamily: 'var(--mono)',
            }}>
              {doc.error_phase ?? 'Error'}
            </span>
            <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-1)' }}>
              {isUnknownForm ? '양식 미인식' : '처리 오류'}
            </span>
          </div>

          <div style={{
            background: '#fae8e8', border: '1px solid #f0c8c8',
            borderRadius: 10, padding: '12px 14px',
            display: 'flex', alignItems: 'flex-start', gap: 10,
          }}>
            <AlertCircle size={15} color="#b03030" style={{ flexShrink: 0, marginTop: 1 }} />
            <p style={{ fontSize: 12, color: '#8a2020', lineHeight: 1.6 }}>
              {doc.error_message ?? '알 수 없는 오류'}
            </p>
          </div>

          {isUnknownForm && (
            <div style={{
              marginTop: 12, background: 'var(--primary-light)', border: '1px solid rgba(10,110,110,0.2)',
              borderRadius: 9, padding: '11px 14px', fontSize: 12, color: 'var(--primary)', lineHeight: 1.6,
            }}>
              처음 보는 양식이라면 <strong>신규 양식으로 등록</strong>하세요.
              등록 후 이 문서를 다시 분석합니다.
            </div>
          )}
          {retryError && (
            <div style={{
              marginTop: 10, padding: '9px 12px', borderRadius: 8,
              background: '#fae8e8', border: '1px solid #f0c8c8',
              fontSize: 11, color: '#8a2020',
            }}>
              {retryError}
            </div>
          )}
        </div>

        <div style={{ padding: '16px 20px', display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button
            onClick={handleRetry}
            disabled={retrying}
            style={{
              display: 'flex', alignItems: 'center', gap: 6,
              padding: '9px 16px', borderRadius: 9, fontSize: 12, fontWeight: 600,
              background: 'var(--card)', border: '1px solid var(--border)',
              color: 'var(--text-2)', cursor: retrying ? 'not-allowed' : 'pointer',
              opacity: retrying ? 0.6 : 1,
            }}
          >
            <RotateCcw size={13} style={retrying ? { animation: 'spin 0.8s linear infinite' } : undefined} />
            {retrying ? '재시도 중...' : '재시도'}
          </button>
          {isUnknownForm && (
            <button
              onClick={onColdStart}
              style={{
                display: 'flex', alignItems: 'center', gap: 6,
                padding: '9px 16px', borderRadius: 9, fontSize: 12, fontWeight: 700,
                background: 'var(--primary)', color: '#fff', border: 'none',
                cursor: 'pointer', boxShadow: '0 3px 10px rgba(10,110,110,0.28)',
              }}
            >
              신규 양식으로 등록 →
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ── 확정 취소 모달 ───────────────────────────────────────────────────────────

function UnconfirmModal({ doc, onClose, onConfirm }: {
  doc: Document; onClose: () => void; onConfirm: () => Promise<void>
}) {
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  async function handleConfirm() {
    setLoading(true)
    setErr(null)
    try {
      await onConfirm()
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '확정 취소 실패')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 100,
        background: 'rgba(26,21,18,0.45)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        backdropFilter: 'blur(2px)',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          width: 400, background: 'var(--card)', borderRadius: 16,
          border: '1px solid var(--border)', boxShadow: '0 20px 60px rgba(26,21,18,0.18)',
          overflow: 'hidden',
        }}
      >
        <div style={{ padding: '20px 22px', borderBottom: '1px solid var(--border)' }}>
          <p style={{ fontSize: 14, fontWeight: 700, color: 'var(--text-1)', marginBottom: 4 }}>확정을 취소하시겠습니까?</p>
          <p style={{ fontSize: 12, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}>{doc.doc_id}</p>
        </div>

        <div style={{ padding: '18px 22px' }}>
          <div style={{
            background: '#fdf0e8', border: '1px solid #dbb590', borderRadius: 10,
            padding: '12px 14px', fontSize: 12, color: '#8a4a20', lineHeight: 1.6,
          }}>
            · 1차/2차 검토 체크는 그대로 유지됩니다<br />
            · Phase 4 결과에서 매핑 수정이 다시 가능해집니다<br />
            · SAP 내보내기 대상에서 제외됩니다
          </div>
          {err && (
            <p style={{ marginTop: 10, fontSize: 12, color: '#b03030' }}>{err}</p>
          )}
        </div>

        <div style={{ padding: '12px 22px 20px', display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button
            onClick={onClose}
            style={{
              padding: '9px 18px', borderRadius: 9, fontSize: 12, fontWeight: 600,
              background: 'var(--card)', border: '1px solid var(--border)',
              color: 'var(--text-2)', cursor: 'pointer',
            }}
          >
            취소
          </button>
          <button
            onClick={handleConfirm}
            disabled={loading}
            style={{
              display: 'flex', alignItems: 'center', gap: 6,
              padding: '9px 18px', borderRadius: 9, fontSize: 12, fontWeight: 700,
              background: '#c4622c', color: '#fff', border: 'none',
              cursor: loading ? 'not-allowed' : 'pointer', opacity: loading ? 0.6 : 1,
            }}
          >
            <LockOpen size={13} />
            {loading ? '처리 중...' : '확정 취소'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── 삭제 확인 모달 ───────────────────────────────────────────────────────────

function DeleteConfirmModal({ doc, onClose, onConfirm }: {
  doc: Document; onClose: () => void; onConfirm: (password: string) => Promise<void>
}) {
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { inputRef.current?.focus() }, [])

  async function handleConfirm() {
    if (!password) { setErr('비밀번호를 입력하세요'); return }
    setLoading(true)
    setErr(null)
    try {
      await onConfirm(password)
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '삭제 실패')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 100,
        background: 'rgba(26,21,18,0.45)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        backdropFilter: 'blur(2px)',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          width: 400, background: 'var(--card)', borderRadius: 16,
          border: '1px solid var(--border)', boxShadow: '0 20px 60px rgba(26,21,18,0.18)',
          overflow: 'hidden',
        }}
      >
        <div style={{ padding: '20px 22px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{ width: 32, height: 32, borderRadius: 8, background: '#fae8e8', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
            <Trash2 size={15} color="#b03030" />
          </div>
          <div>
            <p style={{ fontSize: 14, fontWeight: 700, color: 'var(--text-1)', marginBottom: 2 }}>문서를 삭제하시겠습니까?</p>
            <p style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}>{doc.doc_id}</p>
          </div>
        </div>

        <div style={{ padding: '18px 22px', display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{
            background: '#fae8e8', border: '1px solid #f0c8c8', borderRadius: 10,
            padding: '12px 14px', fontSize: 12, color: '#8a2020', lineHeight: 1.6,
          }}>
            <strong>삭제되는 항목:</strong><br />
            · PDF 원본 파일 및 OCR 데이터<br />
            · 모든 분석 결과 (Phase 1~4)<br />
            · 매핑 확정 내역<br />
            <br />
            <strong>이 작업은 되돌릴 수 없습니다.</strong>
          </div>

          <div>
            <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text-2)', marginBottom: 6 }}>
              본인 계정 비밀번호 확인
            </label>
            <input
              ref={inputRef}
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleConfirm()}
              placeholder="비밀번호 입력"
              style={{
                width: '100%', boxSizing: 'border-box',
                padding: '9px 12px', borderRadius: 8,
                border: `1px solid ${err ? '#f0c8c8' : 'var(--border)'}`,
                fontSize: 13, outline: 'none', background: 'var(--bg)', color: 'var(--text-1)',
              }}
            />
            {err && <p style={{ marginTop: 6, fontSize: 12, color: '#b03030' }}>{err}</p>}
          </div>
        </div>

        <div style={{ padding: '12px 22px 20px', display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button
            onClick={onClose}
            style={{
              padding: '9px 18px', borderRadius: 9, fontSize: 12, fontWeight: 600,
              background: 'var(--card)', border: '1px solid var(--border)',
              color: 'var(--text-2)', cursor: 'pointer',
            }}
          >
            취소
          </button>
          <button
            onClick={handleConfirm}
            disabled={loading || !password}
            style={{
              display: 'flex', alignItems: 'center', gap: 6,
              padding: '9px 18px', borderRadius: 9, fontSize: 12, fontWeight: 700,
              background: '#b03030', color: '#fff', border: 'none',
              cursor: loading || !password ? 'not-allowed' : 'pointer',
              opacity: loading || !password ? 0.5 : 1,
            }}
          >
            <Trash2 size={13} />
            {loading ? '삭제 중...' : '삭제'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── 대시보드 ─────────────────────────────────────────────────────────────────

const COLS = '120px 160px 1fr 90px 150px 190px 130px'

const STATUS_CHIPS: { key: StatusFilter; label: string; accent: string }[] = [
  { key: 'all',         label: '전체',     accent: 'var(--primary)' },
  { key: 'pending',     label: '확인 대기', accent: '#c4622c' },
  { key: 'in_progress', label: '처리 중',  accent: '#3a6b8a' },
  { key: 'done',        label: '완료',     accent: '#2d7d4a' },
  { key: 'confirmed',   label: '확정',     accent: 'var(--primary)' },
  { key: 'error',       label: '오류',     accent: '#b03030' },
]

function isInProgress(s: Status) {
  return s === 'queued' || s === 'ocr' || s === 'analyzing' ||
         s === 'phase1' || s === 'phase2' || s === 'phase3' || s === 'phase4'
}

export function Dashboard() {
  const navigate = useNavigate()
  const { user } = useAuth()
  const { forms } = useForms()
  const [docs, setDocs] = useState<Document[]>([])
  const [errorDoc, setErrorDoc] = useState<Document | null>(null)
  const [retryDoc, setRetryDoc] = useState<Document | null>(null)
  const [unconfirmDoc, setUnconfirmDoc] = useState<Document | null>(null)
  const [deleteDoc, setDeleteDoc] = useState<Document | null>(null)
  const [cancelingIds, setCancelingIds] = useState<Set<string>>(new Set())
  const [forceRetryingIds, setForceRetryingIds] = useState<Set<string>>(new Set())
  const [filterYM, setFilterYM] = useState<string>('')
  const [filterStatus, setFilterStatus] = useState<StatusFilter>('all')
  const [filterUploader, setFilterUploader] = useState<string>('')

  const fetchDocs = useCallback(async () => {
    try {
      const data = await api.listDocuments()
      setDocs(data)
    } catch {
      // 인증 오류는 client.ts가 처리
    }
  }, [])

  useEffect(() => { fetchDocs() }, [fetchDocs])

  async function handleForceRetry(docId: string) {
    if (!window.confirm('서버 재시작 등으로 멈춘 문서를 강제로 재시작합니다. 계속하시겠습니까?')) return
    setForceRetryingIds(prev => new Set(prev).add(docId))
    try {
      await api.retryDocument(docId, true)
      await fetchDocs()
    } catch (e) {
      alert(e instanceof Error ? e.message : '강제 재시작 실패')
    } finally {
      setForceRetryingIds(prev => { const s = new Set(prev); s.delete(docId); return s })
    }
  }

  async function handleCancel(docId: string) {
    if (!window.confirm('분석을 취소하시겠습니까?')) return
    setCancelingIds(prev => new Set(prev).add(docId))
    try {
      await api.cancelDocument(docId)
      await fetchDocs()
    } catch {
      // 이미 완료된 경우 등 무시하고 목록만 갱신
      await fetchDocs()
    } finally {
      setCancelingIds(prev => { const s = new Set(prev); s.delete(docId); return s })
    }
  }

  const formMap = useMemo(
    () => Object.fromEntries(forms.map(f => [f.id, f.short_name])),
    [forms],
  )

  useEffect(() => {
    const inProgress = docs.some(d => isInProgress(d.status))
    if (!inProgress) return
    const id = setInterval(fetchDocs, 3000)
    return () => clearInterval(id)
  }, [docs, fetchDocs])

  // 연월 선택지 (청구연월 기준, 없으면 업로드일)
  const ymOptions = [...new Set(docs.map(d => docYM(d)))].sort().reverse()

  // 업로더 선택지
  const uploaderOptions = Object.values(
    docs.reduce<Record<string, { username: string; label: string }>>((acc, d) => {
      if (!d.uploaded_by_username) return acc
      if (!acc[d.uploaded_by_username]) {
        acc[d.uploaded_by_username] = {
          username: d.uploaded_by_username,
          label: d.uploaded_by_name_ja || d.uploaded_by_name || d.uploaded_by_username,
        }
      }
      return acc
    }, {})
  ).sort((a, b) => a.label.localeCompare(b.label, 'ja'))

  // 월 필터 먼저 (청구연월 기준)
  const monthFiltered = filterYM ? docs.filter(d => docYM(d) === filterYM) : docs

  // 상태별 카운트 (월 필터 기준)
  const statusCounts: Record<StatusFilter, number> = {
    all:         monthFiltered.length,
    pending:     monthFiltered.filter(d => d.status === 'pending').length,
    in_progress: monthFiltered.filter(d => isInProgress(d.status)).length,
    done:        monthFiltered.filter(d => d.status === 'done' && !d.confirmed_at).length,
    confirmed:   monthFiltered.filter(d => !!d.confirmed_at).length,
    error:       monthFiltered.filter(d => d.status === 'error').length,
  }

  const filteredDocs = monthFiltered
    .filter(d => {
      if (filterStatus === 'all') return true
      if (filterStatus === 'in_progress') return isInProgress(d.status)
      if (filterStatus === 'confirmed') return !!d.confirmed_at
      if (filterStatus === 'done') return d.status === 'done' && !d.confirmed_at
      return d.status === filterStatus
    })
    .filter(d => !filterUploader || d.uploaded_by_username === filterUploader)

  const pending   = filteredDocs.filter(d => d.status === 'pending')
  const analyzing = filteredDocs.filter(d => isInProgress(d.status))
  const done      = filteredDocs.filter(d => d.status === 'done')

  return (
    <div style={{ padding: '32px 44px', maxWidth: 1480, margin: '0 auto' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 20, gap: 16 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: 'var(--text-1)', marginBottom: 4 }}>대시보드</h1>
          <p style={{ fontSize: 13, color: 'var(--text-2)' }}>청구서 분석 현황을 확인하세요</p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
          {/* 업로더 필터 */}
          {uploaderOptions.length > 1 && (
            <select
              value={filterUploader}
              onChange={e => setFilterUploader(e.target.value)}
              style={{
                fontSize: 12, padding: '8px 12px', borderRadius: 8,
                border: '1px solid var(--border)', cursor: 'pointer',
                background: filterUploader ? 'var(--primary)' : 'var(--card)',
                color: filterUploader ? '#fff' : 'var(--text-2)',
                fontWeight: filterUploader ? 600 : 400,
              }}
            >
              <option value="">전체 업로더</option>
              {uploaderOptions.map(({ username, label }) => (
                <option key={username} value={username}>{label}</option>
              ))}
            </select>
          )}
          {/* 월 필터 */}
          <select
            value={filterYM}
            onChange={e => setFilterYM(e.target.value)}
            style={{
              fontSize: 12, padding: '8px 12px', borderRadius: 8,
              border: '1px solid var(--border)', cursor: 'pointer',
              background: filterYM ? 'var(--primary)' : 'var(--card)',
              color: filterYM ? '#fff' : 'var(--text-2)',
              fontWeight: filterYM ? 600 : 400,
            }}
          >
            <option value="">전체 기간</option>
            {ymOptions.map(ym => (
              <option key={ym} value={ym}>{ym.replace('.', '년 ')}월</option>
            ))}
          </select>
          <button
            onClick={() => navigate('/upload')}
            style={{
              display: 'flex', alignItems: 'center', gap: 8,
              background: 'var(--primary)', color: '#fff',
              border: 'none', borderRadius: 10, padding: '10px 20px',
              fontSize: 13, fontWeight: 600, cursor: 'pointer',
              boxShadow: '0 4px 14px rgba(10,110,110,0.3)',
            }}
          >
            <Upload size={15} />
            새 청구서 분석
          </button>
        </div>
      </div>

      {/* 상태 필터 칩 */}
      <div style={{ display: 'flex', gap: 6, marginBottom: 24, flexWrap: 'wrap' }}>
        {STATUS_CHIPS.map(({ key, label, accent }) => {
          const count = statusCounts[key]
          const active = filterStatus === key
          return (
            <button
              key={key}
              onClick={() => setFilterStatus(key)}
              style={{
                display: 'flex', alignItems: 'center', gap: 7,
                padding: '7px 14px', borderRadius: 20, cursor: 'pointer',
                fontSize: 12, fontWeight: active ? 700 : 500,
                border: `1.5px solid ${active ? accent : 'var(--border)'}`,
                background: active ? `${accent}18` : 'var(--card)',
                color: active ? accent : 'var(--text-2)',
                transition: 'all 0.15s',
              }}
            >
              {label}
              {count > 0 && (
                <span style={{
                  fontSize: 10, fontWeight: 700, fontFamily: 'var(--mono)',
                  background: active ? accent : 'var(--border)',
                  color: active ? '#fff' : 'var(--text-2)',
                  borderRadius: 10, padding: '1px 6px',
                  lineHeight: 1.5,
                }}>
                  {count}
                </span>
              )}
            </button>
          )
        })}
      </div>

      {/* 요약 카드 */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 16, marginBottom: 28 }}>
        {[
          {
            label: '확인 대기',
            value: pending.length,
            accent: pending.length > 0,
            onClick: () => pending.length > 0 && navigate(`/mapping/${pending[0].doc_id}`),
            sub: pending.length > 0 ? '클릭해서 처리하기' : '없음',
          },
          { label: '처리 중', value: analyzing.length, accent: false, onClick: undefined, sub: '자동 진행 중' },
          { label: '완료', value: done.length, accent: false, onClick: undefined, sub: filterYM ? `${filterYM.replace('.', '년 ')}월` : '전체' },
        ].map(({ label, value, accent, onClick, sub }) => (
          <div
            key={label}
            onClick={onClick}
            style={{
              ...card,
              padding: '24px 28px',
              cursor: onClick ? 'pointer' : 'default',
              borderColor: accent ? '#dbb590' : 'var(--border)',
              background: accent ? '#fdf0e8' : 'var(--card)',
            }}
          >
            <p style={{ fontSize: 12, fontWeight: 500, color: accent ? '#c4622c' : 'var(--text-2)', marginBottom: 10 }}>
              {label}
            </p>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>
              <span style={{ fontSize: 32, fontWeight: 700, color: accent ? '#c4622c' : 'var(--text-1)', fontFamily: 'var(--mono)' }}>
                {value}
              </span>
              <span style={{ fontSize: 14, color: 'var(--text-2)' }}>건</span>
            </div>
            <p style={{ fontSize: 11, color: accent ? '#c4622c' : 'var(--text-3)', marginTop: 6 }}>
              {sub}
              {accent && value > 0 && <ArrowRight size={11} style={{ display: 'inline', marginLeft: 4, verticalAlign: 'middle' }} />}
            </p>
          </div>
        ))}
      </div>

      {/* 문서 목록 */}
      <div style={card}>
        {/* 헤더 */}
        <div style={{
          display: 'grid', gridTemplateColumns: COLS,
          padding: '12px 24px',
          borderBottom: '1px solid var(--border)',
          fontSize: 10, fontWeight: 600, color: 'var(--text-3)',
          letterSpacing: '0.06em', textTransform: 'uppercase', gap: 8,
        }}>
          <span>업로드일</span>
          <span>업로더</span>
          <span>문서명</span>
          <span>양식</span>
          <span>상태</span>
          <span>진행 단계</span>
          <span></span>
        </div>

        {filteredDocs.length === 0 ? (
          <div style={{ padding: '48px 24px', textAlign: 'center', color: 'var(--text-3)', fontSize: 13 }}>
            {filterYM || filterStatus !== 'all' || filterUploader
              ? '조건에 맞는 문서가 없습니다.'
              : '문서가 없습니다. 새 청구서를 업로드하세요.'}
          </div>
        ) : (
          filteredDocs.map((doc, idx) => {
            const cfg = statusConfig[doc.status]
            const uploaderName = doc.uploaded_by_name_ja || doc.uploaded_by_name || doc.uploaded_by_username
            const phaseSum = Object.values(doc.phase_timings ?? {}).reduce((a, b) => a + b, 0)
            const runStartMs = doc.analysis_started_at
              ? new Date(doc.analysis_started_at).getTime()
              : new Date(doc.created_at).getTime()
            const endMs = doc.updated_at ? new Date(doc.updated_at).getTime() : Date.now()
            const durationStr = isInProgress(doc.status)
              ? fmtDuration(Date.now() - runStartMs)
              : phaseSum > 0
                ? fmtDuration(phaseSum * 1000)
                : fmtDuration(endMs - runStartMs)
            const canDelete = !!(user?.is_admin || !doc.uploaded_by_username || user?.username === doc.uploaded_by_username)
            return (
              <div
                key={doc.doc_id}
                style={{
                  display: 'grid', gridTemplateColumns: COLS,
                  padding: '14px 24px', alignItems: 'center', gap: 8,
                  borderBottom: idx < filteredDocs.length - 1 ? '1px solid var(--border)' : 'none',
                  transition: 'background 0.1s',
                }}
                onMouseEnter={e => (e.currentTarget.style.background = '#f5ede0')}
                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
              >
                {/* 업로드일 */}
                <span style={{ lineHeight: 1.55 }}>
                  <span style={{ display: 'block', fontSize: 12, fontFamily: 'var(--mono)', color: 'var(--text-1)', fontWeight: 600 }}>
                    {fmtDate(doc.created_at)}
                  </span>
                  <span style={{ display: 'block', fontSize: 10, fontFamily: 'var(--mono)', color: 'var(--text-3)' }}>
                    {fmtTime(doc.created_at)}
                  </span>
                </span>

                {/* 업로더 */}
                <span style={{ lineHeight: 1.55, overflow: 'hidden' }}>
                  {uploaderName ? (
                    <>
                      <span style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--text-1)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {uploaderName}
                      </span>
                      {doc.uploaded_by_username && uploaderName !== doc.uploaded_by_username && (
                        <span style={{ display: 'block', fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}>
                          {doc.uploaded_by_username}
                        </span>
                      )}
                    </>
                  ) : (
                    <span style={{ fontSize: 12, color: 'var(--text-3)' }}>—</span>
                  )}
                </span>

                {/* 문서명 */}
                <span style={{ display: 'flex', flexDirection: 'column', gap: 2, overflow: 'hidden' }}>
                  <span style={{ display: 'flex', alignItems: 'center', gap: 5, overflow: 'hidden' }}>
                    <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {doc.pdf_filename}
                    </span>
                    {(doc.pages_count ?? 0) > 20 && (
                      <span title="여러 청구서가 포함되어 있을 수 있습니다" style={{
                        flexShrink: 0, fontSize: 10, fontWeight: 700, color: '#c4622c',
                        background: '#fdf0e8', border: '1px solid #dbb590',
                        borderRadius: 4, padding: '1px 5px',
                      }}>!</span>
                    )}
                  </span>
                  <span style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--mono)', display: 'flex', gap: 6 }}>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{doc.doc_id}</span>
                    {(doc.pages_count ?? 0) > 0 && <span style={{ flexShrink: 0 }}>{doc.pages_count}p</span>}
                  </span>
                </span>

                {/* 양식 */}
                <span style={{ fontSize: 12, color: 'var(--text-2)' }} title={doc.form_id ?? undefined}>
                  {doc.form_id ? (formMap[doc.form_id] ?? doc.form_id) : '—'}
                </span>

                {/* 상태 */}
                <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  {doc.confirmed_at ? (
                    <span style={{
                      display: 'inline-flex', alignItems: 'center', gap: 6,
                      background: '#e0f0f0', color: 'var(--primary)',
                      borderRadius: 20, padding: '4px 10px', fontSize: 11, fontWeight: 600,
                    }}>
                      <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--primary)', flexShrink: 0 }} />
                      확정
                    </span>
                  ) : (
                    <span style={{
                      display: 'inline-flex', alignItems: 'center', gap: 6,
                      background: cfg.bg, color: cfg.color,
                      borderRadius: 20, padding: '4px 10px', fontSize: 11, fontWeight: 600,
                    }}>
                      <span style={{ width: 6, height: 6, borderRadius: '50%', background: cfg.dot, flexShrink: 0 }} />
                      {cfg.label}
                      {doc.status === 'pending' && doc.pending_count > 0 ? ` ${doc.pending_count}건` : ''}
                    </span>
                  )}
                  {doc.confirmed_at && (user?.is_admin || user?.username === doc.uploaded_by_username) && (
                    <button
                      onClick={() => setUnconfirmDoc(doc)}
                      title="확정 취소"
                      style={{
                        width: 22, height: 22, borderRadius: 5, border: '1px solid rgba(10,110,110,0.25)',
                        background: 'transparent', cursor: 'pointer', flexShrink: 0,
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        color: 'var(--primary)', opacity: 0.6,
                      }}
                      onMouseEnter={e => (e.currentTarget.style.opacity = '1')}
                      onMouseLeave={e => (e.currentTarget.style.opacity = '0.6')}
                    >
                      <LockOpen size={11} />
                    </button>
                  )}
                </span>

                {/* 진행 단계 + 소요시간 */}
                <span style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  <PhaseIndicator doc={doc} />
                  <span style={{
                    fontSize: 10, fontFamily: 'var(--mono)',
                    color: isInProgress(doc.status) ? '#c4622c' : 'var(--text-3)',
                  }}>
                    {isInProgress(doc.status) ? '⏱ ' : ''}{durationStr}
                  </span>
                </span>

                {/* 액션 */}
                <span style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                  {doc.status === 'pending' && (
                    <button
                      onClick={() => navigate(`/mapping/${doc.doc_id}`)}
                      style={{
                        fontSize: 12, fontWeight: 600, color: 'var(--primary)',
                        background: 'var(--primary-light)', border: '1px solid rgba(10,110,110,0.2)',
                        borderRadius: 7, padding: '5px 12px', cursor: 'pointer',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      확인하기
                    </button>
                  )}
                  {(doc.status === 'done' || doc.status === 'xv_warning') && (
                    <button
                      onClick={() => navigate(`/results/${doc.doc_id}`)}
                      style={{
                        fontSize: 12, fontWeight: 600, color: 'var(--text-2)',
                        background: '#ede9e1', border: '1px solid var(--border)',
                        borderRadius: 7, padding: '5px 12px', cursor: 'pointer',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      결과보기
                    </button>
                  )}
                  {doc.status === 'error' && (
                    <button
                      onClick={() => setErrorDoc(doc)}
                      style={{
                        fontSize: 12, fontWeight: 600, color: '#b03030',
                        background: '#fae8e8', border: '1px solid #f0c8c8',
                        borderRadius: 7, padding: '5px 12px', cursor: 'pointer',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      오류 확인
                    </button>
                  )}
                  {(doc.status === 'done' || doc.status === 'pending' || doc.status === 'xv_warning') && (
                    <button
                      onClick={() => setRetryDoc(doc)}
                      title="재분석"
                      style={{
                        width: 30, height: 30, borderRadius: 7, border: '1px solid var(--border)',
                        background: 'var(--card)', cursor: 'pointer', flexShrink: 0,
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        color: 'var(--text-3)',
                      }}
                    >
                      <RotateCcw size={13} />
                    </button>
                  )}
                  {isInProgress(doc.status) && (
                    <>
                      <button
                        onClick={() => handleForceRetry(doc.doc_id)}
                        disabled={forceRetryingIds.has(doc.doc_id) || cancelingIds.has(doc.doc_id)}
                        title="멈춘 경우 강제 재시작"
                        style={{
                          width: 30, height: 30, borderRadius: 7, flexShrink: 0,
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          border: '1px solid #b0c8e8',
                          background: '#e8f0fa',
                          color: '#2a5a9a',
                          cursor: (forceRetryingIds.has(doc.doc_id) || cancelingIds.has(doc.doc_id)) ? 'not-allowed' : 'pointer',
                          opacity: (forceRetryingIds.has(doc.doc_id) || cancelingIds.has(doc.doc_id)) ? 0.5 : 1,
                        }}
                      >
                        <RotateCcw size={13} style={forceRetryingIds.has(doc.doc_id) ? { animation: 'spin 0.8s linear infinite' } : undefined} />
                      </button>
                      <button
                        onClick={() => handleCancel(doc.doc_id)}
                        disabled={cancelingIds.has(doc.doc_id) || forceRetryingIds.has(doc.doc_id)}
                        title="분석 취소"
                        style={{
                          width: 30, height: 30, borderRadius: 7, flexShrink: 0,
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          border: '1px solid #dbb590',
                          background: '#fdf0e8',
                          color: '#c4622c',
                          cursor: (cancelingIds.has(doc.doc_id) || forceRetryingIds.has(doc.doc_id)) ? 'not-allowed' : 'pointer',
                          opacity: (cancelingIds.has(doc.doc_id) || forceRetryingIds.has(doc.doc_id)) ? 0.5 : 1,
                        }}
                      >
                        <StopCircle size={13} />
                      </button>
                    </>
                  )}
                  {!isInProgress(doc.status) && (
                    <button
                      onClick={canDelete ? () => setDeleteDoc(doc) : undefined}
                      title={canDelete ? '삭제' : '본인이 업로드한 문서만 삭제할 수 있습니다'}
                      disabled={!canDelete}
                      style={{
                        width: 30, height: 30, borderRadius: 7, flexShrink: 0,
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        border: canDelete ? '1px solid #f0c8c8' : '1px solid var(--border)',
                        background: canDelete ? '#fae8e8' : 'var(--card)',
                        color: canDelete ? '#b03030' : 'var(--text-3)',
                        cursor: canDelete ? 'pointer' : 'not-allowed',
                        opacity: canDelete ? 1 : 0.4,
                      }}
                    >
                      <Trash2 size={13} />
                    </button>
                  )}
                </span>
              </div>
            )
          })
        )}
      </div>

      {/* 오류 모달 */}
      {errorDoc && (
        <ErrorModal
          doc={errorDoc}
          onClose={() => setErrorDoc(null)}
          onColdStart={() => {
            const d = errorDoc
            setErrorDoc(null)
            navigate('/cold-start', { state: { fromDoc: { docId: d.doc_id, formLabel: d.form_id } } })
          }}
          onRetry={async () => {
            await api.retryDocument(errorDoc.doc_id)
            await fetchDocs()
          }}
        />
      )}

      {/* 재분석 모달 */}
      {retryDoc && (
        <RetryModal
          doc={retryDoc}
          onClose={() => setRetryDoc(null)}
          onConfirm={async (mode) => {
            if (mode === 'cache_remap') {
              await api.remapCached(retryDoc.doc_id)
            } else {
              await api.retryDocument(retryDoc.doc_id)
            }
            await fetchDocs()
          }}
        />
      )}

      {/* 확정 취소 모달 */}
      {unconfirmDoc && (
        <UnconfirmModal
          doc={unconfirmDoc}
          onClose={() => setUnconfirmDoc(null)}
          onConfirm={async () => {
            await api.unconfirmDocument(unconfirmDoc.doc_id)
            await fetchDocs()
          }}
        />
      )}

      {/* 삭제 확인 모달 */}
      {deleteDoc && (
        <DeleteConfirmModal
          doc={deleteDoc}
          onClose={() => setDeleteDoc(null)}
          onConfirm={async (password) => {
            await api.deleteDocument(deleteDoc.doc_id, password)
            await fetchDocs()
          }}
        />
      )}

      <style>{`
        @keyframes spin { to { transform: rotate(360deg) } }
        @keyframes phasePulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.45; }
        }
      `}</style>

      {/* 처리 중 안내 배지 */}
      {analyzing.length > 0 && (
        <div style={{
          marginTop: 16, display: 'flex', alignItems: 'center', gap: 10,
          background: '#e8eef4', border: '1px solid rgba(58,107,138,0.25)',
          borderRadius: 10, padding: '11px 16px',
        }}>
          <CheckCircle2 size={15} color="#3a6b8a" style={{ flexShrink: 0 }} />
          <p style={{ flex: 1, fontSize: 13, color: '#3a6b8a', fontWeight: 500 }}>
            <strong>{analyzing.length}개 문서</strong>를 분석 중입니다. 자동으로 새로고침됩니다.
          </p>
        </div>
      )}
    </div>
  )
}
