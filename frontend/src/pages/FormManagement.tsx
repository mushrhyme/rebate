import { useState, useRef, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plus, Send, ChevronRight, AlertCircle, Paperclip, X, Image, Save, CheckCircle, MessageSquare, Loader, RefreshCw, Trash2, Zap } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useForms } from '../context/FormsContext'
import { FormChangePreview } from '../components/FormChangePreview'

const BASE = (import.meta as any).env?.VITE_API_URL ?? ''

function sessionHeaders(): Record<string, string> {
  const sid = localStorage.getItem('session_id')
  return sid ? { 'X-Session-ID': sid } : {}
}

// 대화 보관 — 양식별로 비스트리밍 메시지(역할·텍스트)를 localStorage에 저장한다.
// 세션 만료·새로고침으로 화면이 날아가도 같은 양식으로 돌아오면 대화가 복원된다.
const chatKey = (formId: string) => `form_chat_${formId}`

function loadChat(formId: string): ChatMsg[] {
  try {
    const raw = localStorage.getItem(chatKey(formId))
    if (!raw) return []
    const arr = JSON.parse(raw)
    return Array.isArray(arr)
      ? arr.filter((m: any) => m && typeof m.text === 'string').map((m: any) => ({ role: m.role, text: m.text }))
      : []
  } catch { return [] }
}

// 세션 만료(401) 처리 — 대화는 이미 localStorage에 보관되므로, 토큰만 비우고 로그인으로 보낸다.
// 재로그인 후 같은 양식으로 돌아오면 loadChat이 그대로 복원한다.
function handleAuthExpiry() {
  localStorage.removeItem('session_id')
  if (window.location.pathname !== '/login') window.location.href = '/login'
}

function countTbd(md: string) {
  return (md.match(/\bTBD\b/g) ?? []).length
}

function highlightTbd(children: React.ReactNode): React.ReactNode {
  return Array.isArray(children)
    ? children.map((child) => highlightTbd(child) as React.ReactNode)
    : typeof children === 'string' && children.includes('TBD')
      ? children.split(/\b(TBD)\b/).map((part, idx) =>
          part === 'TBD'
            ? <mark key={idx} style={{ background: '#fde68a', color: '#92400e', borderRadius: 3, padding: '0 3px', fontWeight: 700 }}>TBD</mark>
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
  imageUrls?: string[]
  streaming?: boolean
}

export function FormManagement() {
  const navigate = useNavigate()
  const { forms, reload } = useForms()
  const [selectedId, setSelectedId] = useState<string>('form_01')
  const [chatInput, setChatInput] = useState('')
  const [chatHistory, setChatHistory] = useState<ChatMsg[]>([])
  const [attachedImages, setAttachedImages] = useState<{ file: File; previewUrl: string }[]>([])
  const [isSending, setIsSending] = useState(false)
  const [isSaving, setIsSaving] = useState(false)
  const [isApplyingRules, setIsApplyingRules] = useState(false)
  const [isSaved, setIsSaved] = useState(false)
  const [savedAt, setSavedAt] = useState<Date | null>(null)
  const [isChatOpen, setIsChatOpen] = useState(false)
  const [saveToast, setSaveToast] = useState<{ msg: string; ok: boolean; warn?: boolean } | null>(null)
  // 반영 점검 배너 — 동기화/규칙반영 후 엔진에 안 붙은(dev) · 현업 확인(owner) gap을 끝까지 노출 (무음 성공 방지)
  const [wiringNotice, setWiringNotice] = useState<{ dev: string[]; owner: string[] } | null>(null)
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
  const [_syncChanges, setSyncChanges] = useState<string[] | null>(null)
  const [isDeleteOpen, setIsDeleteOpen] = useState(false)
  const [deletePw, setDeletePw] = useState('')
  const [isDeleting, setIsDeleting] = useState(false)
  const [deleteErr, setDeleteErr] = useState<string | null>(null)
  const [panelWidth, setPanelWidth] = useState(340)
  const chatEndRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const chatTextareaRef = useRef<HTMLTextAreaElement>(null)
  const panelResizeRef = useRef<{ startX: number; startWidth: number } | null>(null)

  function startPanelResize(e: React.MouseEvent) {
    e.preventDefault()
    panelResizeRef.current = { startX: e.clientX, startWidth: panelWidth }
    const onMove = (ev: MouseEvent) => {
      if (!panelResizeRef.current) return
      const delta = panelResizeRef.current.startX - ev.clientX
      setPanelWidth(Math.max(280, Math.min(700, panelResizeRef.current.startWidth + delta)))
    }
    const onUp = () => {
      panelResizeRef.current = null
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  const displayMd = contentByForm[selectedId] ?? ''
  const tbdCount = countTbd(displayMd)

  const persistedFormRef = useRef(selectedId)

  useEffect(() => { chatEndRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [chatHistory])

  // 대화 보관 — chatHistory가 바뀔 때마다 localStorage에 저장. 양식이 막 바뀐 직후(아직
  // chatHistory가 이전 양식 것)에는 건너뛴다. 양식 전환 effect가 새 대화로 교체하면 그때 저장된다.
  useEffect(() => {
    if (persistedFormRef.current !== selectedId) {
      persistedFormRef.current = selectedId
      return
    }
    const persistable = chatHistory.filter(m => !m.streaming).map(m => ({ role: m.role, text: m.text }))
    if (persistable.length) localStorage.setItem(chatKey(selectedId), JSON.stringify(persistable))
    else localStorage.removeItem(chatKey(selectedId))
  }, [chatHistory, selectedId])

  useEffect(() => {
    setChatHistory(loadChat(selectedId))  // 보관된 대화 복원 (만료·새로고침에도 보존)
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
    const files = Array.from(e.target.files || [])
    if (!files.length) return
    setAttachedImages(prev => [...prev, ...files.map(f => ({ file: f, previewUrl: URL.createObjectURL(f) }))])
    e.target.value = ''
  }

  async function handleSend() {
    const text = chatInput.trim()
    if ((!text && !attachedImages.length) || isSending) return

    const userText = text || '(이미지 첨부)'
    const imageUrls = attachedImages.map(img => img.previewUrl)

    setIsSaved(false)
    setIsChatOpen(true)

    // 이전 히스토리 + user message를 API 요청에 사용 (streaming 플레이스홀더 제외)
    const prevMessages = chatHistory
      .filter(m => !m.streaming)
      .map(m => ({ role: m.role, content: m.text }))

    setChatHistory(h => [
      ...h,
      { role: 'user', text: userText, imageUrls },
      { role: 'assistant', text: '', streaming: true },
    ])
    setChatInput('')
    setAttachedImages([])
    setIsSending(true)
    if (chatTextareaRef.current) chatTextareaRef.current.style.height = 'auto'

    try {
      const imagesB64 = await Promise.all(
        attachedImages.map(async img => ({ b64: await fileToBase64(img.file), mime: img.file.type }))
      )
      const apiMessages = [
        ...prevMessages,
        {
          role: 'user',
          content: userText,
          ...(imagesB64.length ? { images: imagesB64 } : {}),
        },
      ]

      const res = await fetch(`${BASE}/api/v3/form-manage/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...sessionHeaders() },
        body: JSON.stringify({ form_id: selectedId, messages: apiMessages }),
      })
      if (res.status === 401) {
        // 세션 만료 — 대화는 이미 보관됨. 안내만 남기고 잠시 후 로그인으로 (재로그인 후 복원)
        setChatHistory(h => [
          ...h.slice(0, -1),
          { ...h[h.length - 1], text: '세션이 만료되어 다시 로그인이 필요합니다.\n작성하신 대화는 보관되며, 다시 로그인하면 이어서 사용할 수 있습니다.', streaming: false },
        ])
        setTimeout(handleAuthExpiry, 1800)
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

  // 통합 저장: 1) 산문(문서) 저장 → 2) 실행 규칙(블록) 자동 반영.
  // 사용자가 "설명 변경인지 계산 규칙 변경인지" 판단할 필요 없음 — 백엔드가 자동 분기한다.
  // 산문을 먼저 저장(expected_hash 충돌 검증)하고, 그 다음 규칙을 반영해 hash 충돌을 피한다.
  // 규칙 반영은 /apply-rules가 스스로 변경 여부를 판단(unchanged) → 실제 분기는 백엔드가 결정.
  async function handleSave() {
    if (isSaving || isApplyingRules || isSending) return
    setIsSaving(true)
    setIsSaved(false)

    // 진행 상황을 보여줄 스트리밍 메시지(placeholder) — 단계가 바뀔 때마다 텍스트를 갱신하고,
    // 마지막에 최종 요약으로 확정(streaming:false)한다. 항상 마지막 메시지를 갱신한다.
    setChatHistory(h => [...h, { role: 'assistant', text: '📄 문서 저장 준비 중…', streaming: true }])
    const setProgress = (text: string) =>
      setChatHistory(h => h.map((m, i) =>
        i === h.length - 1 && m.streaming ? { ...m, text } : m))
    const finishProgress = (text: string) =>
      setChatHistory(h => h.map((m, i) =>
        i === h.length - 1 && m.streaming ? { ...m, text, streaming: false } : m))

    // 경과 시간 타이머 — Claude 응답이 막판에 한꺼번에 와도(스트림 버퍼링) "멈춘 듯" 보이지 않게
    // 매초 "··· N초"를 갱신한다. 단계 라벨(phaseText)은 onProgress로 바뀌고, 거기에 경과초를 덧붙인다.
    const startedAt = Date.now()
    let phaseText = '📄 문서 저장 준비 중…'
    const render = () => setProgress(`${phaseText}  ·  ${Math.round((Date.now() - startedAt) / 1000)}초`)
    const onProgress = (text: string) => { phaseText = text; render() }
    const timer = window.setInterval(render, 1000)

    try {
      // ── 1단계: 산문(문서) 저장 ───────────────────────────────
      const prose = await runProseSave(onProgress)
      if (prose.conflict) { finishProgress(`⚠️ 저장 충돌: ${prose.detail ?? ''}`); return }
      if (prose.error) { finishProgress('⚠️ 문서 저장 실패'); showToast('저장 실패', false); return }

      // ── 2단계: 실행 규칙(블록) 자동 반영 ─────────────────────
      setIsApplyingRules(true)
      const rule = await runRuleApply(onProgress)
      setIsApplyingRules(false)

      // ── 결과 합산: placeholder를 최종 요약으로 확정 + 토스트 1개 ──
      const now = new Date()
      const timeStr = now.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })
      setIsSaved(true)
      setSavedAt(now)
      const combined = [`✅ ${timeStr} 저장 완료`, prose.summary, rule.summary]
        .filter(Boolean).join('\n\n')
      finishProgress(combined)
      if (rule.error) showToast('문서 저장됨 · 규칙 반영 실패', false)
      else if (rule.gap) showToast('저장됨 · ⚠ 일부 미반영', false, true)
      else showToast('저장 완료', true)
    } catch {
      finishProgress('⚠️ 저장 중 오류가 발생했습니다.')
      showToast('저장 실패', false)
    } finally {
      window.clearInterval(timer)
      setIsSaving(false)
      setIsApplyingRules(false)
    }
  }

  // 산문(문서) 저장 — 스트리밍. 충돌/실패는 플래그로, 성공은 요약 문자열로 반환(채팅 메시지는 push하지 않음).
  // onProgress로 진행 상황(작성 중인 글자 수)을 호출부의 placeholder에 흘려보낸다.
  async function runProseSave(
    onProgress: (text: string) => void,
  ): Promise<{ conflict?: boolean; error?: boolean; summary?: string; detail?: string }> {
    const apiMessages = chatHistory
      .filter(m => !m.streaming)
      .map(m => ({ role: m.role, content: m.text }))

    onProgress('📄 1/2단계 · 문서 저장 중… (Claude가 양식 문서를 다시 작성합니다)')
    const res = await fetch(`${BASE}/api/v3/form-manage/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...sessionHeaders() },
      body: JSON.stringify({
        form_id: selectedId,
        messages: apiMessages,
        expected_hash: hashByForm[selectedId] ?? null,
      }),
    })
    if (res.status === 401) { handleAuthExpiry(); return { error: true } }  // 대화 보관됨 → 재로그인 후 복원
    if (res.status === 409) {
      const err = await res.json()
      // 최신 내용 다시 fetch해서 hash 갱신
      const fresh = await fetch(`${BASE}/api/v3/forms/${selectedId}`, { headers: sessionHeaders() })
      if (fresh.ok) {
        const data = await fresh.json()
        setContentByForm(prev => ({ ...prev, [selectedId]: data.content }))
        setHashByForm(prev => ({ ...prev, [selectedId]: data.content_hash }))
      }
      return { conflict: true, detail: err.detail }
    }
    if (!res.ok) return { error: true }

    const reader = res.body!.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    let accText = ''
    let lastShown = 0
    let summary = '📄 문서 저장됨'

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
            // 진행 표시는 ~200자마다만 갱신(렌더 과다 방지)
            if (accText.length - lastShown >= 200) {
              lastShown = accText.length
              onProgress(`📄 1/2단계 · 문서 작성 중… (${accText.length.toLocaleString()}자)`)
            }
          } else if (data.type === 'done') {
            if (data.content_hash) setHashByForm(prev => ({ ...prev, [selectedId]: data.content_hash }))
            setHistoryByForm(prev => { const n = { ...prev }; delete n[selectedId]; return n })  // 히스토리 캐시 무효화
            summary = '📄 문서 저장됨'
              + (data.tbd_count > 0 ? ` — TBD ${data.tbd_count}개 남음` : ' — TBD 없이 모두 확정')
              + (data.auto_sync ? ' · 양식 규칙 자동 동기화 진행' : '')
          } else if (data.type === 'error') {
            return { error: true }
          }
        } catch { /* ignore parse errors */ }
      }
    }
    return { summary }
  }

  // 실행 규칙(NET·교차검증·출력)을 [config] 블록에 직접 반영 (산문이 아니라 구조).
  // 채팅 대화 → /apply-rules → Claude가 블록 갱신 → 스키마검증·build·와이어링.
  // 변경 여부는 백엔드(Claude)가 판단(unchanged) — 규칙 변경이 없으면 조용히 넘어간다.
  // 요약 문자열·gap 플래그를 반환하고, 채팅 메시지 push는 호출부(handleSave)가 합산해서 한다.
  async function runRuleApply(
    onProgress: (text: string) => void,
  ): Promise<{ error?: boolean; summary?: string; gap?: boolean }> {
    const apiMessages = chatHistory
      .filter(m => !m.streaming)
      .map(m => ({ role: m.role, content: m.text }))
    onProgress('⚙️ 2/2단계 · 실행 규칙 분석 중… (계산 규칙 변경 여부 확인 + 엔진 연결 점검)')
    const res = await fetch(`${BASE}/api/v3/form-manage/apply-rules`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...sessionHeaders() },
      body: JSON.stringify({ form_id: selectedId, messages: apiMessages }),
    })
    if (res.status === 401) { handleAuthExpiry(); return { error: true } }  // 대화 보관됨 → 재로그인 후 복원
    if (!res.ok) {
      // 에러 본문이 JSON이 아니면(HTML 500·프록시 502 등) 상태코드라도 보여준다
      const err = await res.json().catch(() => ({ detail: `서버 오류 (HTTP ${res.status}) — 백엔드 로그 확인 필요` }))
      return { error: true, summary: `⚠️ 규칙 반영 실패: ${err.detail}` }
    }
    const data = await res.json()           // { ok, form_id, wiring, note?, unchanged? }
    const w = data.wiring ?? {}
    const dev: string[] = w.dev ?? []        // 엔진 미반영 — 계산에 적용 안 됨(관리자/개발 필요)
    const owner: string[] = w.owner ?? []    // 현업이 더 채워야 반영됨
    let summary: string
    if (data.unchanged) {
      // 어휘 밖 요청 등으로 Claude가 블록을 바꾸지 않음 (규칙 변경 없음 → 산문만 저장된 경우)
      summary = `⚙️ 실행 규칙 변경 없음${data.note ? `\n${data.note}` : ''}`
    } else {
      const parts: string[] = ['⚙️ 실행 규칙(블록) 저장됨.']
      if (w.safe_fixed?.length) parts.push(`자동수정 ${w.safe_fixed.length}건`)
      if (owner.length) parts.push(`현업 확인 ${owner.length}건`)
      if (dev.length) parts.push(`개발 필요(T3) ${dev.length}건`)
      summary = parts.join(' · ')
      if (dev.length) {
        // 블록은 저장됐지만 엔진이 이 어휘를 모름 → 실제 계산엔 반영 안 됨. 무음 성공으로 끝내지 않는다.
        summary += `\n\n⛔ 단, 아래는 엔진에 반영되지 않았습니다(계산에 적용되지 않음) — 관리자(개발)에게 연락하세요:\n`
          + dev.map((d: string) => `  • ${d}`).join('\n')
      }
      if (owner.length) {
        summary += `\n\n👤 현업이 확인/보완해야 반영됩니다:\n` + owner.map((o: string) => `  • ${o}`).join('\n')
      }
      if (data.note) summary += `\n${data.note}`
    }
    // 동기화 경로와 공유하는 점검 배너 — gap이 있으면 토스트가 사라져도 끝까지 남긴다
    setWiringNotice(!data.unchanged && (dev.length || owner.length) ? { dev, owner } : null)
    // 블록·자동 섹션이 바뀌었으니 MD 새로고침
    const fresh = await fetch(`${BASE}/api/v3/forms/${selectedId}`, { headers: sessionHeaders() })
    if (fresh.ok) {
      const fd = await fresh.json()
      setContentByForm(prev => ({ ...prev, [selectedId]: fd.content }))
      setHashByForm(prev => ({ ...prev, [selectedId]: fd.content_hash }))
    }
    return { summary, gap: !data.unchanged && dev.length > 0 }
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
      // 동기화 후 와이어링 점검 결과를 끝까지 노출 — "동기화 완료"만 뜨고 실제론 엔진에 안 붙은 무음 갭 방지
      const w = data.wiring ?? {}
      const dev: string[] = w.dev ?? []
      const owner: string[] = w.owner ?? []
      const attention = dev.length > 0 || owner.length > 0
      setWiringNotice(attention ? { dev, owner } : null)
      if (attention) {
        showToast('⚠ 동기화됨 — 일부 미반영(확인 필요)', false, true)
      } else {
        showToast(
          data.changes?.length > 0 ? `동기화 완료 (${data.changes.length}개 변경)` : '동기화 완료 (변경 없음)',
          true,
        )
      }
    } catch {
      showToast('동기화 실패', false)
    } finally {
      setIsSyncing(false)
    }
  }

  async function handleDelete() {
    if (isDeleting || !deletePw) return
    setIsDeleting(true)
    setDeleteErr(null)
    try {
      const res = await fetch(`${BASE}/api/v3/forms/${selectedId}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json', ...sessionHeaders() },
        body: JSON.stringify({ password: deletePw }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: '삭제 실패' }))
        setDeleteErr(err.detail ?? '삭제 실패')
        return
      }
      setContentByForm(prev => { const n = { ...prev }; delete n[selectedId]; return n })
      setHashByForm(prev => { const n = { ...prev }; delete n[selectedId]; return n })
      await reload()
      setIsDeleteOpen(false)
      setDeletePw('')
      const remaining = forms.filter(f => f.id !== selectedId)
      setSelectedId(remaining[0]?.id ?? '')
    } catch {
      setDeleteErr('삭제 중 오류가 발생했습니다.')
    } finally {
      setIsDeleting(false)
    }
  }

  function showToast(msg: string, ok: boolean, warn = false) {
    setSaveToast({ msg, ok, warn })
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
                    {form.syncStatus?.ok === false && (
                      <span
                        title={`자동 동기화 실패 — 수동 동기화 필요: ${form.syncStatus.error ?? ''}`}
                        style={{
                          display: 'inline-flex', alignItems: 'center', gap: 3,
                          fontSize: 10, fontWeight: 700, color: '#c92a2a',
                          background: '#fdeaea', borderRadius: 20, padding: '1px 6px',
                        }}
                      >
                        <AlertCircle size={8} />동기화 실패
                      </span>
                    )}
                    {form.syncStatus?.ok === true && form.syncStatus.formula_changed && (() => {
                      const imp = form.syncStatus.impact
                      let tip = '최근 동기화에서 NET 수식·검증 규칙이 변경되었습니다. 샘플 문서로 검산을 권장합니다.'
                      let label = '수식 변경'
                      if (imp?.available) {
                        const delta = imp.net_delta ?? 0
                        const sign = delta > 0 ? '+' : ''
                        tip = `골든 샘플(${imp.doc_id}) 재계산 영향: ${imp.rows_changed}/${imp.rows_total}행 변동, `
                          + `NET 합계 ${imp.net_before?.toLocaleString()} → ${imp.net_after?.toLocaleString()} (${sign}${delta.toLocaleString()}). `
                          + `의도한 변경인지 확인하세요.`
                        if ((imp.rows_changed ?? 0) > 0) label = `수식 변경 · ${imp.rows_changed}행`
                      } else if (imp && !imp.available) {
                        tip += ` (영향 자동계산 불가: ${imp.reason ?? ''} — 골든 번들 박제 권장)`
                      }
                      return (
                        <span
                          title={tip}
                          style={{
                            display: 'inline-flex', alignItems: 'center', gap: 3,
                            fontSize: 10, fontWeight: 700, color: '#c4622c',
                            background: '#fdf0e8', borderRadius: 20, padding: '1px 6px',
                          }}
                        >
                          {label}
                        </span>
                      )
                    })()}
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
                {/* 삭제 버튼 */}
                <button
                  onClick={() => { setIsDeleteOpen(true); setDeleteErr(null); setDeletePw('') }}
                  title="양식 삭제 (관리자 전용)"
                  style={{
                    display: 'flex', alignItems: 'center', gap: 5,
                    padding: '7px 13px', borderRadius: 8,
                    border: '1px solid #fca5a5',
                    background: '#fff5f5', color: '#dc2626',
                    fontSize: 12, fontWeight: 600, cursor: 'pointer',
                  }}
                >
                  <Trash2 size={12} />
                  삭제
                </button>

                {/* 동기화 버튼 */}
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 2 }}>
                  <button
                    onClick={handleSync}
                    disabled={isSyncing}
                    title="[config] 정본 블록 → form_types.json 빌드. 실행 규칙 변경은 '규칙 반영'으로 (동기화는 빌드만)"
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

              {/* 반영 점검 배너 — dev(엔진 미반영) · owner(현업 확인) gap을 닫기 전까지 노출 */}
              {wiringNotice && (wiringNotice.dev.length > 0 || wiringNotice.owner.length > 0) && (
                <div style={{
                  margin: '12px 32px 0', padding: '12px 16px', borderRadius: 8,
                  border: '1px solid #f5c97a', background: '#fff8ec',
                  display: 'flex', flexDirection: 'column', gap: 8,
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontSize: 13, fontWeight: 700, color: '#b45309' }}>⚠️ 반영 점검</span>
                    <button
                      onClick={() => setWiringNotice(null)}
                      style={{
                        marginLeft: 'auto', border: 'none', background: 'transparent',
                        color: 'var(--text-3)', fontSize: 12, cursor: 'pointer',
                      }}
                    >닫기</button>
                  </div>
                  {wiringNotice.dev.length > 0 && (
                    <div style={{ fontSize: 12, color: '#7c2d12' }}>
                      <div style={{ fontWeight: 600 }}>⛔ 엔진에 반영되지 않음 — 계산에 적용되지 않습니다. 관리자(개발)에게 연락하세요:</div>
                      <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                        {wiringNotice.dev.map((d, i) => <li key={i}>{d}</li>)}
                      </ul>
                    </div>
                  )}
                  {wiringNotice.owner.length > 0 && (
                    <div style={{ fontSize: 12, color: '#92400e' }}>
                      <div style={{ fontWeight: 600 }}>👤 현업이 확인/보완해야 반영됩니다:</div>
                      <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                        {wiringNotice.owner.map((o, i) => <li key={i}>{o}</li>)}
                      </ul>
                    </div>
                  )}
                </div>
              )}

              {/* MD 렌더링 */}
              <div style={{ flex: 1, overflowY: 'auto', padding: '24px 32px', background: 'var(--bg)' }}>
                <FormChangePreview formId={selectedId} />
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
          width: isChatOpen ? panelWidth : 0,
          flexShrink: 0,
          overflow: 'hidden',
          borderLeft: 'none',
          transition: panelResizeRef.current ? 'none' : 'width 0.22s ease',
          background: 'var(--card)',
          display: 'flex',
          flexDirection: 'row',
          position: 'relative',
        }}>
          {/* 드래그 핸들 */}
          {isChatOpen && (
            <div
              onMouseDown={startPanelResize}
              style={{
                width: 5, flexShrink: 0, cursor: 'col-resize',
                background: 'transparent',
                borderLeft: '1px solid var(--border)',
                transition: 'background 0.15s',
              }}
              onMouseEnter={e => (e.currentTarget.style.background = 'var(--primary-light)')}
              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
            />
          )}
          <div style={{ flex: 1, minWidth: 0, height: '100%', display: 'flex', flexDirection: 'column' }}>

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
                        {msg.imageUrls && msg.imageUrls.length > 0 && (
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, justifyContent: 'flex-end' }}>
                            {msg.imageUrls.map((url, ii) => (
                              <img key={ii} src={url} alt="첨부 이미지" style={{
                                width: 72, height: 52, borderRadius: 5,
                                border: '1px solid var(--border)', objectFit: 'cover',
                              }} />
                            ))}
                          </div>
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
                  disabled={isSaving || isApplyingRules || isSending || isSaved}
                  title="문서(산문) 저장 + 실행 규칙(NET 수식·교차검증·출력) 반영을 한 번에. 설명 변경인지 계산 규칙 변경인지 판단할 필요 없이, 백엔드가 알아서 분기합니다."
                  style={{
                    display: 'flex', alignItems: 'center', gap: 5,
                    padding: '6px 14px', borderRadius: 7, border: 'none',
                    background: isSaving || isApplyingRules ? '#ede9e1' : isSaved ? '#d1fae5' : 'var(--primary)',
                    color: isSaving || isApplyingRules ? 'var(--text-3)' : isSaved ? '#065f46' : '#fff',
                    fontSize: 12, fontWeight: 600,
                    cursor: isSaving || isApplyingRules || isSending || isSaved ? 'default' : 'pointer',
                    boxShadow: isSaving || isApplyingRules || isSaved ? 'none' : '0 2px 8px rgba(10,110,110,0.25)',
                    transition: 'background 0.2s, color 0.2s',
                  }}
                >
                  {isSaved ? <CheckCircle size={12} /> : isApplyingRules ? <Zap size={12} /> : <Save size={12} />}
                  {isApplyingRules ? '규칙 반영 중...' : isSaving ? '저장 중...' : isSaved ? '저장 완료' : '저장'}
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
                  <span style={{ fontSize: 11, fontWeight: 600, color: saveToast.warn ? '#b45309' : saveToast.ok ? 'var(--primary)' : '#dc2626' }}>
                    {saveToast.msg}
                  </span>
                )}
              </div>
            )}

            {/* 이미지 미리보기 (대화 탭 전용) */}
            {panelTab === 'chat' && attachedImages.length > 0 && (
              <div style={{
                display: 'flex', flexWrap: 'wrap', gap: 6,
                padding: '8px 12px', background: 'var(--bg)',
                borderTop: '1px solid var(--border)', flexShrink: 0,
              }}>
                {attachedImages.map((img, idx) => (
                  <div key={idx} style={{ position: 'relative' }}>
                    <img
                      src={img.previewUrl}
                      alt="preview"
                      style={{ width: 48, height: 36, objectFit: 'cover', borderRadius: 4, border: '1px solid var(--border)', display: 'block' }}
                    />
                    <button
                      onClick={() => setAttachedImages(prev => prev.filter((_, i) => i !== idx))}
                      style={{
                        position: 'absolute', top: -5, right: -5,
                        width: 16, height: 16, borderRadius: '50%',
                        background: 'var(--text-2)', border: 'none', cursor: 'pointer',
                        display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 0,
                      }}
                    >
                      <X size={9} color="#fff" />
                    </button>
                  </div>
                ))}
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
            {panelTab === 'chat' && <div style={{ padding: '8px 12px 12px', flexShrink: 0, borderTop: attachedImages.length ? 'none' : '1px solid var(--border)' }}>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                multiple
                style={{ display: 'none' }}
                onChange={handleImageSelect}
              />
              <div style={{ display: 'flex', gap: 6, alignItems: 'flex-end' }}>
                <button
                  onClick={() => fileInputRef.current?.click()}
                  title="이미지 첨부"
                  style={{
                    width: 34, height: 34, borderRadius: 8, flexShrink: 0,
                    border: `1.5px solid ${attachedImages.length ? 'var(--primary)' : 'var(--border)'}`,
                    background: attachedImages.length ? 'var(--primary-light)' : 'transparent',
                    color: attachedImages.length ? 'var(--primary)' : 'var(--text-3)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    cursor: 'pointer',
                  }}
                >
                  {attachedImages.length ? <Image size={14} /> : <Paperclip size={13} />}
                </button>
                <textarea
                  ref={chatTextareaRef}
                  value={chatInput}
                  onChange={e => {
                    setChatInput(e.target.value)
                    e.target.style.height = 'auto'
                    const h = Math.min(e.target.scrollHeight, 160)
                    e.target.style.height = h + 'px'
                    e.target.style.overflow = e.target.scrollHeight > 160 ? 'auto' : 'hidden'
                  }}
                  onKeyDown={e => {
                    if (e.key === 'Enter' && e.shiftKey) {
                      e.preventDefault()
                      handleSend()
                    }
                  }}
                  onPaste={e => {
                    const file = Array.from(e.clipboardData.items)
                      .find(item => item.type.startsWith('image/'))
                      ?.getAsFile()
                    if (file) {
                      e.preventDefault()
                      setAttachedImages(prev => [...prev, { file, previewUrl: URL.createObjectURL(file) }])
                    }
                  }}
                  disabled={isSending}
                  placeholder={attachedImages.length ? '이미지 설명 추가 (선택)' : '수정 요청 입력... (줄바꿈: Enter, 전송: Shift+Enter)'}
                  rows={1}
                  style={{
                    flex: 1, border: '1.5px solid var(--border)', borderRadius: 8,
                    padding: '8px 12px', fontSize: 12, outline: 'none',
                    background: isSending ? '#f5f5f5' : 'var(--bg)',
                    color: 'var(--text-1)', fontFamily: 'inherit',
                    resize: 'none', overflow: 'auto', lineHeight: '1.5',
                    minHeight: 36, maxHeight: 160,
                  }}
                />
                <button
                  onClick={handleSend}
                  disabled={(!chatInput.trim() && !attachedImages.length) || isSending}
                  style={{
                    width: 34, height: 34, borderRadius: 8, border: 'none', flexShrink: 0,
                    background: (!chatInput.trim() && !attachedImages.length) || isSending ? '#ede9e1' : 'var(--primary)',
                    color: (!chatInput.trim() && !attachedImages.length) || isSending ? 'var(--text-3)' : '#fff',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    cursor: (!chatInput.trim() && !attachedImages.length) || isSending ? 'not-allowed' : 'pointer',
                    boxShadow: (!chatInput.trim() && !attachedImages.length) || isSending ? 'none' : '0 2px 8px rgba(10,110,110,0.28)',
                  }}
                >
                  <Send size={13} />
                </button>
              </div>
            </div>}

          </div>
        </div>

      </div>

      {/* 삭제 확인 모달 */}
      {isDeleteOpen && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.45)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          zIndex: 1000,
        }}>
          <div style={{
            background: '#fff', borderRadius: 16, padding: '28px 32px',
            width: 380, boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
            display: 'flex', flexDirection: 'column', gap: 18,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <Trash2 size={20} color="#dc2626" />
              <span style={{ fontSize: 16, fontWeight: 700, color: '#111' }}>양식 삭제</span>
            </div>
            <p style={{ fontSize: 13, color: '#444', lineHeight: 1.7, margin: 0 }}>
              <strong style={{ fontFamily: 'var(--mono)' }}>{selectedId}.md</strong>를 삭제합니다.<br />
              MD 파일과 form_types.json 항목이 영구 삭제됩니다.<br />
              <span style={{ color: '#dc2626', fontSize: 12 }}>이 작업은 되돌릴 수 없습니다.</span>
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <label style={{ fontSize: 12, fontWeight: 600, color: '#555' }}>관리자 비밀번호</label>
              <input
                type="password"
                value={deletePw}
                onChange={e => { setDeletePw(e.target.value); setDeleteErr(null) }}
                onKeyDown={e => e.key === 'Enter' && handleDelete()}
                placeholder="비밀번호 입력"
                autoFocus
                style={{
                  border: `1.5px solid ${deleteErr ? '#dc2626' : '#d1d5db'}`,
                  borderRadius: 8, padding: '9px 13px', fontSize: 13,
                  outline: 'none', fontFamily: 'inherit', color: '#111',
                }}
              />
              {deleteErr && (
                <span style={{ fontSize: 11, color: '#dc2626', fontWeight: 600 }}>{deleteErr}</span>
              )}
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                onClick={() => { setIsDeleteOpen(false); setDeletePw(''); setDeleteErr(null) }}
                disabled={isDeleting}
                style={{
                  padding: '8px 20px', borderRadius: 8,
                  border: '1px solid #d1d5db', background: 'transparent',
                  color: '#555', fontSize: 13, fontWeight: 600, cursor: 'pointer',
                }}
              >
                취소
              </button>
              <button
                onClick={handleDelete}
                disabled={!deletePw || isDeleting}
                style={{
                  padding: '8px 20px', borderRadius: 8, border: 'none',
                  background: !deletePw || isDeleting ? '#fca5a5' : '#dc2626',
                  color: '#fff', fontSize: 13, fontWeight: 600,
                  cursor: !deletePw || isDeleting ? 'not-allowed' : 'pointer',
                }}
              >
                {isDeleting ? '삭제 중...' : '삭제'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
