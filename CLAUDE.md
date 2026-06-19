# CLAUDE.md

이 폴더는 **React Rebate v2 워크스페이스**입니다.

## 시스템 구조

**분석 레이어**(파이프라인, 개발자 소유) + **가이드 레이어**(form 정의, 현업 소유) — 전체 개요: [docs/architecture.md](docs/architecture.md)

### 역할 분리 — Claude가 사용자를 안내할 때 반드시 지킬 것

| 역할 | 담당 파일 | 작업 방식 |
|------|----------|----------|
| **개발자** | `.claude/`, `scripts/`, `docs/`, `backend/` | 코드·프롬프트 수정 |
| **현업** | `form_definitions/form_XX.md`, `mappings/*.csv` | 자연어 지시 + 승인 (파일은 Claude가 작성) |

현업 사용자에게 `.claude/`, `scripts/`, `docs/`, `backend/` 편집을 안내하지 않는다.  
현업은 form_definitions/ 파일을 직접 편집하지 않는다. Claude와 대화로 내용을 만들고, 승인하면 Claude가 저장한다.  
새 양식 추가·규칙 변경은 `form_definitions/`와 `mappings/`만으로 완결된다.

## 현재 시스템 (Phase B 전환 완료)

Python 백엔드 + FastAPI + Anthropic SDK 기반 자동 파이프라인이 운영 중입니다.  
- **파이프라인**: `backend/pipeline/` — OCR → Phase 1~4 자동 실행
- **API 서버**: `backend/api/` — FastAPI, 사용자 확인 UI 연동
- **프론트엔드**: `frontend/` — React (Vite + TypeScript)
- **프롬프트**: `docs/phase1-prompt.md` 등 이 워크스페이스의 docs/를 백엔드가 직접 읽음

Claude Code 채팅은 **form 정의 관리**(cold-start, update-form)와 **개발·디버깅** 전용.  
문서 분석 파이프라인은 백엔드가 처리하므로 Claude Code에서 직접 실행하지 않는다.

## 디렉토리

```
.
├── CLAUDE.md                   # 이 파일 — 워크스페이스 컨벤션
├── MANUAL.md                   # 현업 사용 가이드 — 명령어 목록·분석 사이클
├── README.md                   # 짧은 소개
├── .claude/                    # Claude Code 설정 (개발자 소유 — 현업 편집 금지)
│   ├── agents/                 # 매핑 서브에이전트 (form 관리·디버깅용)
│   │   ├── page-md-generator.md    # OCR txt → page MD (ad-hoc 검증용)
│   │   ├── retailer-mapper.md      # OCR 得意先名 → 소매처코드
│   │   └── product-mapper.md       # OCR 商品名 → 제품코드
│   ├── commands/               # 단발 slash 커맨드
│   │   ├── phase4.md           # /phase4 <doc_id> — Phase 4 단독 재실행
│   │   ├── cache-show.md       # /cache-show — 매핑 캐시 현황
│   │   └── cold-start.md       # /cold-start — 신규 양식 정의 작성
│   └── skills/                 # 멀티스텝 워크플로
│       ├── analyze-invoice/    # 청구서 분석 파이프라인 (ad-hoc 검증용)
│       ├── azure-ocr/          # PDF → page_NNN.ocr.txt
│       ├── update-form/        # 이미지 → form 정의 업데이트
│       └── sync-form-config/   # form_XX.md [config] 블록 → config/form_types.json 빌드
├── backend/                    # Python 백엔드 (개발자 소유)
│   ├── pipeline/               # 분석 파이프라인
│   │   ├── orchestrator.py     # Phase 1→2→3→4 오케스트레이터
│   │   ├── ocr.py              # Azure OCR 호출
│   │   ├── phase1.py           # OCR txt → page MD (Claude Haiku, 캐싱)
│   │   ├── phase2.py           # page MD → items[] JSON (Claude Sonnet, 스트리밍)
│   │   ├── phase3.py           # 소매처·제품·판매처 매핑 (Python + Claude)
│   │   └── phase4.py           # NET 계산 (Python subprocess) + 교차검증 (Claude Haiku)
│   ├── api/routes/             # FastAPI 라우터
│   ├── core/                   # 설정·DB 연결·인증
│   └── db/queries.py           # DB 쿼리 모음 (토큰 사용량 포함)
├── docs/                       # 설계 문서 (개발자 소유 — 현업 편집 금지)
│   ├── architecture.md         # 아키텍처 종합 참조 (두 레이어·파이프라인·에이전트 구조·의사결정 기록)
│   ├── cold-start.md           # cold-start UI 설계
│   ├── phase1-prompt.md        # Phase 1 변환 기준 + 검증 기준 (백엔드가 직접 읽음) ✅
│   ├── phase2-prompt.md        # Phase 2 항목 추출 스펙 ✅
│   ├── phase3-mapping.md       # Phase 3 매핑 설계 ✅
│   ├── phase3-prompt.md        # Phase 3 시스템 프롬프트 (백엔드가 직접 읽음) ✅
│   ├── phase3-tool-use-product-prompt.md  # Phase 3 Tool Use 제품 매핑 프롬프트 (백엔드가 직접 읽음) ✅
│   ├── phase4-design.md        # Phase 4 NET계산·SAP포맷 설계 ✅
│   ├── output-format.md        # Phase 4 최종 출력 포맷 정의 ✅
│   └── tool_contracts.md       # Phase 3 Tool Use — lookup_retailer·search_product·confirm_mapping 계약
├── form_definitions/           # 양식별 기준 정보 MD (현업 소유)
│   ├── _index.md               # 양식 등록부 (식별 열 조합 + 파일 링크)
│   ├── form_template.md        # 신규 양식 초안 템플릿
│   ├── form_01.md              # FINET 買掛金特別値引請求明細書 ✅
│   ├── form_04.md              # 日本アクセス 販売促進金請求書 (CVS) ✅
│   ├── form_XX.md              # 03 미작성 (02·05는 작성 완료)
│   └── image/                  # form 정의 근거 이미지 (샘플 캡처, PPT 규칙 등)
│       └── <form_id>/          # 양식별 폴더
├── mappings/                   # 소매처·제품 매핑용 CSV 마스터 (현업 소유)
│   ├── domae_retail_1.csv      # form_01: 도매소매처코드→소매처코드
│   ├── retail_user.csv         # 이름 기반 소매처 검색 + 판매처 기본값 (소매처코드→소매처명+판매처코드+판매처명)
│   ├── unit_price.csv          # 제품코드→시키리·본부장·JANコード
│   ├── ocr_retailer.csv        # 확정 캐시: OCR거래처명→소매처코드 ← 분석 시 자동 누적
│   ├── ocr_product.csv         # 확정 캐시: OCR제품명→제품코드 ← 분석 시 자동 누적
│   ├── ocr_dist.csv            # 확정 캐시: (form_id,issuer_fingerprint,소매처코드)→판매처코드 ← 분석 시 자동 누적
│   └── form_columns.json       # 양식별 DB 저장 컬럼명 정규화 매핑
├── config/                     # Python 실행용 설정 (개발자 소유)
│   ├── form_types.json         # ⚙️ 생성물 — form_XX.md [config] 블록에서 빌드 (phase4가 읽음, 손편집 금지)
│   └── tax_rules.json          # 消費税率 규칙 — タイプ명→세율 (phase4·phase4_calc가 읽음, 코드 하드코딩 금지)
├── scripts/                    # 보조 스크립트 (개발자 소유)
│   ├── phase4_calc.py          # NET 계산 결정적 코드 (phase4.py subprocess로 호출)
│   ├── build_form_types.py     # form_XX.md [config] 정본 블록 → form_types.json 빌드 (--check 가드)
│   └── cleanup_phase1.py       # page MD 코드블록 래핑 후처리 (ad-hoc용)
├── frontend/                   # React 프론트엔드 (개발자 소유)
│   └── src/
│       ├── pages/              # Dashboard, Upload, MappingReview, Results, ColdStart 등
│       ├── components/         # Layout, PdfViewer, ProtectedRoute 등
│       ├── context/            # AuthContext, FormsContext
│       └── api/client.ts       # API 호출 모음
├── extracted/                  # ad-hoc 분석 산출물 (백엔드는 별도 경로 사용)
│   └── <doc_id>/
│       ├── page_NNN.md             # Phase 1 생성 page MD
│       ├── phase2_output.json      # Phase 2 항목 추출 결과
│       ├── phase3_output.json      # Phase 3 매핑 확정 결과
│       └── phase4_output.json      # Phase 4 계산 결과
└── samples/                    # 검증용 PDF
    ├── <doc_id>.pdf
    └── <doc_id>_pages/         # azure-ocr 출력 — page_NNN.ocr.txt
```

## 경로 주의

이 워크스페이스는 `/Users/nongshim/Desktop/Python/lesson/Lecture/`입니다.  
VSCode에 별도 프로젝트 `/Users/nongshim/Desktop/Python/react_rebate_v2/`(구 시스템)가 함께 열려 있지만, **Claude는 이 디렉터리를 절대 참조하거나 편집하지 않는다.**  
프론트엔드를 찾을 때는 `Lecture/frontend/`를 볼 것. `react_rebate_v2/frontend/`가 아님.

## 작업 컨벤션

- 표준 마크다운만 사용. 내부 링크는 `[text](path)` 형식. `[[wikilink]]` 사용 금지.
- 새 설계 산출물은 `docs/`에. 합의된 큰 결정은 [docs/architecture.md](docs/architecture.md)의 "의사결정 기록" 섹션에 추가.
- `samples/`의 PDF는 검증용. 파일명은 문서 식별자(발행처명\_발행월 등)를 그대로 사용.
- **Literate config (룰 단일 진실 소스):** 양식 실행 설정의 정본은 `form_definitions/form_XX.md`의 `## [config]` 블록이다. `config/form_types.json`은 `scripts/build_form_types.py`가 빌드하는 생성물이며 손편집하지 않는다. 규칙을 바꾸면 블록을 고치고 재빌드한다. 설계: [docs/literate-config-migration.md](docs/literate-config-migration.md).

## Claude(LLM)과 Python 역할 분리

| 담당 | 작업 | 이유 |
|------|------|------|
| **Claude** | Phase 1: OCR txt → page MD 구조화 | 비정형 텍스트 이해 |
| **Claude** | Phase 2: page MD → items[] JSON 추출 | 문서 맥락 이해 |
| **Claude** | Phase 3: OCR 명칭 → 소매처·제품코드 매핑 (모호 케이스) | 명칭 불일치 추론 |
| **Claude** | 양식 식별·cold-start | 새 양식 구조 파악 |
| **Python** | Phase 3: 캐시·결정적 CSV 조회 | 속도·재현성 |
| **Python** | Phase 4: NET 계산·교차검증 | 사칙연산은 결정적 코드. 재현성이 생명 |

---

## Claude의 역할 (현재)

Claude Code 채팅에서 Claude가 직접 수행하는 작업:

### 1. 신규 양식 cold-start

"새 양식 등록해줘", "처음 보는 청구서야, 양식 만들어줘" 등 신규 양식 등록 요청 시  
`.claude/commands/cold-start.md` 플로우를 즉시 실행한다.  
발행처명·컬럼·업무규칙은 현업 도메인 지식이 필요하므로 반드시 현업이 대화에 참여해야 한다.

**이것이 이 시스템의 존재 이유다. 부수 기능으로 격하시키지 말 것.**

### 2. form 정의 업데이트

이미지 첨부 + "반영해줘" 지시 시 **update-form 스킬**이 처리한다.  
절차·포맷·확인 규칙은 `.claude/skills/update-form/SKILL.md` 참조.

### 3. 설계 문서 관리

- 설계 문서 작성·갱신·일관성 점검
- 합의된 결정은 [docs/architecture.md](docs/architecture.md) 의사결정 기록 섹션에 추가

### 4. 개발·디버깅 지원

- 백엔드 코드 수정, 프롬프트 튜닝
- ad-hoc 분석: 특정 문서를 직접 점검할 때만 analyze-invoice 스킬 사용

## 워크플로우 구조

이 시스템의 정확한 명칭: **Python 오케스트레이터 기반 역할 분리형 LLM 워크플로우**  
(Python-orchestrated LLM workflow with role-specific worker prompts)

Multi-Agent System이 아니다. 운영 파이프라인은 `backend/pipeline/orchestrator.py`가 Phase 1~4를 순차 실행하며, 필요한 지점에서 Claude API를 직접 호출하는 구조다. `.claude/agents/`의 서브에이전트는 **운영 백엔드와 무관하며** Claude Code 환경에서만 동작하는 보조 워커다.

### 1. 운영 파이프라인 (backend/)

`orchestrator.py` → Phase 1~4 순차 실행. Claude API 호출은 각 phase 내부에서 직접 수행.

| Phase | 실행 주체 | Claude 역할 |
|-------|---------|-----------|
| OCR | Azure API | — |
| Phase 1 | Python + Claude Haiku | OCR txt → page MD 구조화 |
| Phase 2 | Python + Claude Sonnet | page MD → items[] 추출 |
| Phase 3 | Python (캐시) + Claude Haiku (모호 케이스) | OCR 명칭 → 소매처·제품코드 매핑 |
| Phase 4 | Python 결정적 코드 + Claude Haiku (교차검증) | NET 계산은 Python, 검증만 Claude |

### 2. Claude Code 보조 워커 (.claude/agents/)

운영 파이프라인과 **완전히 분리**. Claude Code 채팅에서 ad-hoc 검증·form 관리 시에만 사용.

| 워커 | 역할 | 사용 위치 | spawn 조건 |
|-----|------|----------|-----------|
| `page-md-generator` | OCR txt → page MD | analyze-invoice (ad-hoc 검증) | 5페이지 이상 시 3장씩 병렬 |
| `retailer-mapper` | OCR 거래처명 → 소매처코드 | analyze-invoice (ad-hoc 검증) | product-mapper와 동시 spawn |
| `product-mapper` | OCR 제품명 → 제품코드 | analyze-invoice (ad-hoc 검증) | retailer-mapper와 동시 spawn |

**Agent tool을 쓰지 않는 기준**: 페이지 간 컨텍스트 공유가 필요하거나(Phase 2), 순차 의존성이 있거나, 병렬 이득보다 중복 토큰 비용이 클 때.

### 항상

- 사용자가 잘 모르는 부분은 *자율 판단보다 짧게 묻고 진행*

## 절대 잊지 말 것

- **회계 산수는 LLM이 하지 않는다.** 분류·식별은 Claude, 계산은 결정적 코드. 재현성이 생명.

## Phase 3 Tool Use ✅ 운영 적용 완료

Claude tool_use 프로토콜로 소매처·제품·판매처 매핑을 수행하는 경로입니다.  
Tool 계약(입력·출력·보장사항): [docs/tool_contracts.md](docs/tool_contracts.md)  
기능 전환·검증 완료. Controlled Production Enable 통과. 현재 운영 중.  
기본값 ON — 레거시 경로가 항상 fallback으로 보장됩니다.

### 비활성화 방법

```bash
# backend/.env 에 추가
PHASE3_TOOL_USE_ENABLED=false
```

### 기본값: ON

환경변수가 없으면 Tool Use 경로가 활성화됩니다. 기타 설정:

```bash
PHASE3_TOOL_USE_MODEL=claude-haiku-4-5-20251001      # 기본값, 생략 가능
PHASE3_TOOL_USE_CONCURRENCY=1                        # 문서 내부 동시성 (기본값 1)
PHASE3_TOOL_USE_GLOBAL_CONCURRENCY=3                 # 전체 동시 문서 수 상한 (기본값 3, Tier 업 시 조정)
```

### 동작 방식

```
Tool Use 성공 → adapter output을 phase3 결과로 사용
                + confirm_mapping으로 캐시 CSV 업데이트
                + token usage를 phase3_tool_use phase로 DB 기록
Tool Use 실패 → 자동으로 legacy run_phase3()로 fallback
                + 이미 누적된 token usage는 fallback 시에도 보존
```

fallback 발생 시 로그에 `Phase 3 Tool Use fallback 발생: [클래스] 이유` 메시지가 출력됩니다.

### 구현 완료 기능

| 기능 | 상태 |
|------|------|
| retailer Tool Use 매핑 (lookup → confirm) | ✅ 완료 |
| retailer not_found → pending | ✅ 완료 |
| dist 1:1 자동 확정 | ✅ 완료 |
| dist 1:N Claude 결정 (후보 2개 이상) | ✅ 완료 — 확정 또는 pending |
| product Tool Use (캐시 히트) | ✅ 완료 |
| product Tool Use (후보 있음 → Claude 결정) | ✅ 완료 |
| token usage 기록 (success + fallback, retailer/product/dist 포함) | ✅ 완료 |
| Retry-After 헤더 반영 | ✅ 완료 |
| concurrency 제어 (PHASE3_TOOL_USE_CONCURRENCY) | ✅ 완료 |
| 전역 문서 동시 처리 수 제한 (PHASE3_TOOL_USE_GLOBAL_CONCURRENCY) | ✅ 완료 (Rate Limit 방지) |
| E2E 통합 테스트 (실제 CSV 기반) | ✅ 18개 |
| retailer/product/dist real Claude smoke | ✅ 존재 (`tests/smoke/`, 실행 시 env 필요) |
| FastAPI token DB 기록 확인 | ✅ 운영 확인 완료 (2026-06-05) |

### 운영 참고 사항

| 항목 | 내용 |
|------|------|
| `PHASE3_TOOL_USE_CONCURRENCY` | 1(순차, 문서 내부) — 기본값 유지 |
| `PHASE3_TOOL_USE_GLOBAL_CONCURRENCY` | 3(기본) — 동시 문서 수 상한. API Tier 업 시 조정 |
| dist 1:N 모호 케이스 | 지역 힌트 없는 다중 후보는 pending 유지 (정상 동작) |
| `_attempt_tool_use_phase` generic exception | `ToolUseFallbackTrigger` 서브클래스도 `ToolUseDispatchError`로 래핑 가능 — 동작상 무해 |

### 문제 발생 시 즉시 rollback

```bash
# .env에서 비활성화 또는 제거
PHASE3_TOOL_USE_ENABLED=false
# 백엔드 재시작 → 기존 legacy 경로 즉시 복원
```

---

## 다음 단계

1. ⏭ form 03 `form_definitions/form_03.md` 작성 (업무규칙 수령 후)
2. ⏭ 중간 산출물 정리 로직 — `samples/<doc_id>_pages/`, `extracted/<doc_id>/`는 DB 업로드 후에도 잔류. 운영 규모에서 삭제·아카이브 필요. **사이클 종료 시점 현업 협의 후 확정.**
3. ⏭ normalize_ocr_name 특수문자 처리 — `《集》` 등 포함 OCR명 cache miss 해소 (P1)

### form_03 업무규칙 수령 시 절차

업무규칙 받으면 이 순서대로 하면 됨. 상세는 [docs/phase4-dsl-readiness.md](docs/phase4-dsl-readiness.md) 섹션 11 참고.

```
1. cp config/form_types.json config/form_types.json.bak   ← 백업
2. form_XX.md에 ## [config] 정본 블록 작성 (업무규칙 → 실행 설정 JSON)
   → "form_XX 동기화해줘" (sync-form-config) = build_form_types.py + Schema + 회귀
3. python scripts/phase4_calc.py {샘플_doc_id}  ← 계산 확인
4. 수동 검산 (수식이 업무규칙 기대값과 일치하는지)
5. python scripts/phase4_calc.py {doc_id} --save  ← 픽스처 생성
6. tests/regression/test_phase4_regression.py CASES에 케이스 추가
7. python -m pytest tests/ -q --tb=no  ← 전체 통과 확인
```

이상 시: form_XX.md의 `[config]` 블록을 직전 상태로 되돌리고 `python scripts/build_form_types.py` 재빌드 (또는 `cp config/form_types.json.bak config/form_types.json`)

---

**Last Updated:** 2026-06-18 (Literate config **P3 완료 — 정본-only**. 산문→구조 LLM 추론·auto 블록·blockless 폴백을 표준 경로에서 영구 제거. sync는 `[config]` 블록 빌드만 하고, 블록 없으면 시끄럽게 실패. 신규 양식 첫 블록은 cold-start/create가 골격으로 부착, 규칙은 '규칙 반영'으로. form_03 정본 승격. 잔여: form_02. 설계: docs/literate-config-migration.md §P3)

이전 (2026-06-17): Literate config 전환 — 룰 단일 진실 소스를 form_XX.md `[config]` 정본 블록으로. form_types.json은 `build_form_types.py` 생성물.
