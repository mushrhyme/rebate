import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Upload as UploadIcon, FileText, X, Info, Loader2, CheckCircle2, AlertCircle } from 'lucide-react'
import { api } from '../api/client'

function defaultHatsuMonth(): string {
  const now = new Date()
  const y = now.getFullYear()
  const m = String(now.getMonth() + 1).padStart(2, '0')
  return `${y}-${m}`
}

function makeDocId(filename: string): string {
  let s = filename.replace(/\.pdf$/i, '')
  s = s.replace(/\s*\([^)]*\)/g, '')
  s = s.replace(/[\s.]+/g, '_')
  s = s.replace(/[^\w\-]/g, '')
  s = s.replace(/_+/g, '_').replace(/^_|_$/g, '')
  return s
}

type FileStatus =
  | { type: 'pending' }
  | { type: 'uploading' }
  | { type: 'ok'; docId: string }
  | { type: 'conflict'; docId: string; detail: string }
  | { type: 'error'; message: string }

export function Upload() {
  const navigate = useNavigate()
  const [dragging, setDragging] = useState(false)
  const [files, setFiles] = useState<File[]>([])
  const [statuses, setStatuses] = useState<FileStatus[]>([])
  const [loading, setLoading] = useState(false)
  const [hatsuMonth, setHatsuMonth] = useState(defaultHatsuMonth)

  const addFiles = (fileList: FileList | null) => {
    if (!fileList) return
    const pdfs = Array.from(fileList).filter(f => f.name.endsWith('.pdf'))
    setFiles(prev => [...prev, ...pdfs])
    setStatuses([])
  }

  function toSystemMonth(v: string) { return v.replace('-', '.') }

  async function handleStart() {
    if (!files.length) return
    setLoading(true)
    setStatuses(files.map(() => ({ type: 'uploading' })))

    // 업로드 전에 기존 문서 목록 조회 → 즉시 충돌 감지
    let existingMap = new Map<string, string>()
    try {
      const existing = await api.listDocuments()
      existingMap = new Map(existing.map(d => [d.doc_id, d.status]))
    } catch { /* 조회 실패 시 그냥 업로드 진행 */ }

    const preChecked: FileStatus[] = files.map(f => {
      const docId = makeDocId(f.name)
      const st = existingMap.get(docId)
      if (st && st !== 'error') {
        return { type: 'conflict', docId, detail: `이미 분석된 문서입니다 (상태: ${st})` }
      }
      return { type: 'uploading' }
    })
    setStatuses(preChecked)

    // 충돌이 아닌 파일만 실제 업로드
    const finalStatuses = [...preChecked]
    await Promise.allSettled(
      files.map(async (f, i) => {
        if (preChecked[i].type === 'conflict') return
        try {
          const res = await api.uploadDocument(f, toSystemMonth(hatsuMonth))
          finalStatuses[i] = { type: 'ok', docId: res.doc_id }
          setStatuses(prev => {
            const next = [...prev]; next[i] = finalStatuses[i]; return next
          })
        } catch (e) {
          const msg = e instanceof Error ? e.message : '업로드 실패'
          const isConflict = msg.includes('이미 분석') || msg.includes('이미 분석 중')
          finalStatuses[i] = isConflict
            ? { type: 'conflict', docId: makeDocId(f.name), detail: msg }
            : { type: 'error', message: msg }
          setStatuses(prev => {
            const next = [...prev]; next[i] = finalStatuses[i]; return next
          })
        }
      })
    )

    setLoading(false)
    if (finalStatuses.every(s => s.type === 'ok')) navigate('/')
  }

  const isDone = statuses.length > 0 && !loading
  const hasConflict = statuses.some(s => s.type === 'conflict')
  const hasError = statuses.some(s => s.type === 'error')
  const successCount = statuses.filter(s => s.type === 'ok').length

  return (
    <div style={{ padding: '40px', maxWidth: 680, margin: '0 auto' }}>
      <div style={{ marginBottom: 32 }}>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: 'var(--text-1)', marginBottom: 6 }}>청구서 업로드</h1>
        <p style={{ fontSize: 13, color: 'var(--text-2)' }}>PDF 파일을 업로드하면 자동으로 분석이 시작됩니다</p>
      </div>

      {/* 청구연월 선택 */}
      <div style={{
        marginBottom: 24, padding: '18px 20px',
        background: 'var(--card)', border: '1px solid var(--border)',
        borderRadius: 14, boxShadow: 'var(--shadow-sm)',
      }}>
        <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--text-2)', marginBottom: 8 }}>
          청구연월
        </label>
        <input
          type="month"
          value={hatsuMonth}
          onChange={e => setHatsuMonth(e.target.value)}
          style={{
            fontSize: 15, fontWeight: 600, color: 'var(--text-1)',
            border: '1.5px solid var(--border)', borderRadius: 8,
            padding: '7px 12px', outline: 'none', background: 'var(--bg)',
            cursor: 'pointer',
          }}
        />
        <p style={{ marginTop: 8, fontSize: 11, color: 'var(--text-3)' }}>
          청구서에 기재된 발생월입니다. 업로드 날짜와 다를 수 있습니다.
        </p>
      </div>

      {/* 드롭존 — 결과 표시 전에만 */}
      {!isDone && (
        <div
          onDragOver={e => { e.preventDefault(); setDragging(true) }}
          onDragLeave={() => setDragging(false)}
          onDrop={e => { e.preventDefault(); setDragging(false); addFiles(e.dataTransfer.files) }}
          style={{
            border: `2px dashed ${dragging ? 'var(--primary)' : 'var(--border)'}`,
            borderRadius: 16, padding: '52px 24px', textAlign: 'center',
            background: dragging ? 'var(--primary-light)' : 'var(--card)',
            transition: 'all 0.15s', cursor: 'pointer',
          }}
        >
          <div style={{
            width: 56, height: 56, borderRadius: 14,
            background: dragging ? 'var(--primary)' : '#ede9e1',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            margin: '0 auto 16px', transition: 'all 0.15s',
          }}>
            <UploadIcon size={24} color={dragging ? '#fff' : 'var(--text-3)'} />
          </div>
          <p style={{ fontSize: 15, fontWeight: 600, color: 'var(--text-1)', marginBottom: 6 }}>
            PDF를 드래그하거나 클릭해서 선택
          </p>
          <p style={{ fontSize: 12, color: 'var(--text-2)', marginBottom: 20 }}>복수 파일 동시 업로드 가능</p>
          <label style={{
            display: 'inline-block', background: 'var(--primary)', color: '#fff',
            borderRadius: 9, padding: '10px 24px', fontSize: 13, fontWeight: 600, cursor: 'pointer',
            boxShadow: '0 4px 12px rgba(10,110,110,0.28)',
          }}>
            파일 선택
            <input type="file" accept=".pdf" multiple onChange={e => addFiles(e.target.files)} style={{ display: 'none' }} />
          </label>
        </div>
      )}

      {/* 파일 목록 */}
      {files.length > 0 && (
        <div style={{ marginTop: 20, display: 'flex', flexDirection: 'column', gap: 8 }}>
          {files.map((f, i) => {
            const st: FileStatus = statuses[i] ?? { type: 'pending' }
            const isConflict = st.type === 'conflict'
            const isErr = st.type === 'error'
            const isOk = st.type === 'ok'
            const isUploading = st.type === 'uploading'

            return (
              <div key={i} style={{
                display: 'flex', alignItems: 'flex-start', gap: 14,
                background: isConflict ? '#fdf0e8' : isErr ? '#fae8e8' : isOk ? '#f0faf4' : 'var(--card)',
                border: `1px solid ${isConflict ? '#dbb590' : isErr ? '#f0c8c8' : isOk ? 'rgba(10,110,110,0.2)' : 'var(--border)'}`,
                borderRadius: 12, padding: '12px 16px', boxShadow: 'var(--shadow-sm)',
              }}>
                <div style={{
                  width: 36, height: 36, borderRadius: 9, flexShrink: 0,
                  background: isConflict ? '#fdf0e8' : isErr ? '#fae8e8' : isOk ? '#e0f0e8' : 'var(--primary-light)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}>
                  {isUploading
                    ? <Loader2 size={16} color="var(--primary)" style={{ animation: 'spin 0.8s linear infinite' }} />
                    : isOk
                    ? <CheckCircle2 size={16} color="#2d7d4a" />
                    : isConflict
                    ? <AlertCircle size={16} color="#c4622c" />
                    : isErr
                    ? <AlertCircle size={16} color="#b03030" />
                    : <FileText size={16} color="var(--primary)" />
                  }
                </div>

                <div style={{ flex: 1, minWidth: 0 }}>
                  <p style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-1)', marginBottom: 2 }}>{f.name}</p>

                  {st.type === 'pending' && (
                    <p style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'var(--mono)' }}>
                      대기 중
                    </p>
                  )}
                  {st.type === 'uploading' && (
                    <p style={{ fontSize: 11, color: 'var(--primary)', fontFamily: 'var(--mono)' }}>
                      업로드 중...
                    </p>
                  )}
                  {st.type === 'ok' && (
                    <p style={{ fontSize: 11, color: '#2d7d4a', fontFamily: 'var(--mono)' }}>
                      분석 시작됨 · {st.docId}
                    </p>
                  )}
                  {st.type === 'conflict' && (
                    <>
                      <p style={{ fontSize: 11, fontWeight: 600, color: '#c4622c', marginBottom: 3 }}>
                        이미 존재하는 문서
                      </p>
                      <p style={{ fontSize: 11, color: '#8a4a20', fontFamily: 'var(--mono)', marginBottom: 2 }}>
                        doc_id: {st.docId}
                      </p>
                      <p style={{ fontSize: 11, color: '#8a4a20' }}>
                        {st.detail} — 대시보드에서 해당 문서를 삭제 후 재업로드하세요
                      </p>
                    </>
                  )}
                  {st.type === 'error' && (
                    <p style={{ fontSize: 11, color: '#b03030' }}>{st.message}</p>
                  )}
                </div>

                {st.type === 'pending' && !loading && (
                  <button
                    onClick={() => {
                      setFiles(prev => prev.filter((_, j) => j !== i))
                      setStatuses([])
                    }}
                    style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-3)', padding: 4, flexShrink: 0 }}
                  >
                    <X size={15} />
                  </button>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* 결과 요약 */}
      {isDone && (hasConflict || hasError) && (
        <div style={{
          marginTop: 16, padding: '13px 16px', borderRadius: 10,
          background: '#fdf0e8', border: '1px solid #dbb590',
          fontSize: 12, color: '#8a4a20', lineHeight: 1.7,
        }}>
          {successCount > 0 && <span><strong>{successCount}건</strong> 분석 시작됨. </span>}
          {hasConflict && <span>위 주황색 파일은 이미 분석된 문서입니다. 대시보드에서 doc_id로 찾아 삭제 후 재업로드하세요.</span>}
        </div>
      )}

      {!isDone && (
        <div style={{
          marginTop: 20, display: 'flex', alignItems: 'flex-start', gap: 10,
          background: 'var(--primary-light)', border: '1px solid rgba(10,110,110,0.2)',
          borderRadius: 10, padding: '13px 16px', fontSize: 12, color: 'var(--primary)',
        }}>
          <Info size={14} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>각 문서는 독립적으로 병렬 처리됩니다. 한 문서에서 매핑 확인이 필요해도 나머지 문서는 계속 분석됩니다.</span>
        </div>
      )}

      {/* 버튼 */}
      <div style={{ marginTop: 28, display: 'flex', gap: 12 }}>
        <button
          onClick={() => navigate('/')}
          disabled={loading}
          style={{
            padding: '11px 20px', borderRadius: 10, fontSize: 13, fontWeight: 500,
            background: 'var(--card)', border: '1px solid var(--border)',
            color: 'var(--text-2)', cursor: loading ? 'not-allowed' : 'pointer',
            opacity: loading ? 0.5 : 1,
          }}
        >
          {isDone ? '대시보드로' : '취소'}
        </button>
        {!isDone && (
          <button
            disabled={files.length === 0 || loading}
            onClick={handleStart}
            style={{
              flex: 1, padding: '11px 0', borderRadius: 10, fontSize: 13, fontWeight: 700,
              background: files.length > 0 ? 'var(--primary)' : '#ede9e1',
              color: files.length > 0 ? '#fff' : 'var(--text-3)',
              border: 'none', cursor: files.length > 0 && !loading ? 'pointer' : 'not-allowed',
              boxShadow: files.length > 0 ? '0 4px 14px rgba(10,110,110,0.28)' : 'none',
              display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
              transition: 'all 0.15s',
            }}
          >
            {loading ? (
              <>
                <Loader2 size={15} style={{ animation: 'spin 0.8s linear infinite' }} />
                업로드 중...
              </>
            ) : (
              `분석 시작 ${files.length > 0 ? `(${files.length}건)` : ''}`
            )}
          </button>
        )}
        {isDone && (hasConflict || hasError) && (
          <button
            onClick={() => { setFiles([]); setStatuses([]) }}
            style={{
              flex: 1, padding: '11px 0', borderRadius: 10, fontSize: 13, fontWeight: 700,
              background: 'var(--primary)', color: '#fff', border: 'none', cursor: 'pointer',
              boxShadow: '0 4px 14px rgba(10,110,110,0.28)',
            }}
          >
            다시 업로드
          </button>
        )}
      </div>

      <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
    </div>
  )
}
