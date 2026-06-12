const BASE = import.meta.env.VITE_API_URL ?? ''

function sessionId(): string | null {
  return localStorage.getItem('session_id')
}

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = 30_000): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)

  const sid = sessionId()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init.headers as Record<string, string> ?? {}),
  }
  if (sid) headers['X-Session-ID'] = sid

  if (init.body instanceof FormData) delete headers['Content-Type']

  try {
    const res = await fetch(`${BASE}${path}`, { ...init, headers, signal: controller.signal })

    if (res.status === 401) {
      localStorage.removeItem('session_id')
      const body = await res.json().catch(() => ({} as { detail?: string }))
      if (path !== '/api/auth/login') {
        window.location.href = '/login'
      }
      throw new Error(body.detail ?? '로그인이 필요합니다.')
    }

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(err.detail ?? '서버 오류')
    }

    return res.json()
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') {
      throw new Error('요청 시간 초과 (30초). 잠시 후 다시 시도하세요.')
    }
    throw e
  } finally {
    clearTimeout(timer)
  }
}

export const api = {
  // auth
  login: (username: string, password: string) =>
    request<{ session_id: string; user: User }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  me: () => request<User>('/api/auth/me'),
  logout: () => request('/api/auth/logout', { method: 'POST' }),
  changePassword: (current_password: string, new_password: string) =>
    request<{ ok: boolean }>('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password, new_password }),
    }),

  // admin — 사용자 관리
  getUsers: () => request<AdminUser[]>('/api/auth/users'),
  createUser: (data: CreateUserPayload) =>
    request<{ user_id: number; ok: boolean }>('/api/auth/users', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateUser: (userId: number, data: UpdateUserPayload) =>
    request<{ ok: boolean }>(`/api/auth/users/${userId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteUser: (userId: number) =>
    request<{ ok: boolean }>(`/api/auth/users/${userId}`, { method: 'DELETE' }),

  // documents
  uploadDocument: (file: File, hatsuMonth: string) => {
    const fd = new FormData()
    fd.append('file', file)
    if (hatsuMonth) fd.append('hatsu_month', hatsuMonth)
    return request<{ doc_id: string; status: string }>('/api/v3/documents', {
      method: 'POST',
      body: fd,
    })
  },
  listDocuments: () => request<Document[]>('/api/v3/documents'),
  getDocument:   (docId: string) => request<Document>(`/api/v3/documents/${docId}`),
  getResults:    (docId: string) => request<Phase4Result>(`/api/v3/documents/${docId}/results`),
  retryDocument: (docId: string, force = false) =>
    request<{ doc_id: string; status: string }>(
      `/api/v3/documents/${docId}/retry${force ? '?force=true' : ''}`,
      { method: 'POST' },
    ),
  cancelDocument: (docId: string) =>
    request<{ doc_id: string; status: string }>(`/api/v3/documents/${docId}/cancel`, { method: 'POST' }),
  remapCached: (docId: string) =>
    request<{ doc_id: string; status: string }>(`/api/v3/documents/${docId}/remap-cached`, { method: 'POST' }),

  // SSE — 파이프라인 상태 스트리밍 (EventSource 반환, 호출자가 close 책임)
  streamStatus: (docId: string): EventSource => {
    const sid = sessionId()
    const url = `${BASE}/api/v3/documents/${docId}/stream` + (sid ? `?sid=${sid}` : '')
    return new EventSource(url)
  },

  // mappings
  getMappings: (docId: string) =>
    request<Mapping[]>(`/api/v3/documents/${docId}/mappings`),
  confirmMapping: (docId: string, mappingId: number, code: string, name: string) =>
    request(`/api/v3/documents/${docId}/mappings/confirm`, {
      method: 'POST',
      body: JSON.stringify({ mapping_id: mappingId, confirmed_code: code, confirmed_name: name }),
    }),
  confirmAllMappings: (docId: string) =>
    request(`/api/v3/documents/${docId}/mappings/confirm-all`, { method: 'POST' }),
  remapRetailer: (docId: string, ocrName: string, retailerCode: string, retailerName: string) =>
    request<{ ok: boolean; status: string }>(`/api/v3/documents/${docId}/mappings/remap-retailer`, {
      method: 'POST',
      body: JSON.stringify({ ocr_name: ocrName, retailer_code: retailerCode, retailer_name: retailerName }),
    }),
  remapDist: (docId: string, ocrName: string, distCode: string, distName: string) =>
    request<{ ok: boolean; status: string }>(`/api/v3/documents/${docId}/mappings/remap-dist`, {
      method: 'POST',
      body: JSON.stringify({ ocr_name: ocrName, dist_code: distCode, dist_name: distName }),
    }),
  remapProduct: (docId: string, ocrName: string, productCode: string, productName: string) =>
    request<{ ok: boolean; status: string }>(`/api/v3/documents/${docId}/mappings/remap-product`, {
      method: 'POST',
      body: JSON.stringify({ ocr_name: ocrName, product_code: productCode, product_name: productName }),
    }),

  // reviews
  getReviews: (docId: string) =>
    request<ReviewRecord[]>(`/api/v3/documents/${docId}/reviews`),
  markReviewed: (docId: string, retailerCode: string, reviewType: string) =>
    request<ReviewRecord & { doc_confirmed: boolean }>(`/api/v3/documents/${docId}/review`, {
      method: 'PATCH',
      body: JSON.stringify({ retailer_code: retailerCode, review_type: reviewType }),
    }),
  unmarkReviewed: (docId: string, retailerCode: string, reviewType: string) =>
    request<{ ok: boolean }>(`/api/v3/documents/${docId}/review`, {
      method: 'DELETE',
      body: JSON.stringify({ retailer_code: retailerCode, review_type: reviewType }),
    }),
  recheckConfirm: (docId: string) =>
    request<{ doc_confirmed: boolean }>(`/api/v3/documents/${docId}/recheck-confirm`, { method: 'POST' }),
  unconfirmDocument: (docId: string) =>
    request<{ ok: boolean }>(`/api/v3/documents/${docId}/unconfirm`, { method: 'POST' }),
  deleteDocument: (docId: string, password: string) =>
    request<{ ok: boolean }>(`/api/v3/documents/${docId}`, {
      method: 'DELETE',
      body: JSON.stringify({ password }),
    }),
  getMyRetailers: () => request<MyRetailer[]>('/api/v3/retailers/my'),

  // search
  searchProduct:  (q: string) =>
    request<ProductResult[]>(`/api/v3/search/product?q=${encodeURIComponent(q)}`),
  searchRetailer: (q: string) =>
    request<RetailerResult[]>(`/api/v3/search/retailer?q=${encodeURIComponent(q)}`),
  searchDist: (q: string) =>
    request<DistResult[]>(`/api/v3/search/dist?q=${encodeURIComponent(q)}`),
  getDistCandidates: (retailerCode: string) =>
    request<DistResult[]>(`/api/v3/search/retailer-dists?retailer_code=${encodeURIComponent(retailerCode)}`),

  // sap export
  listConfirmedDocs: (year?: number, month?: number) => {
    const params = new URLSearchParams()
    if (year  != null) params.set('year',  String(year))
    if (month != null) params.set('month', String(month))
    const qs = params.toString()
    return request<ConfirmedDoc[]>(`/api/v3/sap/confirmed-docs${qs ? `?${qs}` : ''}`)
  },
  previewSap: (docIds: string[]) =>
    request<SapPreview>('/api/v3/sap/preview', {
      method: 'POST',
      body: JSON.stringify({ doc_ids: docIds }),
    }),
  downloadSap: async (docIds: string[]): Promise<void> => {
    const sid = localStorage.getItem('session_id')
    const BASE = import.meta.env.VITE_API_URL ?? ''
    const res = await fetch(`${BASE}/api/v3/sap/download`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(sid ? { 'X-Session-ID': sid } : {}),
      },
      body: JSON.stringify({ doc_ids: docIds }),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(err.detail ?? '다운로드 실패')
    }
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    const cd = res.headers.get('Content-Disposition') ?? ''
    const m = cd.match(/filename="([^"]+)"/)
    a.href = url
    a.download = m ? m[1] : 'SAP_export.xlsx'
    a.click()
    URL.revokeObjectURL(url)
  },

  // admin — 사용량 모니터링
  getUsage: (params: { startDate?: string; endDate?: string } = {}) => {
    const qs = new URLSearchParams()
    if (params.startDate) qs.set('start_date', params.startDate)
    if (params.endDate)   qs.set('end_date',   params.endDate)
    const q = qs.toString()
    return request<UsageResponse>(`/api/admin/usage${q ? '?' + q : ''}`)
  },

  // admin — 소매처 담당자 관리
  getRetailAssignments: () => request<RetailAssignmentResponse>('/api/admin/retail-assignment'),
  patchRetailAssignment: (body: PatchAssignmentPayload) =>
    request<{ ok: boolean; updated: number }>('/api/admin/retail-assignment', {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  // forms
  listForms: () => request<FormEntry[]>('/api/v3/forms'),
  getForm:   (formId: string) => request<{ form_id: string; content: string }>(`/api/v3/forms/${formId}`),
  createForm: (data: { issuer: string; net_formula: string; cf_keywords: string }) =>
    request<{ form_id: string; content: string }>('/api/v3/forms', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
}

// ── 타입 ──────────────────────────────────────────────────────

export interface User {
  user_id: number
  username: string
  display_name: string
  display_name_ja?: string
  is_admin: boolean
  force_password_change: boolean
  role?: string
  department_ko?: string
}

export interface ReviewRecord {
  id: number
  doc_id: string
  retailer_code: string
  review_type: '1차' | '2차'
  reviewer_id: number
  reviewed_at: string
  reviewer_name?: string
  reviewer_name_ja?: string
  reviewer_username?: string
}

export interface MyRetailer {
  retailer_code: string
  retailer_name: string
  dist_code: string
  dist_name: string
}

export interface AdminUser {
  user_id: number
  username: string
  display_name: string
  display_name_ja?: string
  department_ko?: string
  department_ja?: string
  role?: string
  category?: string
  is_admin: boolean
  is_active: boolean
  force_password_change: boolean
  login_count: number
  last_login_at?: string
  created_at: string
}

export interface CreateUserPayload {
  username: string
  display_name: string
  display_name_ja?: string
  department_ko?: string
  department_ja?: string
  role?: string
  category?: string
  is_admin?: boolean
}

export interface UpdateUserPayload {
  display_name?: string
  display_name_ja?: string
  department_ko?: string
  department_ja?: string
  role?: string
  category?: string
  is_active?: boolean
  is_admin?: boolean
  reset_password?: boolean
}

export interface TokenUsagePhase {
  input: number
  output: number
  model: string
  cache_read?: number
  cache_creation?: number
}

export interface UsageRun {
  run_id: string
  doc_id: string
  run_at: string
  pdf_filename: string
  status: string
  confirmed_at: string | null
  uploader_username: string | null
  uploader_name_ja: string | null
  uploader_name: string | null
  phases: Record<string, TokenUsagePhase>   // phase1 / phase2 / phase3 / phase4_xv
}

export interface UsageResponse {
  runs: UsageRun[]
  period: string
  start: string
  end: string
}

export interface Document {
  doc_id: string
  pdf_filename: string
  form_id: string | null
  status: 'uploaded' | 'queued' | 'ocr' | 'analyzing' | 'phase1' | 'phase2' | 'phase3' | 'phase4' | 'pending' | 'done' | 'error' | 'xv_warning'
  error_type: string | null
  error_phase: string | null
  error_message: string | null
  created_at: string
  updated_at?: string
  analysis_started_at?: string | null
  hatsu_month?: string | null
  pending_count: number
  pages_count: number
  confirmed_at: string | null
  token_usage: Record<string, TokenUsagePhase> | null
  phase_timings?: Record<string, number>
  uploaded_by_username?: string
  uploaded_by_name?: string
  uploaded_by_name_ja?: string
  // 분석 시점 이후 form 정의·form_types가 변경됨 — 재분석 권장 (null: 판단 불가)
  stale_rules?: boolean | null
}

export interface Mapping {
  id: number
  doc_id: string
  mapping_type: 'retailer' | 'product' | 'dist'
  ocr_name: string
  candidates: MappingCandidate[]
  page_number: number | null
  confirmed_code: string | null
  confirmed_name: string | null
}

export interface MappingCandidate {
  code: string
  name: string
  score?: number
  // 제품 전용
  volume?: string
  case_qty?: string   // 규격 (예: "12×2")
  shikiri?: number
  honbucho?: number
}

export interface ProductResult {
  code: string
  name: string
  volume: string
  spec: string
  sikiri: number | null
  honbucho: number | null
  jan: string
}

export interface RetailerResult {
  code: string
  name: string
}

export interface DistResult {
  code: string
  name: string
}

export interface RateSummary {
  by_rate: Record<string, number>
  total_ex: number
}

export interface BundleInfo {
  bundle_idx: number
  page_range: [number, number]
  cover_page: number
}

export interface BundleXv {
  bundle_idx: number
  jisho: string
  cover_page: number
  xv: CrossValidation[]
}

export interface Phase4Result {
  doc_id: string
  form_id: string
  xv: CrossValidation[]
  xv_error?: boolean
  rows: Phase4Row[]
  summary?: RateSummary
  bundles?: BundleInfo[]
  bundle_xv?: BundleXv[]
  show_sections?: string[]
  aggregate_label?: string
}

export interface XvAmounts {
  ex_tax: number
  tax: number
  inc_tax: number
}

export interface XvRow {
  label: string
  ex_tax: number
  tax: number
  inc_tax: number
}

export interface XvCustomer {
  name: string
  rows: XvRow[]
  total: XvAmounts
  summary_ex_tax: number | null
  ok: boolean
}

export interface XvGrandTotal {
  rows: XvRow[]
  total: XvAmounts
}

export interface CrossValidation {
  label: string
  expected: number | null
  actual: number | null
  ok: boolean
  diff?: number | null
  xv_type?: 'simple' | 'customer_breakdown'
  status?: 'OK' | 'MISMATCH' | 'NEEDS_CONFIRMATION'
  customers?: XvCustomer[]
  grand_total?: XvGrandTotal
}

export interface Phase4Row {
  受注先: string
  受注先コード: string
  担当者?: string
  担当者ID?: string
  代表スーパー: string
  スーパー: string
  商品名: string
  商品コード: string
  タイプ: string
  仕切: number | null
  本部長価格: number | null
  NET: number | null
  net_lt_honbu: boolean
  unconfirmed: boolean
  // 수량
  ケース入数?: number | null
  ボール入数?: number | null
  ケース: number | null
  バラ: number | null
  個数計: number | null
  ケース計?: number | null
  発生月: string
  // 조건
  '条件1（パック）': number | null
  '条件2（パック）': number | null
  '条件1（ボール）'?: number | null
  '条件2（ボール）'?: number | null
  '条件1（ケース）': number | null
  '条件2（ケース）': number | null
  Q?: number | null
  S?: number | null
  AF?: number | null
  AG?: number | null
  // 금액
  '未収金額合計'?: number | null
  // 오리지널 OCR 명칭 (저장 시 _ prefix 제거됨)
  customer_ocr: string
  product_ocr: string
  invoice_no: string
  page_number?: number | null
  jisho?: string
  condition_type?: string
}

export interface ConfirmedDoc {
  doc_id: string
  pdf_filename: string
  confirmed_at: string
  created_at: string
}

export interface SapPreview {
  columns: string[]
  rows: Record<string, unknown>[]
}

export interface RetailRetailer {
  retailer_code: string
  retailer_name: string
  dist_code: string
  dist_name: string
}

export interface RetailRep {
  rep_id: string
  rep_name: string
  system_id: string
  retailers: RetailRetailer[]
}

export interface RetailAssignmentResponse {
  reps: RetailRep[]
  total_retailers: number
}

export interface PatchAssignmentPayload {
  retailer_codes: string[]
  new_rep_id: string
  new_rep_name: string
  new_system_id: string
}

export interface FormSyncStatus {
  ok: boolean
  changes?: string[]
  formula_changed?: boolean
  synced_at: string
  error?: string | null
}

export interface FormEntry {
  form_id: string
  name: string
  short_name: string
  tbd_count: number
  sync_status?: FormSyncStatus | null
}
