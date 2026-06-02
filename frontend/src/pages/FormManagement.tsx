import { useState, useRef, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plus, Send, ChevronRight, AlertCircle, Paperclip, X, Image, Save, CheckCircle, MessageSquare, Loader, RefreshCw } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useForms } from '../context/FormsContext'

const BASE = (import.meta as any).env?.VITE_API_URL ?? ''

function sessionHeaders(): Record<string, string> {
  const sid = localStorage.getItem('session_id')
  return sid ? { 'X-Session-ID': sid } : {}
}

function countTbd(md: string) {
  return (md.match(/\bTBD\b/g) ?? []).length
}

function highlightTbd(children: React.ReactNode): React.ReactNode {
  return Array.isArray(children)
    ? children.map((child, i) => highlightTbd(child) as React.ReactNode)
    : typeof children === 'string' && children.includes('TBD')
      ? children.split(/\b(TBD)\b/).map((part, i) =>
          part === 'TBD'
            ? <mark key={i} style={{ background: '#fde68a', color: '#92400e', borderRadius: 3, padding: '0 3px', fontWeight: 700 }}>TBD</mark>
            : part
        )
      : children
}

async function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.readAsDataURL(file)
    reader.onload = () => resolve((reader.result as string).split(',')[1])
    reader.onerror = reject
  })
}

type ChatMsg = {
  role: 'user' | 'assistant'
  text: string
  imageUrl?: string
  streaming?: boolean
}

export function FormManagement() {
  const navigate = useNavigate()
  const { forms } = useForms()
  const [selectedId, setSelectedId] = useState<string>('form_01')
  const [chatInput, setChatInput] = useState('')
  const [chatHistory, setChatHistory] = useState<ChatMsg[]>([])
  const [attachedImage, setAttachedImage] = useState<{ file: File; previewUrl: string } | null>(null)
  const [isSending, setIsSending] = useState(false)
  const [isSaving, setIsSaving] = useState(false)
  const [isSaved, setIsSaved] = useState(false)
  const [savedAt, setSavedAt] = useState<Date | null>(null)
  const [isChatOpen, setIsChatOpen] = useState(false)
  const [saveToast, setSaveToast] = useState<{ msg: string; ok: boolean } | null>(null)
  const [contentByForm, setContentByForm] = useState<Record<string, string>>({})
  const [hashByForm, setHashByForm] = useState<Record<string, string>>({})
  const [isLoadingContent, setIsLoadingContent] = useState(false)
  const [panelTab, setPanelTab] = useState<'chat' | 'history'>('chat')
  type HistoryEntry = { id: number; display_name: string; saved_at: string; content_hash: string; diff: string }
  const [historyByForm, setHistoryByForm] = useState<Record<string, HistoryEntry[]>>({})
  const [isLoadingHistory, setIsLoadingHistory] = useState(false)
  const [expandedDiff, setExpandedDiff] = useState<number | null>(null)
  const [isSyncing, setIsSyncing] = useState(false)
  const [lastSyncedAt, setLastSyncedAt] = useState<Date | null>(null)
  const [syncChanges, setSyncChanges] = useState<string[] | null>(null)
  const chatEndRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const displayMd = contentByForm[selectedId] ?? ''
  const tbdCount = countTbd(displayMd)

  useEffect(() => { chatEndRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [chatHistory])

  useEffect(() => {
    setChatHistory([])
    setIsSaved(false)
    setSavedAt(null)
    if (contentByForm[selectedId]) return  // 이미 로드된 경우 재사용
    setIsLoadingContent(true)
    fetch(`${BASE}/api/v3/forms/${selectedId}`, { headers: sessionHeaders() })
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => {
        setContentByForm(prev => ({ ...prev, [selectedId]: data.content }))
        setHashByForm(prev => ({ ...prev, [selectedId]: data.content_hash }))
      })
      .catch(() => {})
      .finally(() => setIsLoadingContent(false))
  }, [selectedId])

  function handleImageSelect(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    setAttachedImage({ file, previewUrl: URL.createObjectURL(file) })
    e.target.value = ''
  }

  async function handleSend() {
    const text = chatInput.trim()
    if ((!text && !attachedImage) || isSending) return

    const userText = text || '(이미지 첨부)'
    const imageFile = attachedImage?.file
    const imageUrl = attachedImage?.previewUrl

    setIsSaved(false)
    setIsChatOpen(true)

    // 이전 히스토리 + user message를 API 요청에 사용 (streaming 플레이스홀더 제외)
    const prevMessages = chatHistory
      .filter(m => !m.streaming)
      .map(m => ({ role: m.role, content: m.text }))

    setChatHistory(h => [
      ...h,
      { role: 'user', text: userText, imageUrl },
      { role: 'assistant', text: '', streaming: true },
    ])
    setChatInput('')
    setAttachedImage(null)
    setIsSending(true)

    try {
      const imageB64 = imageFile ? await fileToBase64(imageFile) : undefined
      const apiMessages = [
        ...prevMessages,
        {
          role: 'user',
          content: userText,
          ...(imageB64 ? { image_b64: imageB64, image_mime: imageFile?.type } : {}),
        },
      ]

      const res = await fetch(`${BASE}/api/v3/form-manage/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...sessionHeaders() },
        body: JSON.stringify({ form_id: selectedId, messages: apiMessages }),
      })
      if (!res.ok) throw new Error(`서버 오류 ${res.status}`)

      const reader = res.body!.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      let accText = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const data = JSON.parse(line.slice(6))
            if (data.type === 'text') {
              accText += data.text
              const snapshot = accText
              setChatHistory(h => [
                ...h.slice(0, -1),
                { ...h[h.length - 1], text: snapshot },
              ])
            } else if (data.type === 'done') {
              setChatHistory(h => [
                ...h.slice(0, -1),
                { ...h[h.length - 1], streaming: false },
              ])
            } else if (data.type === 'error') {
              setChatHistory(h => [
                ...h.slice(0, -1),
                { ...h[h.length - 1], text: `오류: ${data.message}`, streaming: false },
              ])
            }
          } catch { /* ignore parse errors */ }
        }
      }
    } catch (err) {
      setChatHistory(h => [
        ...h.slice(0, -1),
        { ...h[h.length - 1], text: '오류가 발생했습니다. 다시 시도해 주세요.', streaming: false },
      ])
    } finally {
      setIsSending(false)
    }
  }

  async function handleSave() {
    if (isSaving || isSending) return
    setIsSaving(true)
    setIsSaved(false)
    try {
      const apiMessages = chatHistory
        .filter(m => !m.streaming)
        .map(m => ({ role: m.role, content: m.text }))

      const res = await fetch(`${BASE}/api/v3/form-manage/apply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...sessionHeaders() },
        body: JSON.stringify({
          form_id: selectedId,
          messages: apiMessages,
          expected_hash: hashByForm[selectedId] ?? null,
        }),
      })
      if (res.status === 409) {
        const err = await res.json()
        setChatHistory(h => [...h, { role: 'assistant', text: `⚠️ 저장 충돌: ${err.detail}` }])
        // 최신 내용 다시 fetch해서 hash 갱신
        const fresh = await fetch(`${BASE}/api/v3/forms/${selectedId}`, { headers: sessionHeaders() })
        if (fresh.ok) {
          const data = await fresh.json()
          setContentByForm(prev => ({ ...prev, [selectedId]: data.content }))
          setHashByForm(prev => ({ ...prev, [selectedId]: data.content_hash }))
        }
        return
      }
      if (!res.ok) throw new Error(`서버 오류 ${res.status}`)

      const reader = res.body!.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      let accText = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const data = JSON.parse(line.slice(6))
            if (data.type === 'text') {
              accText += data.text
              const snapshot = accText
              setContentByForm(prev => ({ ...prev, [selectedId]: snapshot }))
            } else if (data.type === 'done') {
              const now = new Date()
              setIsSaved(true)
              setSavedAt(now)
              if (data.content_hash) setHashByForm(prev => ({ ...prev, [selectedId]: data.content_hash }))
              setHistoryByForm(prev => { const n = { ...prev }; delete n[selectedId]; return n })  // 히스토리 캐시 무효화
              const timeStr = now.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })
              const summaryMsg = data.tbd_count > 0
                ? `✅ ${timeStr} 저장 완료. TBD ${data.tbd_count}개가 아직 남아 있습니다.`
                : `✅ ${timeStr} 저장 완료. TBD 항목 없이 모두 확정되었습니다.`
              setChatHistory(h => [...h, { role: 'assistant', text: summaryMsg }])
              showToast('저장 완료', true)
            } else if (data.type === 'error') {
              throw new Error(data.message)
            }
          } catch { /* ignore parse errors */ }
        }
      }
    } catch {
      showToast('저장 실패', false)
    } finally {
      setIsSaving(false)
    }
  }

  async function handleSync() {
    if (isSyncing) return
    setIsSyncing(true)
    setSyncChanges(null)
    try {
      const res = await fetch(`${BASE}/api/v3/forms/${selectedId}/sync`, {
        method: 'POST',
        headers: sessionHeaders(),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: '동기화 실패' }))
        showToast(err.detail ?? '동기화 실패', false)
        return
      }
      const data = await res.json()
      setLastSyncedAt(new Date())
      setSyncChanges(data.changes ?? [])
      showToast(
        data.changes?.length > 0 ? `동기화 완료 (${data.changes.length}개 변경)` : '동기화 완료 (변경 없음)',
        true,
      )
    } catch {
      showToast('동기화 실패', false)
    } finally {
      setIsSyncing(false)
    }
  }

  function showToast(msg: string, ok: boolean) {
    setSaveToast({ msg, ok })
    setTimeout(() => setSaveToast(null), 3000)
  }

  const hasConversation = chatHistory.some(m => m.role === 'assistant' && !m.streaming)
  const hasUnsaved = hasConversation && !isSaved

  // 탭 닫기 / 새로고침 경고 (BrowserRouter 환경에서는 이것만 가능)
  useEffect(() => {
    if (!hasUnsaved) return
    const handler = (e: BeforeUnloadEvent) => { e.preventDefault() }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [hasUnsaved])

  const unreadCount = chatHistory.filter(m => !m.streaming).length

  return (
    <div style={{ display: 'flex', height: '100%', overflow: 'hidden' }}>
      {/* 좌: 양식 목록 */}
      <aside style={{
        width: 240, flexShrink: 0,
        borderRight: '1px solid var(--border)',
        display: 'flex', flexDirection: 'column',
        background: 'var(--card)',
      }}>
        <div style={{ padding: '20px 16px 12px', borderBottom: '1px solid var(--border)' }}>
          <h1 style={{ fontSize: 15, fontWeight: 700, color: 'var(--text-1)', marginBottom: 2 }}>Form 관리</h1>
          <p style={{ fontSize: 11, color: 'var(--text-3)' }}>양식 정의 MD 파일</p>
        </div>

        <nav style={{ flex: 1, overflowY: 'auto', padding: '10px 10px' }}>
          {forms.map(form => {
            const active = form.id === selectedId
            const tbd = countTbd(contentByForm[form.id] ?? '')
            return (
              <button
                key={form.id}
                onClick={() => setSelectedId(form.id)}
                style={{
                  width: '100%', textAlign: 'left',
                  padding: '11px 12px', borderRadius: 9, marginBottom: 4,
                  background: active ? 'var(--primary-light)' : 'transparent',
                  border: `1px solid ${active ? 'rgba(10,110,110,0.25)' : 'transparent'}`,
                  cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8,
                }}
              >
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 2 }}>
                    <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--text-1)', fontFamily: 'var(--mono)' }}>{form.id}</span>
                    {tbd > 0 && (
                      <span style={{
                        display: 'inline-flex', alignItems: 'center', gap: 3,
                        fontSize: 10, fontWeight: 700, color: '#c4622c',
                        background: '#fdf0e8', borderRadius: 20, padding: '1px 6px',
                      }}>
                        <AlertCircle size={8} />TBD {tbd}
                      </span>
                    )}
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-2)' }}>{form.name}</div>
                  {form.lastEditor ? (
                    <div style={{ fontSize: 10, color: 'var(--text-3)' }}>
                      {form.lastEditor} · {new Date(form.lastEditedAt!).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })}
                    </div>
                  ) : (
                    <div style={{ fontSize: 10, color: 'var(--text-3)' }}>{form.issuer}</div>
                  )}
                </div>
                <ChevronRight size={13} color="var(--text-3)" />
              </button>
            )
          })}
        </nav>

        <div style={{ padding: '10px' }}>
          <button
            onClick={() => navigate('/cold-start')}
            style={{
              width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
              background: 'var(--primary)', color: '#fff', border: 'none',
              borderRadius: 9, padding: '10px 0',
              fontSize: 12, fontWeight: 600, cursor: 'pointer',
              boxShadow: '0 3px 10px rgba(10,110,110,0.28)',
            }}
          >
            <Plus size={13} />
            신규 양식 등록
          </button>
        </div>
      </aside>

      {/* 우: MD 뷰어 + 채팅 슬라이드 패널 */}
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden', minWidth: 0 }}>

        {/* MD 뷰어 영역 */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', minWidth: 0 }}>
          {selectedId ? (
            <>
              {/* 헤더 */}
              <div style={{
                padding: '14px 24px', borderBottom: '1px solid var(--border)',
                display: 'flex', alignItems: 'center', gap: 10,
                background: 'var(--card)', flexShrink: 0,
              }}>
                <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-1)', fontFamily: 'var(--mono)', flex: 1 }}>
                  {selectedId}.md
                </span>
                {tbdCount > 0 && (
                  <span style={{
                    display: 'inline-flex', alignItems: 'center', gap: 4,
                    fontSize: 11, fontWeight: 600, color: '#c4622c',
                    background: '#fdf0e8', borderRadius: 20, padding: '3px 9px',
                  }}>
                    <AlertCircle size={10} />
                    TBD {tbdCount}개
                  </span>
                )}
                {/* 동기화 버튼 */}
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 2 }}>
                  <button
                    onClick={handleSync}
                    disabled={isSyncing}
                    title="form_XX.md → form_types.json 동기화"
                    style={{
                      display: 'flex', alignItems: 'center', gap: 5,
                      padding: '7px 13px', borderRadius: 8,
                      border: '1px solid var(--border)',
                      background: lastSyncedAt && !isSyncing ? 'var(--primary-light)' : 'var(--card)',
                      color: isSyncing ? 'var(--text-3)' : lastSyncedAt ? 'var(--primary)' : 'var(--text-2)',
                      fontSize: 12, fontWeight: 600, cursor: isSyncing ? 'not-allowed' : 'pointer',
                      transition: 'all 0.15s',
                    }}
                  >
                    <RefreshCw
                      size={12}
                      style={{ animation: isSyncing ? 'spin 0.8s linear infinite' : 'none' }}
                    />
                    {isSyncing ? '동기화 중...' : '동기화'}
                  </button>
                  {lastSyncedAt && (
                    <span style={{ fontSize: 10, color: 'var(--text-3)' }}>
                      {lastSyncedAt.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })}
                    </span>
                  )}
                </div>

                {/* 채팅 토글 버튼 */}
                <button
                  onClick={() => setIsChatOpen(o => !o)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    padding: '7px 16px', borderRadius: 8, flexShrink: 0,
                    border: 'none',
                    background: isChatOpen ? '#ede9e1' : 'var(--primary)',
                    color: isChatOpen ? 'var(--text-2)' : '#fff',
                    fontSize: 12, fontWeight: 600, cursor: 'pointer',
                    boxShadow: isChatOpen ? 'none' : '0 2px 8px rgba(10,110,110,0.28)',
                    position: 'relative',
                    transition: 'background 0.15s',
                  }}
                >
                  <MessageSquare size={13} />
                  {isChatOpen ? '채팅 닫기' : '채팅으로 수정'}
                  {!isChatOpen && unreadCount > 0 && (
                    <span style={{
                      position: 'absolute', top: -5, right: -5,
                      background: '#e55', color: '#fff',
                      fontSize: 9, fontWeight: 700, borderRadius: 10,
                      padding: '1px 5px', minWidth: 14, textAlign: 'center',
                    }}>
                      {unreadCount}
                    </span>
                  )}
                </button>
              </div>

              {/* MD 렌더링 */}
              <div style={{ flex: 1, overflowY: 'auto', padding: '24px 32px', background: 'var(--bg)' }}>
                {isLoadingContent ? (
                  <div style={{
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    height: '100%', gap: 8, color: 'var(--text-3)',
                  }}>
                    <Loader size={16} style={{ animation: 'spin 1s linear infinite' }} />
                    <span style={{ fontSize: 13 }}>불러오는 중...</span>
                  </div>
                ) : displayMd ? (
                  <div className="md-content">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        // TBD를 강조 표시
                        p: ({ children }) => <p>{highlightTbd(children)}</p>,
                        li: ({ children }) => <li>{highlightTbd(children)}</li>,
                        td: ({ children }) => <td>{highlightTbd(children)}</td>,
                      }}
                    >{displayMd}</ReactMarkdown>
                  </div>
                ) : (
                  <div style={{
                    display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                    height: '100%', gap: 10, color: 'var(--text-3)',
                  }}>
                    <p style={{ fontSize: 13, fontWeight: 500 }}>MD 파일 준비 중</p>
                    <p style={{ fontSize: 12 }}>Cold-start 완료 후 양식 정의가 자동 생성됩니다</p>
                  </div>
                )}
              </div>
            </>
          ) : (
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-3)', fontSize: 13 }}>
              좌측에서 양식을 선택하세요
            </div>
          )}
        </div>

        {/* 채팅 슬라이드 패널 */}
        <div style={{
          width: isChatOpen ? 340 : 0,
          flexShrink: 0,
          overflow: 'hidden',
          borderLeft: isChatOpen ? '1px solid var(--border)' : 'none',
          transition: 'width 0.22s ease',
          background: 'var(--card)',
          display: 'flex',
          flexDirection: 'column',
        }}>
          <div style={{ width: 340, height: '100%', display: 'flex', flexDirection: 'column' }}>

            {/* 패널 헤더 */}
            <div style={{
              padding: '10px 16px', borderBottom: '1px solid var(--border)',
              display: 'flex', alignItems: 'center', gap: 6,
              flexShrink: 0,
            }}>
              {(['chat', 'history'] as const).map(tab => (
                <button
                  key={tab}
                  onClick={() => {
                    setPanelTab(tab)
                    if (tab === 'history' && !historyByForm[selectedId]) {
                      setIsLoadingHistory(true)
                      fetch(`${BASE}/api/v3/forms/${selectedId}/history`, { headers: sessionHeaders() })
                        .then(r => r.ok ? r.json() : [])
                        .then(data => setHistoryByForm(prev => ({ ...prev, [selectedId]: data })))
                        .catch(() => {})
                        .finally(() => setIsLoadingHistory(false))
                    }
                  }}
                  style={{
                    padding: '4px 12px', borderRadius: 6, border: 'none',
                    background: panelTab === tab ? 'var(--primary-light)' : 'transparent',
                    color: panelTab === tab ? 'var(--primary)' : 'var(--text-3)',
                    fontSize: 12, fontWeight: panelTab === tab ? 700 : 400,
                    cursor: 'pointer',
                  }}
                >
                  {tab === 'chat' ? '대화' : '변경 이력'}
                </button>
              ))}
              <button
                onClick={() => setIsChatOpen(false)}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-3)', padding: 2, display: 'flex', marginLeft: 'auto' }}
              >
                <X size={14} />
              </button>
            </div>

            {/* 탭 콘텐츠 */}
            <div style={{ flex: 1, overflowY: 'auto', padding: '12px', display: 'flex', flexDirection: 'column', gap: 8 }}>
              {panelTab === 'chat' ? (
                chatHistory.length === 0 ? (
                  <div style={{ color: 'var(--text-3)', fontSize: 12, textAlign: 'center', marginTop: 40, lineHeight: 1.6 }}>
                    <MessageSquare size={24} style={{ margin: '0 auto 8px', opacity: 0.3 }} />
                    <p>{selectedId} 수정 요청을 입력해 주세요</p>
                    <p style={{ fontSize: 11 }}>이미지 첨부도 가능합니다</p>
                  </div>
                ) : (
                  <>
                    {chatHistory.map((msg, i) => (
                      <div key={i} style={{
                        display: 'flex', flexDirection: 'column', gap: 4,
                        alignSelf: msg.role === 'user' ? 'flex-end' : 'flex-start',
                        maxWidth: '90%',
                      }}>
                        {msg.imageUrl && (
                          <img src={msg.imageUrl} alt="첨부 이미지" style={{
                            maxWidth: 160, maxHeight: 110, borderRadius: 7,
                            border: '1px solid var(--border)', objectFit: 'cover',
                            alignSelf: 'flex-end',
                          }} />
                        )}
                        <div style={{
                          padding: '7px 11px', borderRadius: 8, fontSize: 12,
                          background: msg.role === 'user' ? 'var(--primary-light)' : '#ede9e1',
                          color: msg.role === 'user' ? 'var(--primary)' : 'var(--text-2)',
                          whiteSpace: 'pre-wrap', lineHeight: 1.5,
                        }}>
                          {msg.streaming && !msg.text ? '...' : msg.text}
                          {msg.streaming && <span style={{ opacity: 0.5 }}>▌</span>}
                        </div>
                      </div>
                    ))}
                    <div ref={chatEndRef} />
                  </>
                )
              ) : (
                /* 변경 이력 탭 */
                isLoadingHistory ? (
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 8, color: 'var(--text-3)' }}>
                    <Loader size={14} style={{ animation: 'spin 1s linear infinite' }} />
                    <span style={{ fontSize: 12 }}>불러오는 중...</span>
                  </div>
                ) : (historyByForm[selectedId] ?? []).length === 0 ? (
                  <div style={{ color: 'var(--text-3)', fontSize: 12, textAlign: 'center', marginTop: 40 }}>
                    <p>저장 이력이 없습니다</p>
                  </div>
                ) : (
                  (historyByForm[selectedId] ?? []).map(entry => {
                    const dt = new Date(entry.saved_at)
                    const dateStr = dt.toLocaleDateString('ko-KR', { month: 'short', day: 'numeric' })
                    const timeStr = dt.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })
                    const isExpanded = expandedDiff === entry.id
                    return (
                      <div key={entry.id} style={{
                        border: '1px solid var(--border)', borderRadius: 8,
                        overflow: 'hidden', fontSize: 12,
                      }}>
                        <div
                          onClick={() => setExpandedDiff(isExpanded ? null : entry.id)}
                          style={{
                            padding: '8px 12px', cursor: 'pointer',
                            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                            background: 'var(--bg)',
                          }}
                        >
                          <div>
                            <span style={{ fontWeight: 600, color: 'var(--text-1)' }}>{entry.display_name}</span>
                            <span style={{ color: 'var(--text-3)', marginLeft: 6 }}>{dateStr} {timeStr}</span>
                          </div>
                          <span style={{ color: 'var(--text-3)', fontSize: 10 }}>{isExpanded ? '▲' : '▼'}</span>
                        </div>
                        {isExpanded && (
                          <div style={{
                            padding: '8px 12px', background: '#1e1e1e', borderTop: '1px solid var(--border)',
                            fontFamily: 'var(--mono)', fontSize: 11, lineHeight: 1.6,
                            overflowX: 'auto', whiteSpace: 'pre',
                          }}>
                            {entry.diff ? entry.diff.split('\n').map((line, i) => (
                              <div key={i} style={{
                                color: line.startsWith('+') ? '#4ec9b0' : line.startsWith('-') ? '#f48771' : line.startsWith('@') ? '#569cd6' : '#d4d4d4',
                                background: line.startsWith('+') ? 'rgba(78,201,176,0.08)' : line.startsWith('-') ? 'rgba(244,135,113,0.08)' : 'transparent',
                              }}>{line || ' '}</div>
                            )) : <span style={{ color: '#888' }}>(변경 내용 없음)</span>}
                          </div>
                        )}
                      </div>
                    )
                  })
                )
              )}
            </div>

            {/* 저장 버튼 행 (대화 탭 전용) */}
            {panelTab === 'chat' && hasConversation && (
              <div style={{
                padding: '8px 12px', borderTop: '1px solid var(--border)',
                display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0,
              }}>
                <button
                  onClick={handleSave}
                  disabled={isSaving || isSending || isSaved}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 5,
                    padding: '6px 14px', borderRadius: 7, border: 'none',
                    background: isSaving ? '#ede9e1' : isSaved ? '#d1fae5' : 'var(--primary)',
                    color: isSaving ? 'var(--text-3)' : isSaved ? '#065f46' : '#fff',
                    fontSize: 12, fontWeight: 600,
                    cursor: isSaving || isSending || isSaved ? 'default' : 'pointer',
                    boxShadow: isSaving || isSaved ? 'none' : '0 2px 8px rgba(10,110,110,0.25)',
                    transition: 'background 0.2s, color 0.2s',
                  }}
                >
                  {isSaved ? <CheckCircle size={12} /> : <Save size={12} />}
                  {isSaving ? '저장 중...' : isSaved ? '저장 완료' : '저장'}
                </button>
                {isSaved && savedAt && (
                  <span style={{ fontSize: 11, color: 'var(--text-3)' }}>
                    {savedAt.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })}
                  </span>
                )}
                <button
                  onClick={() => {
                    if (!window.confirm('대화 내역을 모두 삭제하겠습니까?\n저장하지 않은 변경 제안은 사라집니다.')) return
                    setChatHistory([]); setIsSaved(false); setSavedAt(null)
                  }}
                  disabled={isSending || isSaving}
                  style={{
                    padding: '6px 10px', borderRadius: 7,
                    border: '1px solid var(--border)',
                    background: 'transparent', color: 'var(--text-3)',
                    fontSize: 12, cursor: 'pointer', marginLeft: 'auto',
                  }}
                >
                  초기화
                </button>
                {saveToast && (
                  <span style={{ fontSize: 11, fontWeight: 600, color: saveToast.ok ? 'var(--primary)' : '#dc2626' }}>
                    {saveToast.msg}
                  </span>
                )}
              </div>
            )}

            {/* 이미지 미리보기 (대화 탭 전용) */}
            {panelTab === 'chat' && attachedImage && (
              <div style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '8px 12px', background: 'var(--bg)',
                borderTop: '1px solid var(--border)', flexShrink: 0,
              }}>
                <img
                  src={attachedImage.previewUrl}
                  alt="preview"
                  style={{ width: 40, height: 30, objectFit: 'cover', borderRadius: 4, border: '1px solid var(--border)' }}
                />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <p style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-1)', marginBottom: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {attachedImage.file.name}
                  </p>
                  <p style={{ fontSize: 10, color: 'var(--text-3)' }}>
                    {(attachedImage.file.size / 1024).toFixed(0)} KB
                  </p>
                </div>
                <button
                  onClick={() => setAttachedImage(null)}
                  style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-3)', padding: 2 }}
                >
                  <X size={13} />
                </button>
              </div>
            )}

            {/* 빠른 요청 버튼 (대화 탭 전용) */}
            {panelTab === 'chat' && chatHistory.length === 0 && (
              <div style={{ padding: '0 12px 8px', display: 'flex', gap: 6, flexWrap: 'wrap', flexShrink: 0 }}>
                {[
                  { label: 'TBD 전부 확인해줘', icon: '🔍' },
                  { label: '전체 내용 검토해줘', icon: '📋' },
                  { label: '변경 사항 요약해줘', icon: '📝' },
                ].map(({ label, icon }) => (
                  <button
                    key={label}
                    onClick={() => { setChatInput(label) }}
                    disabled={isSending}
                    style={{
                      padding: '5px 10px', borderRadius: 20,
                      border: '1px solid var(--border)',
                      background: 'var(--bg)', color: 'var(--text-2)',
                      fontSize: 11, cursor: 'pointer', whiteSpace: 'nowrap',
                    }}
                  >
                    {icon} {label}
                  </button>
                ))}
              </div>
            )}

            {/* 입력창 (대화 탭 전용) */}
            {panelTab === 'chat' && <div style={{ padding: '8px 12px 12px', flexShrink: 0, borderTop: attachedImage ? 'none' : '1px solid var(--border)' }}>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                style={{ display: 'none' }}
                onChange={handleImageSelect}
              />
              <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <button
                  onClick={() => fileInputRef.current?.click()}
                  title="이미지 첨부"
                  style={{
                    width: 34, height: 34, borderRadius: 8, flexShrink: 0,
                    border: `1.5px solid ${attachedImage ? 'var(--primary)' : 'var(--border)'}`,
                    background: attachedImage ? 'var(--primary-light)' : 'transparent',
                    color: attachedImage ? 'var(--primary)' : 'var(--text-3)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    cursor: 'pointer',
                  }}
                >
                  {attachedImage ? <Image size={14} /> : <Paperclip size={13} />}
                </button>
                <input
                  value={chatInput}
                  onChange={e => setChatInput(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && !e.shiftKey && handleSend()}
                  disabled={isSending}
                  placeholder={attachedImage ? '이미지 설명 추가 (선택)' : '수정 요청 입력...'}
                  style={{
                    flex: 1, border: '1.5px solid var(--border)', borderRadius: 8,
                    padding: '8px 12px', fontSize: 12, outline: 'none',
                    background: isSending ? '#f5f5f5' : 'var(--bg)',
                    color: 'var(--text-1)', fontFamily: 'inherit',
                  }}
                />
                <button
                  onClick={handleSend}
                  disabled={(!chatInput.trim() && !attachedImage) || isSending}
                  style={{
                    width: 34, height: 34, borderRadius: 8, border: 'none', flexShrink: 0,
                    background: (!chatInput.trim() && !attachedImage) || isSending ? '#ede9e1' : 'var(--primary)',
                    color: (!chatInput.trim() && !attachedImage) || isSending ? 'var(--text-3)' : '#fff',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    cursor: (!chatInput.trim() && !attachedImage) || isSending ? 'not-allowed' : 'pointer',
                    boxShadow: (!chatInput.trim() && !attachedImage) || isSending ? 'none' : '0 2px 8px rgba(10,110,110,0.28)',
                  }}
                >
                  <Send size={13} />
                </button>
              </div>
            </div>}

          </div>
        </div>

      </div>
    </div>
  )
}
