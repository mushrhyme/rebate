export type InvoiceStatus = 'ocr' | 'analyzing' | 'pending' | 'done' | 'error'
export type ErrorType = 'unknown_form' | 'technical'

export interface InvoiceError {
  type: ErrorType
  phase: string
  message: string
}

export interface Invoice {
  id: string
  docId: string
  formLabel: string
  status: InvoiceStatus
  uploadedAt: string
  pendingCount?: number
  error?: InvoiceError
}

export const mockInvoices: Invoice[] = [
  { id: '1', docId: 'sample_003', formLabel: 'FINET', status: 'analyzing', uploadedAt: '05-13' },
  { id: '2', docId: 'sample_004', formLabel: '日本アクセス CVS', status: 'pending', uploadedAt: '05-13', pendingCount: 5 },
  { id: '3', docId: 'sample_007', formLabel: '日本アクセス CVS', status: 'pending', uploadedAt: '05-13', pendingCount: 2 },
  { id: '4', docId: 'sample_005', formLabel: 'FINET', status: 'done', uploadedAt: '05-12' },
  { id: '5', docId: 'sample_001', formLabel: 'FINET', status: 'done', uploadedAt: '05-10' },
  {
    id: '6', docId: 'sample_006', formLabel: '야마에구미', status: 'error', uploadedAt: '05-09',
    error: { type: 'unknown_form', phase: 'Phase 2', message: '양식을 인식할 수 없습니다. form_definitions에 일치하는 양식이 없습니다.' },
  },
]

export interface MappingItem {
  type: 'retailer' | 'product'
  ocrName: string
  candidates: { code: string; name: string; score: number; extra?: string }[]
}

export const mockMappings: MappingItem[] = [
  {
    type: 'retailer',
    ocrName: 'ローソントウカイ (1991474)',
    candidates: [
      { code: '1991474', name: 'ローソン東海', score: 98 },
      { code: '1991475', name: 'ローソン東海北', score: 72 },
      { code: '1991476', name: 'ローソン東海南', score: 61 },
    ],
  },
  {
    type: 'retailer',
    ocrName: 'ローソンシズオカ (1993201)',
    candidates: [
      { code: '1993201', name: 'ローソン静岡', score: 95 },
      { code: '1993202', name: 'ローソン静岡東', score: 68 },
    ],
  },
  {
    type: 'product',
    ocrName: 'チャパゲティ 140g',
    candidates: [
      { code: '101000551', name: 'チャパゲティー1P', score: 92, extra: '140g | 30입 | 시키리 133' },
      { code: '101000552', name: 'チャパゲティ 140g×2P', score: 74, extra: '280g | 15입 | 시키리 250' },
      { code: '101004881', name: 'チャパゲリカップ24入', score: 51, extra: '114g | 12입 | 시키리 185' },
    ],
  },
]

export const mockForms = [
  { id: 'form_01', name: 'FINET', issuer: '国分グループ 등', status: '운영중', tbdCount: 2 },
  { id: 'form_04', name: '日本アクセス CVS', issuer: '日本アクセス', status: '운영중', tbdCount: 0 },
]
