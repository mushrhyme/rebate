import { useState, useRef } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { Check, Upload, CheckCircle2, ChevronRight, AlertCircle, FileText, MousePointerClick } from 'lucide-react'
import { useForms } from '../context/FormsContext'
// @ts-ignore — Vite ?url import
import pdfjsWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

type Step = 1 | 2 | 3
const STEPS = ['기본 정보', '페이지 선택', '확인 및 저장']

const BASE = import.meta.env.VITE_API_URL ?? `http://${window.location.hostname}:8001`
function sessionHeaders(): Record<string, string> {
  const sid = localStorage.getItem('session_id')
  return sid ? { 'X-Session-ID': sid } : {}
}

export function ColdStart() {
  const navigate = useNavigate()
  const location = useLocation()
  const { forms, reload } = useForms()
  const fromDoc = (location.state as any)?.fromDoc as { docId: string; formLabel: string } | undefined

  const existingForms = forms
    .map(f => {
      const num = parseInt(f.id.replace('form_', ''), 10)
      const abbr = f.short_name.replace(/^\d+_?/, '') || f.id
      return { num, abbr }
    })
    .filter(f => !isNaN(f.num))
  const existingNums = existingForms.map(f => f.num)
  const defaultNum = existingNums.length > 0 ? Math.max(...existingNums) + 1 : 1

  const [step, setStep] = useState<Step>(1)
  const [form, setForm] = useState({ shortName: '', memo: '', formNum: defaultNum })

  // PDF + 페이지 선택
  const pdfDocRef = useRef<any>(null)
  const [thumbnails, setThumbnails] = useState<{ pageNum: number; dataUrl: string }[]>([])
  const [selectedPages, setSelectedPages] = useState<Set<number>>(new Set())
  const [isLoadingPdf, setIsLoadingPdf] = useState(false)
  const [pdfFileName, setPdfFileName] = useState<string | null>(null)

  // 왼쪽 미리보기
  const [previewDataUrl, setPreviewDataUrl] = useState<string | null>(null)
  const [previewPageNum, setPreviewPageNum] = useState<number | null>(null)

  // 분석 상태
  const [isAnalyzing, setIsAnalyzing] = useState(false)
  const [analyzeError, setAnalyzeError] = useState<string | null>(null)

  const inputStyle: React.CSSProperties = {
    width: '100%', border: '1.5px solid var(--border)', borderRadius: 9,
    padding: '10px 14px', fontSize: 13, outline: 'none',
    fontFamily: 'inherit', color: 'var(--text-1)', background: 'var(--card)',
    boxSizing: 'border-box',
  }

  async function handlePdfUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    setPdfFileName(file.name)
    setIsLoadingPdf(true)
    setThumbnails([])
    setSelectedPages(new Set())
    e.target.value = ''

    try {
      const pdfjsLib = await import('pdfjs-dist')
      pdfjsLib.GlobalWorkerOptions.workerSrc = pdfjsWorkerUrl
      const arrayBuffer = await file.arrayBuffer()
      const pdf = await pdfjsLib.getDocument({ data: arrayBuffer }).promise
      pdfDocRef.current = pdf

      for (let i = 1; i <= pdf.numPages; i++) {
        const page = await pdf.getPage(i)
        const viewport = page.getViewport({ scale: 0.25 })
        const canvas = document.createElement('canvas')
        canvas.width = viewport.width
        canvas.height = viewport.height
        await page.render({ canvasContext: canvas.getContext('2d')!, viewport }).promise
        const dataUrl = canvas.toDataURL('image/jpeg', 0.7)
        setThumbnails(prev => [...prev, { pageNum: i, dataUrl }])
      }
    } catch (err) {
      console.error('PDF 로드 실패', err)
    } finally {
      setIsLoadingPdf(false)
    }
  }

  async function showPreview(pageNum: number) {
    const pdf = pdfDocRef.current
    if (!pdf) return
    const page = await pdf.getPage(pageNum)
    const viewport = page.getViewport({ scale: 1.2 })
    const canvas = document.createElement('canvas')
    canvas.width = viewport.width
    canvas.height = viewport.height
    await page.render({ canvasContext: canvas.getContext('2d')!, viewport }).promise
    setPreviewDataUrl(canvas.toDataURL('image/jpeg', 0.92))
    setPreviewPageNum(pageNum)
  }

  function togglePage(pageNum: number) {
    setSelectedPages(prev => {
      const next = new Set(prev)
      if (next.has(pageNum)) next.delete(pageNum)
      else next.add(pageNum)
      return next
    })
    showPreview(pageNum)
  }

  async function extractPageImages(): Promise<string[]> {
    const pdf = pdfDocRef.current
    if (!pdf) return []
    const sorted = [...selectedPages].sort((a, b) => a - b)
    const images: string[] = []
    for (const pageNum of sorted) {
      const page = await pdf.getPage(pageNum)
      const viewport = page.getViewport({ scale: 2.0 })
      const canvas = document.createElement('canvas')
      canvas.width = viewport.width
      canvas.height = viewport.height
      await page.render({ canvasContext: canvas.getContext('2d')!, viewport }).promise
      images.push(canvas.toDataURL('image/jpeg', 0.85).split(',')[1])
    }
    return images
  }

  async function handleSave() {
    if (isAnalyzing) return
    setIsAnalyzing(true)
    setAnalyzeError(null)
    try {
      const pageImages = await extractPageImages()
      const res = await fetch(`${BASE}/api/v3/forms/cold-start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...sessionHeaders() },
        body: JSON.stringify({ short_name: form.shortName, memo: form.memo, form_num: form.formNum, page_images: pageImages }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: '분석 실패' }))
        throw new Error(err.detail ?? '분석 실패')
      }
      const data = await res.json()
      await reload()
      navigate(`/forms?id=${data.form_id}`)
    } catch (err: any) {
      setAnalyzeError(err.message ?? '분석 중 오류가 발생했습니다.')
      setIsAnalyzing(false)
    }
  }

  const numTaken = existingNums.includes(form.formNum)
  const canNext1 = form.shortName.trim().length > 0 && form.formNum > 0 && !numTaken
  const canNext2 = selectedPages.size > 0 && !isLoadingPdf
  const disabled = step === 1 ? !canNext1 : !canNext2

  return (
    <div style={{ display: 'flex', height: '100%' }}>
      {/* 좌: 페이지 미리보기 */}
      <div style={{
        flex: 1, display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center',
        background: 'var(--bg)', overflow: 'hidden', position: 'relative',
      }}>
        {previewDataUrl ? (
          <>
            {/* 페이지 번호 뱃지 */}
            <div style={{
              position: 'absolute', top: 16, left: 16,
              background: selectedPages.has(previewPageNum!) ? 'var(--primary)' : 'rgba(0,0,0,0.5)',
              color: '#fff', borderRadius: 7, padding: '4px 10px',
              fontSize: 12, fontWeight: 700, zIndex: 1,
              transition: 'background 0.15s',
            }}>
              p.{previewPageNum}{selectedPages.has(previewPageNum!) ? ' ✓' : ''}
            </div>
            <img
              src={previewDataUrl}
              alt={`preview-p${previewPageNum}`}
              style={{
                maxWidth: '90%', maxHeight: '90%',
                objectFit: 'contain',
                borderRadius: 6,
                boxShadow: '0 8px 32px rgba(0,0,0,0.18)',
              }}
            />
          </>
        ) : (
          <div style={{ textAlign: 'center', color: 'var(--text-3)', padding: 24 }}>
            <MousePointerClick size={32} strokeWidth={1.5} style={{ marginBottom: 12, opacity: 0.4 }} />
            <p style={{ fontSize: 13 }}>썸네일을 클릭하면<br />여기서 크게 볼 수 있습니다</p>
          </div>
        )}
      </div>

      {/* 우: 폼 패널 */}
      <div style={{
        width: 440, borderLeft: '1px solid var(--border)',
        background: 'var(--card)', display: 'flex', flexDirection: 'column',
      }}>
        {/* 헤더 + 스텝 인디케이터 */}
        <div style={{ padding: '24px 24px 20px', borderBottom: '1px solid var(--border)' }}>
          <h2 style={{ fontSize: 15, fontWeight: 700, color: 'var(--text-1)', marginBottom: fromDoc ? 12 : 20 }}>신규 양식 등록</h2>

          {fromDoc && (
            <div style={{
              display: 'flex', alignItems: 'flex-start', gap: 8,
              background: '#fdf0e8', border: '1px solid rgba(196,98,44,0.3)',
              borderRadius: 9, padding: '10px 13px', marginBottom: 20,
            }}>
              <AlertCircle size={13} color="#c4622c" style={{ flexShrink: 0, marginTop: 1 }} />
              <div>
                <p style={{ fontSize: 11, fontWeight: 700, color: '#c4622c', marginBottom: 2 }}>미인식 양식에서 연결됨</p>
                <p style={{ fontSize: 11, color: '#a04c20', fontFamily: 'var(--mono)' }}>{fromDoc.docId}</p>
              </div>
            </div>
          )}

          <div style={{ display: 'flex', alignItems: 'center' }}>
            {STEPS.map((label, i) => {
              const s = (i + 1) as Step
              const done = s < step
              const active = s === step
              return (
                <div key={s} style={{ display: 'flex', alignItems: 'center', flex: i < STEPS.length - 1 ? 1 : 'none' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 5 }}>
                    <div style={{
                      width: 28, height: 28, borderRadius: '50%',
                      background: done || active ? 'var(--primary)' : '#ede9e1',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      boxShadow: active ? '0 0 0 4px rgba(10,110,110,0.12)' : 'none',
                    }}>
                      {done
                        ? <Check size={13} color="#fff" strokeWidth={3} />
                        : <span style={{ fontSize: 11, fontWeight: 700, color: active ? '#fff' : 'var(--text-3)' }}>{s}</span>}
                    </div>
                    <span style={{ fontSize: 10, fontWeight: active ? 600 : 400, color: active ? 'var(--primary)' : 'var(--text-3)', whiteSpace: 'nowrap' }}>
                      {label}
                    </span>
                  </div>
                  {i < STEPS.length - 1 && (
                    <div style={{ flex: 1, height: 1.5, background: done ? 'var(--primary)' : 'var(--border)', margin: '0 6px', marginBottom: 22 }} />
                  )}
                </div>
              )
            })}
          </div>
        </div>

        {/* 폼 내용 */}
        <div style={{ flex: 1, overflowY: 'auto', padding: 24 }}>

          {/* ─── Step 1: 기본 정보 ─── */}
          {step === 1 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
              {/* 양식 번호 */}
              <div>
                <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--text-1)', marginBottom: 8 }}>
                  양식 번호
                </label>
                <input
                  type="number"
                  min={1}
                  value={form.formNum}
                  onChange={e => setForm(f => ({ ...f, formNum: parseInt(e.target.value) || 1 }))}
                  style={{ ...inputStyle, width: 100 }}
                />
                {/* 이미 사용 중인 번호 표시 */}
                {existingNums.length > 0 && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 8, flexWrap: 'wrap' }}>
                    <span style={{ fontSize: 11, color: 'var(--text-3)' }}>사용 중:</span>
                    {[...existingForms].sort((a, b) => a.num - b.num).map(({ num, abbr }) => (
                      <span key={num} style={{
                        fontSize: 11, fontWeight: 600, fontFamily: 'var(--mono)',
                        padding: '2px 8px', borderRadius: 5,
                        background: num === form.formNum ? '#fde8e8' : '#ede9e1',
                        color: num === form.formNum ? '#c0392b' : 'var(--text-2)',
                      }}>
                        {num} <span style={{ fontWeight: 400, opacity: 0.7 }}>· {abbr}</span>
                      </span>
                    ))}
                  </div>
                )}
                {numTaken && (
                  <p style={{ fontSize: 11, color: '#c0392b', marginTop: 6 }}>
                    form_{String(form.formNum).padStart(2, '0')}는 이미 존재합니다.
                  </p>
                )}
              </div>
              <div>
                <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--text-1)', marginBottom: 8 }}>
                  약칭 <span style={{ fontWeight: 400, color: 'var(--text-3)' }}>대시보드·목록에 표시되는 이름</span>
                </label>
                <input
                  type="text"
                  value={form.shortName}
                  onChange={e => setForm(f => ({ ...f, shortName: e.target.value }))}
                  placeholder="예: FINET, ACCESS, 旭食品"
                  style={inputStyle}
                />
              </div>
              <div>
                <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--text-1)', marginBottom: 8 }}>
                  메모 <span style={{ fontWeight: 400, color: 'var(--text-3)' }}>선택 — 시스템 동작에 영향 없음</span>
                </label>
                <textarea
                  value={form.memo}
                  onChange={e => setForm(f => ({ ...f, memo: e.target.value }))}
                  placeholder="예: 旭食品, ヤマキ 등에서 수신하는 청구서"
                  rows={3}
                  style={{ ...inputStyle, resize: 'vertical' }}
                />
              </div>
            </div>
          )}

          {/* ─── Step 2: 페이지 선택 ─── */}
          {step === 2 && (
            <div>
              {!pdfFileName ? (
                // PDF 업로드 존
                <label style={{
                  display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                  border: '2px dashed var(--border)', borderRadius: 14, padding: '48px 24px',
                  cursor: 'pointer', background: 'var(--bg)', gap: 12,
                }}>
                  <div style={{
                    width: 48, height: 48, borderRadius: 13, background: 'var(--primary-light)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                  }}>
                    <Upload size={22} color="var(--primary)" />
                  </div>
                  <div style={{ textAlign: 'center' }}>
                    <p style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-1)', marginBottom: 4 }}>PDF 업로드</p>
                    <p style={{ fontSize: 11, color: 'var(--text-3)' }}>클릭하여 청구서 PDF 선택</p>
                  </div>
                  <input type="file" accept=".pdf" style={{ display: 'none' }} onChange={handlePdfUpload} />
                </label>
              ) : isLoadingPdf ? (
                // 렌더링 중
                <div style={{ textAlign: 'center', padding: '40px 0' }}>
                  <div style={{
                    width: 32, height: 32, borderRadius: '50%',
                    border: '3px solid var(--border)', borderTopColor: 'var(--primary)',
                    animation: 'spin 0.8s linear infinite',
                    margin: '0 auto 14px',
                  }} />
                  <p style={{ fontSize: 13, color: 'var(--text-2)' }}>페이지 렌더링 중...</p>
                  <p style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>{thumbnails.length}페이지 완료</p>
                </div>
              ) : (
                // 썸네일 그리드
                <div>
                  {/* 파일명 + 교체 */}
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, overflow: 'hidden' }}>
                      <FileText size={13} color="var(--primary)" style={{ flexShrink: 0 }} />
                      <span style={{ fontSize: 11, color: 'var(--text-2)', fontFamily: 'var(--mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{pdfFileName}</span>
                    </div>
                    <label style={{ fontSize: 11, color: 'var(--primary)', cursor: 'pointer', fontWeight: 600, flexShrink: 0, marginLeft: 8 }}>
                      교체
                      <input type="file" accept=".pdf" style={{ display: 'none' }} onChange={handlePdfUpload} />
                    </label>
                  </div>

                  <p style={{ fontSize: 12, color: 'var(--text-2)', marginBottom: 3 }}>구조가 다른 페이지를 선택하세요.</p>
                  <p style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 12 }}>
                    커버 1장 + 대표 디테일 1~2장 + 마지막 페이지 권장
                  </p>

                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8, maxHeight: 380, overflowY: 'auto', paddingRight: 2 }}>
                    {thumbnails.map(({ pageNum, dataUrl }) => {
                      const selected = selectedPages.has(pageNum)
                      return (
                        <div
                          key={pageNum}
                          onClick={() => togglePage(pageNum)}
                          style={{
                            position: 'relative', cursor: 'pointer', borderRadius: 8, overflow: 'hidden',
                            border: `2.5px solid ${selected ? 'var(--primary)' : 'var(--border)'}`,
                            transition: 'border-color 0.12s',
                          }}
                        >
                          <img src={dataUrl} style={{ width: '100%', display: 'block' }} alt={`p${pageNum}`} />
                          <div style={{
                            position: 'absolute', top: 4, left: 4,
                            background: selected ? 'var(--primary)' : 'rgba(0,0,0,0.45)',
                            color: '#fff', borderRadius: 4, padding: '1px 5px', fontSize: 10, fontWeight: 700,
                          }}>
                            {pageNum}
                          </div>
                          {selected && (
                            <div style={{
                              position: 'absolute', inset: 0, background: 'rgba(10,110,110,0.15)',
                              display: 'flex', alignItems: 'center', justifyContent: 'center',
                            }}>
                              <div style={{
                                background: 'var(--primary)', borderRadius: '50%',
                                width: 28, height: 28, display: 'flex', alignItems: 'center', justifyContent: 'center',
                              }}>
                                <Check size={14} color="#fff" strokeWidth={3} />
                              </div>
                            </div>
                          )}
                        </div>
                      )
                    })}
                  </div>

                  {selectedPages.size > 0 && (
                    <p style={{ fontSize: 12, color: 'var(--primary)', marginTop: 10, fontWeight: 600 }}>
                      {selectedPages.size}페이지 선택됨
                    </p>
                  )}
                </div>
              )}
            </div>
          )}

          {/* ─── Step 3: 확인 및 저장 ─── */}
          {step === 3 && (
            <div>
              {isAnalyzing ? (
                <div style={{ textAlign: 'center', padding: '52px 0' }}>
                  <div style={{
                    width: 40, height: 40, borderRadius: '50%',
                    border: '3px solid var(--border)', borderTopColor: 'var(--primary)',
                    animation: 'spin 0.8s linear infinite',
                    margin: '0 auto 18px',
                  }} />
                  <p style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-1)', marginBottom: 8 }}>
                    Claude가 양식 구조를 분석 중입니다
                  </p>
                  <p style={{ fontSize: 12, color: 'var(--text-3)' }}>
                    선택한 {selectedPages.size}페이지 기준 · 30~60초 소요
                  </p>
                </div>
              ) : analyzeError ? (
                <div style={{ background: '#fdf0e8', border: '1px solid rgba(196,98,44,0.3)', borderRadius: 10, padding: '16px 18px' }}>
                  <p style={{ fontSize: 13, fontWeight: 700, color: '#c4622c', marginBottom: 6 }}>분석 실패</p>
                  <p style={{ fontSize: 12, color: '#a04c20', marginBottom: 14 }}>{analyzeError}</p>
                  <button
                    onClick={() => setAnalyzeError(null)}
                    style={{
                      padding: '8px 16px', borderRadius: 8, fontSize: 12, fontWeight: 600,
                      background: 'var(--card)', border: '1px solid var(--border)', cursor: 'pointer', color: 'var(--text-1)',
                    }}>
                    다시 시도
                  </button>
                </div>
              ) : (
                <div>
                  <p style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 16 }}>저장 정보를 확인하세요.</p>
                  <div style={{
                    background: '#f0f7f7', border: '1px solid rgba(10,110,110,0.2)', borderRadius: 10,
                    padding: '16px 18px', fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text-1)', lineHeight: 1.9,
                  }}>
                    <p><span style={{ color: 'var(--primary)' }}>약칭</span>: {form.shortName}</p>
                    {form.memo && <p><span style={{ color: 'var(--primary)' }}>메모</span>: {form.memo}</p>}
                    <p><span style={{ color: 'var(--primary)' }}>분석 페이지</span>: {[...selectedPages].sort((a, b) => a - b).join(', ')}p ({selectedPages.size}장)</p>
                  </div>
                  <div style={{
                    marginTop: 14, background: 'var(--primary-light)', border: '1px solid rgba(10,110,110,0.2)',
                    borderRadius: 9, padding: '12px 14px', fontSize: 12, color: 'var(--primary)', lineHeight: 1.6,
                  }}>
                    저장 후 Form 관리에서 TBD 항목을 채팅으로 순차 확정할 수 있습니다.
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* 네비게이션 */}
        <div style={{ padding: '16px 24px', borderTop: '1px solid var(--border)', display: 'flex', gap: 10 }}>
          {step > 1 && !isAnalyzing && (
            <button onClick={() => setStep(s => (s - 1) as Step)} style={{
              padding: '11px 20px', borderRadius: 10, fontSize: 13, fontWeight: 500,
              background: 'var(--card)', border: '1px solid var(--border)', color: 'var(--text-2)', cursor: 'pointer',
            }}>
              이전
            </button>
          )}
          {step < 3 ? (
            <button
              onClick={() => setStep(s => (s + 1) as Step)}
              disabled={disabled}
              style={{
                flex: 1, padding: '11px 0', borderRadius: 10, fontSize: 13, fontWeight: 700,
                background: disabled ? '#ede9e1' : 'var(--primary)',
                color: disabled ? 'var(--text-3)' : '#fff',
                border: 'none', cursor: disabled ? 'not-allowed' : 'pointer',
                boxShadow: disabled ? 'none' : '0 4px 12px rgba(10,110,110,0.28)',
                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
              }}>
              다음 <ChevronRight size={15} />
            </button>
          ) : (
            <button
              onClick={handleSave}
              disabled={isAnalyzing || !!analyzeError}
              style={{
                flex: 1, padding: '11px 0', borderRadius: 10, fontSize: 13, fontWeight: 700,
                background: isAnalyzing ? '#ede9e1' : 'var(--primary)',
                color: isAnalyzing ? 'var(--text-3)' : '#fff',
                border: 'none', cursor: isAnalyzing ? 'not-allowed' : 'pointer',
                boxShadow: isAnalyzing ? 'none' : '0 4px 12px rgba(10,110,110,0.28)',
                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
              }}>
              {isAnalyzing
                ? '분석 중...'
                : <><CheckCircle2 size={15} /> 저장 및 등록</>}
            </button>
          )}
        </div>
      </div>

      {/* spin 애니메이션 */}
      <style>{`@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
    </div>
  )
}
