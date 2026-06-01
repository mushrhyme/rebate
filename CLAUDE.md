# CLAUDE.md

이 폴더는 **React Rebate v2 워크스페이스**입니다.

## 시스템 구조

**분석 레이어**(파이프라인, 개발자 소유) + **가이드 레이어**(form 정의, 현업 소유) — 전체 개요: [docs/framework.md](docs/framework.md)

### 역할 분리 — Claude가 사용자를 안내할 때 반드시 지킬 것

| 역할 | 담당 파일 | 작업 방식 |
|------|----------|----------|
| **개발자** | `.claude/`, `scripts/`, `docs/`, `backend/` | 코드·프롬프트 수정 |
| **현업** | `form_definitions/form_XX.md`, `mappings/*.csv` | 자연어 지시 + 승인 (파일은 Claude가 작성) |

현업 사용자에게 `.claude/`, `scripts/`, `docs/`, `backend/` 편집을 안내하지 않는다.  
현업은 form_definitions/ 파일을 직접 편집하지 않는다. Claude와 대화로 내용을 만들고, 승인하면 Claude가 저장한다.  
새 양식 추가·규칙 변경은 `form_definitions/`와 `mappings/`만으로 완결된다.

## 배경

기존 시스템([docs/current-system.md](docs/current-system.md))은 PDF 청구서를 OCR → RAG → LLM 파이프라인으로 처리합니다. 한계 두 가지:

1. **신규 양식 cold-start 불가** — RAG가 유사 페이지를 찾지 못하면 엉뚱한 예시를 가져오고, 결국 개발자가 수동으로 정답지를 만들어야 시스템이 학습을 시작합니다.
2. **다중 페이지 합성 불가** — 페이지 단위 분석이라 표지·상세·총합이 분리된 문서를 종합 해석하지 못합니다.

새 설계는 Claude의 long-context로 PDF 통째 분석 + 신규 양식 자동 학습을 *메인 파이프라인의 일급 분기*로 둡니다.

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
│       └── sync-form-config/   # form_XX.md → config/form_types.json 동기화
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
│   ├── current-system.md       # 기존 시스템 스냅샷 (참조용, 변경 금지)
│   ├── architecture.md         # 새 아키텍처 설계
│   ├── framework.md            # 두 레이어 구조 개요
│   ├── cold-start.md           # cold-start UI 설계
│   ├── phase1-prompt.md        # Phase 1 변환 기준 + 검증 기준 (백엔드가 직접 읽음) ✅
│   ├── phase2-prompt.md        # Phase 2 항목 추출 스펙 ✅
│   ├── phase3-mapping.md       # Phase 3 매핑 설계 ✅
│   ├── phase3-redesign.md      # Phase 3 재설계 검토 메모 (중간 산출물 — 참조용)
│   ├── phase4-design.md        # Phase 4 NET계산·SAP포맷 설계 ✅
│   └── output-format.md        # Phase 4 최종 출력 포맷 정의 ✅
├── form_definitions/           # 양식별 기준 정보 MD (현업 소유)
│   ├── _index.md               # 양식 등록부 (식별 열 조합 + 파일 링크)
│   ├── form_template.md        # 신규 양식 초안 템플릿
│   ├── form_01.md              # FINET 買掛金特別値引請求明細書 ✅
│   ├── form_04.md              # 日本アクセス 販売促進金請求書 (CVS) ✅
│   ├── form_XX.md              # 02·03·05 미작성
│   └── image/                  # form 정의 근거 이미지 (샘플 캡처, PPT 규칙 등)
│       └── <form_id>/          # 양식별 폴더
├── mappings/                   # 소매처·제품 매핑용 CSV 마스터 (현업 소유)
│   ├── domae_retail_1.csv      # form_01: 도매소매처코드→소매처코드
│   ├── domae_retail_2.csv      # form_02~05: 도매소매처명→소매처코드
│   ├── retail_user.csv         # 이름 기반 소매처 검색 + 판매처 기본값 (소매처코드→소매처명+판매처코드+판매처명)
│   ├── unit_price.csv          # 제품코드→시키리·본부장·JANコード
│   ├── ocr_retailer.csv        # 확정 캐시: OCR거래처명→소매처코드 ← 분석 시 자동 누적
│   ├── ocr_product.csv         # 확정 캐시: OCR제품명→제품코드 ← 분석 시 자동 누적
│   ├── ocr_dist.csv            # 확정 캐시: (form_id,issuer_fingerprint,소매처코드)→판매처코드 ← 분석 시 자동 누적
│   └── form_columns.json       # 양식별 DB 저장 컬럼명 정규화 매핑
├── config/                     # Python 실행용 설정 (개발자 소유)
│   └── form_types.json         # 양식별 NET 수식·タイプ분류·출력 타입 정의 (phase4가 읽음)
├── scripts/                    # 보조 스크립트 (개발자 소유)
│   ├── phase4_calc.py          # NET 계산 결정적 코드 (phase4.py subprocess로 호출)
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

### 항상

- 사용자가 잘 모르는 부분은 *자율 판단보다 짧게 묻고 진행*

## 절대 잊지 말 것

- **회계 산수는 LLM이 하지 않는다.** 분류·식별은 Claude, 계산은 결정적 코드. 재현성이 생명.

## 다음 단계

1. ✅ 워크스페이스 초기화 및 Phase 1~4 스펙 완성
2. ✅ form_01, form_04 정의
3. ✅ `.claude/agents/` · `.claude/commands/` · skills 구현
4. ✅ Python 백엔드 파이프라인 구현 (backend/pipeline/)
5. ✅ FastAPI + React 프론트엔드 구현
6. ✅ Prompt Caching 최적화 (phase1: 첫 페이지 sequential → 나머지 parallel)
7. ✅ cold-start UI (프론트엔드 — `ColdStart.tsx` 구현됨)
8. ✅ 1차/2차 검토 체크 저장 (`backend/api/routes/reviews.py`, `v3_reviews` 테이블)
9. ✅ 문서 확정 플래그 — `v3_documents.confirmed_at` 컬럼 추가 + 자동 확정·해제 API + 잠금 UI
10. ✅ SAP 업로드 화면 — 연월·문서명·확정여부 필터, 엑셀 미리보기·다운로드
11. ⏭ form 02·03·05 `form_definitions/form_XX.md` 작성 (업무규칙 수령 후)
12. ⏭ 중간 산출물 정리 로직 추가 — `samples/<doc_id>_pages/` OCR txt, `extracted/<doc_id>/` MD·JSON은 DB 업로드 성공 후에도 영구 잔류함. 운영 규모에서는 삭제 또는 아카이브 필요. **사이클 종료 시점(현업과 협의 후) 확정 전까지 삭제 로직 추가 보류.**

---

**Last Updated:** 2026-05-26 (hatsu_month 청구연월 full-stack 구현, T列 config화, MD 파일 정비)
