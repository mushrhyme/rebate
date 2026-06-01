# React Rebate — 프로젝트 구조 개요

> **목적**: PDF 조건청구서를 OCR → RAG → LLM 파이프라인으로 분석하고, 사용자가 검토·수정 후 SAP용 Excel로 내보내는 풀스택 시스템.
> 이 문서는 **현재 코드베이스의 구조 스냅샷**으로, 새 폴더에서 전면 개편할 때 참고용 입력으로 사용하기 위한 것이다.
>
> 작성 기준일: 2026-04-27 / main 브랜치

---

## 1. 한 줄 요약

```
PDF 업로드 → PDF→이미지(PyMuPDF) → Azure OCR(prebuilt-layout)
          → RAG 검색(pgvector + BM25 hybrid) → LLM 분석(GPT-4 + 양식별 프롬프트)
          → DB 저장(items + page_data) → WebSocket 진행률 → 사용자 검토·수정 → SAP Excel 내보내기
```

## 2. 기술 스택

| 영역 | 사용 기술 |
|------|----------|
| Backend | FastAPI, Python 3.10+, uv 패키지 매니저, Uvicorn(ASGI) |
| Frontend | React 19 + TypeScript + Vite, react-data-grid, recharts |
| 상태관리 | Zustand(클라이언트), @tanstack/react-query(서버), Context(Auth/Toast) |
| DB | PostgreSQL 12+, **pgvector**(임베딩), **pg_trgm**(유사도 검색) |
| OCR | **Azure Document Intelligence** (prebuilt-layout / prebuilt-read) — 단일 제공자. Tesseract는 회전각 측정에만 사용 |
| LLM | OpenAI GPT-4 (주), Google Gemini · Anthropic Claude (보조) |
| 임베딩 | sentence-transformers (vector(384)) |
| 벡터 DB | pgvector(primary) + FAISS(fallback) |
| 검색 | Hybrid: BM25 + semantic (rank-bm25) |
| 스케줄러 | APScheduler (월 1회 아카이브 마이그레이션) |
| 인증 | bcrypt 비밀번호 해시, 세션 기반 |
| 실시간 통신 | WebSocket (FastAPI) |

## 3. 디렉토리 구조 (요약)

```
react_rebate/
├── backend/                 FastAPI 앱
│   ├── main.py                  진입점, 라우터 등록, lifespan, CORS
│   ├── api/routes/              엔드포인트 라우터 (10개 파일 — §5 참고)
│   └── core/                    설정·인증·세션·스케줄러
├── frontend/                React 19 SPA
│   └── src/
│       ├── App.tsx              탭 라우팅 (사이드바 + 6개 탭)
│       ├── components/          탭별 컴포넌트 (§7)
│       ├── api/                 axios 클라이언트
│       ├── hooks/               useItems, useItemLocks, useWebSocket 등
│       ├── stores/              Zustand (uploadStore)
│       ├── contexts/            AuthContext, ToastContext
│       ├── config/              formConfig (UPLOAD_CHANNELS 등)
│       └── types/               공용 타입
├── database/                DB 스키마·매니저·CSV 마스터
│   ├── init_database.sql        DDL + users 시드
│   ├── SCHEMA.md                테이블 상세 문서
│   ├── db_*.py                  매니저 모듈 (db_items, db_users 등)
│   ├── registry.py              커넥션 풀
│   ├── table_selector.py        current/archive 라우팅
│   ├── archive_migration.py     매월 1일 자동 마이그레이션
│   ├── csv/                     마스터 데이터 (retail_user, dist_retail, unit_price)
│   └── migrations/              증분 마이그레이션 SQL
├── modules/                 PDF 처리 핵심 로직 (백엔드에서 import)
│   ├── core/
│   │   ├── processor.py         PdfProcessor — 파이프라인 오케스트레이터
│   │   ├── rag_manager.py       RAG 검색·학습 (최대 모듈, ~89K)
│   │   ├── build_pgvector_db.py 임베딩 빌더
│   │   ├── build_faiss_db.py    FAISS 인덱스 빌더
│   │   ├── storage.py           파일 저장 유틸
│   │   └── extractors/          OCR/RAG/LLM 추출기 (§6)
│   └── utils/                   양식별 후처리·정규화·LLM 래퍼
├── prompts/                 LLM 프롬프트 (rag_with_example_v1~v11, prompt_v1~v5, zero_shot)
├── config/
│   ├── form_types.json          양식별 필드·계산식·매핑 (데이터 주도)
│   └── rag_provider.json        LLM 제공자 설정 (현재 "gpt5.2")
├── scripts/                 유틸리티 스크립트 (백필, 버전 체크 등)
├── static/                  업로드 PDF·페이지 이미지 서빙
├── logs/                    backend_/frontend_ 타임스탬프 로그 + symlink
├── docs/                    설계·진단 문서
├── debug/, debug2/, img/    샘플 PDF (개발용)
├── dev.sh                   백엔드+프론트엔드 동시 기동
├── requirements.txt / pyproject.toml / uv.lock
└── .env                     API 키, DB 접속, 포트 등
```

## 4. PDF 처리 파이프라인 상세

```
[1] Upload (FormUploadSection)
    └─ POST /api/documents (multipart) → temp/ 임시 저장

[2] PDF → Images
    └─ PyMuPDF로 페이지별 이미지 변환 (modules/core/extractors/pdf_processor.py)

[3] OCR (Azure Document Intelligence)
    └─ modules/core/extractors/azure_extractor.py — prebuilt-layout/prebuilt-read
    └─ 회전각 보정은 Tesseract OSD로 측정만 수행

[4] RAG 검색 (modules/core/rag_manager.py)
    ├─ pgvector(384차원, 코사인유사도, HNSW 인덱스)
    ├─ BM25 (rank-bm25)
    └─ hybrid → form_type별 예제 + 정답 컨텍스트

[5] LLM 분석 (modules/core/extractors/rag_pages_extractor.py)
    ├─ 프롬프트 템플릿: prompts/rag_with_example_v11.txt (현행)
    ├─ openai_chat_completion.py + llm_retry.py
    └─ 양식별 후처리: form2_rebate_utils, form04_mishu_utils, finet01_cs_utils

[6] DB 저장
    ├─ documents_current     문서 메타
    ├─ page_data_current     페이지별 OCR 텍스트 + meta(JSONB) + ocr_words
    ├─ items_current         행 단위(item_data JSONB) + version(낙관적 락)
    └─ page_images_current   이미지 경로(파일시스템)

[7] WebSocket (/ws)
    └─ 메시지: connected | start | progress | page_complete | complete | error

[8] 사용자 편집
    ├─ ItemsGridRdg (react-data-grid) — 본 행 편집, 1차/2차 검토 체크
    ├─ AnswerKeyTab — 정답지 생성 (관리자가 RAG 학습용 정답 편집)
    └─ optimistic locking via item_locks_current (locked_by_user_id, expires_at)

[9] SAP 내보내기
    └─ POST /api/sap-upload — form_types.json의 sap_quantity·extra_columns 규칙으로 Excel 생성
```

## 5. 백엔드 API 라우터 (`backend/api/routes/`)

| 파일 | 마운트 prefix | 주요 책임 |
|------|--------------|----------|
| `auth.py` | `/api/auth` | 로그인, 비밀번호 변경, 세션 |
| `attachments.py` | `/api/documents` (먼저 등록) | 첨부 파일 list/조회 |
| `documents.py` | `/api/documents` | PDF 업로드, 문서 목록/삭제, 페이지 재분석 |
| `items.py` | `/api/items` | 아이템 CRUD, 락 획득/해제 |
| `search.py` | `/api/search` | 고객 검색(RAG/trgm), 페이지 이미지 |
| `websocket.py` | `/ws` | 처리 진행률 push |
| `performance.py` | `/api/performance` | 성능 메트릭 |
| `sap_upload.py` | `/api/sap-upload` | SAP Excel 생성·다운로드 |
| `rag_admin.py` | `/api/rag-admin` | RAG 벡터 학습/관리 (관리자) |
| `settings.py` | `/api/settings` | 사용자 설정 |
| `form_types.py` | `/api/form-types` | 양식 타입 라벨/매핑 |

라우터 등록 순서가 중요: `attachments` → `documents` (`/{pdf_filename}` 와이드카드보다 먼저 매칭).

## 6. 모듈 핵심 파일

### `modules/core/`
| 파일 | 역할 |
|------|------|
| `processor.py` | `PdfProcessor.process_pdf()` — 파이프라인 메인 진입점 |
| `rag_manager.py` | RAG 인덱싱/검색/학습 전체 (가장 큰 모듈) |
| `build_pgvector_db.py` | pgvector 임베딩 빌더 |
| `build_faiss_db.py` | FAISS fallback 인덱스 빌더 |
| `storage.py` | static/ 저장 유틸 |

### `modules/core/extractors/`
| 파일 | 역할 |
|------|------|
| `pdf_processor.py` | PDF → 이미지 변환 |
| `azure_extractor.py` | Azure Document Intelligence 래퍼 (단일 OCR 제공자) |
| `rag_extractor.py` | 단건 RAG 추출 |
| `rag_pages_extractor.py` | **메인 추출기**: 페이지별 OCR→RAG→LLM |

### `modules/utils/` (대표 파일만)
| 파일 | 역할 |
|------|------|
| `openai_chat_completion.py` | OpenAI Chat API 래퍼 |
| `llm_retry.py` | LLM 호출 재시도/백오프 |
| `form2_rebate_utils.py`, `form04_mishu_utils.py`, `finet01_cs_utils.py` | 양식별 후처리 |
| `retail_resolve.py`, `retail_user_utils.py` | 고객 마스터 매칭 (CSV 기반) |
| `text_normalizer.py` | 일본어 텍스트 정규화 |
| `fill_empty_values_utils.py` | 빈값 자동 채움 |
| `image_rotation_utils.py` | Tesseract OSD로 회전각 보정 |
| `pdf_utils.py`, `table_ocr_utils.py`, `ocr_words_utils.py` | OCR 보조 |
| `master_display_enrich.py` | 마스터 정보로 표시 enrich |
| `net_calc_utils.py` | 純額 계산 (form_types.json 규칙) |
| `session_manager.py`, `db_manifest_manager.py`, `hash_utils.py` | 인프라 유틸 |

## 7. 프론트엔드 컴포넌트 (`frontend/src/components/`)

App.tsx에서 사이드바 + 6개 탭 구성:

| 탭 (key) | 라벨 | 컴포넌트 |
|---------|------|----------|
| `dashboard` | 現況 | `Dashboard/Dashboard.tsx` |
| `upload` | アップロード | `Upload/FormUploadSection`, `UploadedFilesList`, `UploadProgressList`, `UploadPagePreview` |
| `search` | 請求 (검토) | `Search/CustomerSearch.tsx` + `Grid/ItemsGridRdg.tsx` |
| `ocr_test` | 解答作成 | `AnswerKey/AnswerKeyTab.tsx` (LeftPanel + GridSection + PageMetaSection) |
| `sap_upload` | SAPアップロード | `SAPUpload/SAPUpload.tsx` |
| `rag_admin` | 管理者画面 | `Admin/RagAdminPanel.tsx` (관리자 전용) |

추가:
- `Auth/Login.tsx`, `ChangePasswordModal.tsx` — 로그인 게이트
- `Grid/` — react-data-grid 기반 메인 편집 그리드 (`useItemsGridColumns`, `ReviewCheckboxCell`, `ActionCellWithMenu`, `AttachmentModal`, `UnitPriceMatchModal`, `ComplexFieldDetail`, `netCalc.ts`, `utils.ts`)
- `Ocr/OcrOverlay.tsx` — OCR 결과 오버레이
- `ErrorBoundary.tsx`

### 상태관리 분담

| 방식 | 용도 | 위치 |
|------|------|------|
| Zustand | 업로드 진행/파일 목록 | `stores/uploadStore.ts` |
| React Query | 서버 데이터 캐시 (`['documents','all']` 등, refetchInterval 30s) | 컴포넌트/훅 내 |
| Context | 인증, 토스트 | `contexts/AuthContext.tsx`, `ToastContext.tsx` |

### 커스텀 훅

- `useItems.ts` — 아이템 CRUD mutations
- `useItemLocks.ts` — optimistic locking (획득/해제)
- `useWebSocket.ts` — WebSocket 연결 + 메시지 라우팅
- `useFormTypes.ts`, `useFormTypesConfig.ts` — 양식 타입/매핑

### Vite 프록시 (`vite.config.ts`)

- `/api`     → `http://127.0.0.1:8000`
- `/ws`      → `ws://127.0.0.1:8000`
- `/static`  → `http://127.0.0.1:8000`

## 8. 데이터베이스 스키마 (요약)

> 상세는 `database/SCHEMA.md` 참고. **모든 핵심 테이블은 `_current` / `_archive` 쌍**.

| 테이블 (current/archive) | 용도 | 핵심 컬럼 |
|--------------------------|------|----------|
| `documents_*` | 문서 메타 | `pdf_filename`(UQ), `form_type`, `upload_channel`, `total_pages`, `data_year/month`, `is_answer_key_document` |
| `page_data_*` | 페이지별 데이터 | `(pdf_filename, page_number)` UQ, `page_role`, `page_meta`(JSONB), `is_rag_candidate`, `ocr_text`, `ocr_words` |
| `items_*` | 행 단위 데이터 | `item_id`, `item_data`(JSONB), `customer`, `version`(낙관적 락), `first/second_review_checked` |
| `item_locks_*` | 편집 락 | `item_id`(PK/FK), `locked_by_user_id`, `expires_at` |
| `page_images_*` | 페이지 이미지 경로 | `image_path`, `image_format`, `image_size` |

비-아카이브 테이블:

| 테이블 | 용도 |
|--------|------|
| `rag_page_embeddings` | 페이지 단위 pgvector 임베딩 (vector(384) + answer_json + form_type) — RAG 검색·학습의 단일 소스 |
| `rag_vector_index` | FAISS 글로벌 인덱스 (BYTEA) — fallback |
| `form_field_mappings` | 논리키 ↔ 양식별 물리 필드명 매핑 (DB 우선, config fallback) |
| `form_type_labels` | form_code → 표시명 |
| `users` | 사용자 (`is_admin` 플래그) |
| `user_sessions` | 세션 |

### 외래키
- `page_data_*.pdf_filename` → `documents_*.pdf_filename`
- `items_*(pdf_filename, page_number)` → `page_data_*`
- `item_locks_*.item_id` → `items_*.item_id`

### DB 함수
- `cleanup_expired_locks()`, `cleanup_expired_sessions()` — 만료 정리

## 9. 핵심 설계 개념

### form_type (양식코드)
- 현재 `01`~`05`: FINET / 야마에 / 아사히 / 악세스 / 와쿠가와
- **고정 아님**. 신규 양식이 언제든 추가될 수 있고, 기존에 학습되지 않은 양식도 수용 가능해야 함.
- **데이터 주도**: 필드/계산식/SAP 매핑은 모두 `config/form_types.json`에 외부화 — 하드코딩 금지.
- 신규 양식 cold start 흐름: unresolved 큐 → 사용자 수정 → alias/규칙 자동 승격 → 다음부터 자동화.
- 각 양식별 설정 키: `fields`, `net_calculation`, `decimal_conversion`, `row_merging`, `sap_quantity`, `sap_extra_columns`, `use_customer_lookup`, `inference_keys`, `inference_priority`.

### upload_channel
- `finet`: Excel 텍스트 파싱 (빠른 경로)
- `mail`: PDF + Azure OCR (메일 첨부)

### current / archive 패턴
- 모든 핵심 테이블이 `_current` / `_archive`로 분리.
- 매월 1일 0시 자동 마이그레이션 (APScheduler, `database/archive_migration.py`).
- 조회 시 `table_selector.py`가 `(data_year, data_month)`로 라우팅.

### Optimistic Locking
- `items.version` 필드로 동시 편집 충돌 감지.
- `item_locks_*` 테이블로 명시적 편집 락도 별도 운영 (사용자별 `expires_at`).

### 문서 언어
- 원문은 **일본어**. anaphora(`上記`, `同条件`, `前記`, `当該` 등), 숫자/날짜 포맷 모두 일본어 기준.

### 인증·권한
- `username == 'admin'` 또는 `is_admin == TRUE` → 관리자.
- 관리자만 `RAG Admin` 탭 접근.
- 비관리자가 `rag_admin` 탭 진입 시도하면 자동으로 `upload` 탭으로 리다이렉트.

## 10. 실행 / 환경

### 개발 서버
```bash
./dev.sh                       # 백엔드(8000) + 프론트(3002) 동시 기동, 로그는 logs/에 저장
uv run rebate-server           # 백엔드 단독 (DEBUG=true 면 reload)
cd frontend && npm run dev     # 프론트 단독
```

### 환경변수 (`.env`)
```
GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY
AZURE_API_KEY, AZURE_API_ENDPOINT
DB_NAME=rebate_db, DB_HOST, DB_PORT, DB_USER, DB_PASSWORD
API_HOST=0.0.0.0, API_PORT=8000, DEBUG=true
LOCAL_IP                       # CORS regex 허용용 (선택)
ALLOW_LOCAL_NETWORK=true       # 로컬 네트워크 IP 허용
UV_RELOAD=1                    # uvicorn auto-reload
```

### DB 초기화
```bash
psql -U postgres -d rebate_db -f database/init_database.sql
# users 시드: database/csv/users_import.csv 가 있으면 자동 \copy
```

### 로그
- `logs/backend_<TS>.log`, `logs/frontend_<TS>.log` (타임스탬프별)
- `logs/backend_latest.log`, `logs/frontend_latest.log` symlink → 최신
- 실시간: `tail -f logs/backend_latest.log`

## 11. 주요 외부 의존성

### Python (requirements.txt)
- `fastapi`, `uvicorn[standard]`, `python-multipart`, `pydantic`, `pydantic-settings`
- `psycopg2-binary` (pgvector는 `init_database.sql` extension)
- `Pillow`, `pytesseract`, `PyMuPDF`
- `openai`, `google-generativeai`, `anthropic`
- `sentence-transformers`, `faiss-cpu`, `rank-bm25`
- `bcrypt`, `pandas`, `numpy`, `openpyxl`, `APScheduler`, `python-dotenv`

### Node (frontend/package.json)
- `react@19`, `react-dom@19`, `react-is@19`
- `@tanstack/react-query@5`, `axios`, `zustand`
- `react-data-grid@7-beta`, `recharts`, `uuid`
- 빌드: `vite@7`, `typescript@5`, `eslint@10`

## 12. 작업·디버깅 원칙 (CLAUDE.md에서 반복 강조)

- **같은 방향 수정이 2회 이상 실패하면 즉시 멈춤.** 코드 더 쓰지 말고 사용자에게 "내 가설은 X다" 한 줄로 먼저 말한 뒤, 반대 방향 접근(예: "막기" → "허용하되 보정")도 함께 제시하고 방향을 재합의한 뒤에 수정.
- **같은 방향의 판정 기준**: 타이머 값·재시도 횟수·플래그 개수·스킵 윈도우 길이만 바꾸는 건 전부 같은 방향. 접근 자체(이벤트 차단 vs. 결과 보정, 상위에서 막기 vs. 하위에서 받기 등)가 달라져야 "다른 방향".
- **코드 작성 전에 가설을 먼저 말할 것.** 라이브러리 내부 동작이 얽힌 버그는 추측 패치 금지, 결과를 한 줄로 설명 가능할 때만 수정.

---

## 부록 A. 새로 다시 만들 때 우선순위 제안 (참고)

1. **DB 스키마 + 마이그레이션** (current/archive 패턴, pgvector, 외래키)
2. **인증/세션** (관리자 게이트가 거의 모든 화면에 영향)
3. **업로드 → OCR → DB 저장** (Azure 단일 OCR 경로, WebSocket 진행률)
4. **편집 그리드 + optimistic locking** (편집 충돌 시나리오 견고성)
5. **RAG 학습 루프** (정답지 탭 → rag_page_embeddings → 다음 업로드 자동화)
6. **양식별 규칙 외부화** (form_types.json 데이터 주도 — 신규 양식 cold start 수용 필수)
7. **SAP 내보내기** (form_types.json의 sap_* 규칙)
8. **관리자 화면** (form_field_mappings·form_type_labels·is_rag_candidate 토글)

## 부록 B. 본 문서가 다루지 않는 것

- 개별 함수의 파라미터/반환 타입 시그니처 → 코드 직접 참조
- 프롬프트 본문 (현행 `prompts/rag_with_example_v11.txt`)
- 마이그레이션 SQL 본문 (`database/migrations/`)
- 실제 운영 데이터·CSV 마스터 내용
