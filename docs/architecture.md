# Architecture 

새 시스템의 설계 합의 사항. 결정 근거(왜 X 대신 Y인지)도 함께 기록한다.

## 1. 목적

기존 시스템(GPT-4 + RAG 기반. Azure OCR → pgvector + BM25 hybrid 검색 → GPT-4 분석)은 RAG로 페이지마다 유사 예시를 검색해서 LLM에 던지는 구조. 두 가지 한계가 리팩을 트리거한다:

1. **신규 양식 cold-start 불가** — 처음 보는 양식은 RAG가 *유사 페이지 0건* → 엉뚱한 예시 검색 → LLM이 의미 없는 추출. 개발자가 수동으로 정답지(`is_answer_key_document`)를 만들어야 비로소 학습 시작. 그동안 사용자는 결과를 받지 못함.
2. **다중 페이지 합성 불가** — 페이지 단위 분석이라 표지의 계약 조건과 상세 페이지의 항목, 마지막 총합 페이지를 종합 해석하지 못함.

새 시스템은 Claude long-context로 PDF 통째 분석 + 신규 양식 cold-start를 운영자 주도 수동 프로세스로 지원한다.


## 2. 자동 파이프라인

PDF 업로드 직후 끝까지 자동 실행. 사용자 개입은 *검토 단계*에서만 발생.

```
[1] React: PDF 업로드 (multipart)
        ↓
[2] Backend: Azure OCR (페이지별 병렬)
        → page1.txt … pageN.txt
        ↓
[3] Phase 1 (Claude): 페이지 MD 생성
        - 각 페이지 OCR → 구조화 MD (페이지별 병렬 호출)
        - 섹션 구성: ## 헤더 (키-값) / ## 조건·기타 / ## 테이블: {제목} (TSV→마크다운)
        - page_role·related_pages는 단일 페이지로 신뢰성 있는 판단 불가 → Phase 2에서 처리
        - extracted/<doc_id>/page_001.md … page_NNN.md 저장
        - 프롬프트 스펙: [phase1-prompt.md](phase1-prompt.md)
        ↓
[4] 양식 식별 (결정적 패턴 매칭 — LLM 없음)
        - form_[0-9]*.md 순회 → ## 식별 패턴 섹션의 키워드를 OCR txt와 문자열 매칭
        - 전체 패턴 일치 → (form_id, 1.0) / 불일치 → ('unknown', 0.0)
        ├── 매칭 성공 → [5] 정상 흐름
        └── 실패(unknown) → error 상태로 중단, 운영자가 cold-start로 수동 대응
        ↓
[5] Phase 2 (Claude): 페이지 역할 판단 + 항목 추출 + 조건 분류
        - 번들 감지 → detail 페이지 수 기준 청크 분할 → 청크별 병렬 호출 후 머지
          (detail ≤ 4페이지이면 단일 호출)
        - form_definitions/form_XX.md를 시스템 프롬프트에 포함
        - page_role (cover/detail/summary) 및 related_pages 판단 (Phase 1에서 이관)
        - 출력:
            {
              "pages": [{"page": 1, "role": "cover", "totals": {...}}, ...],
              "items": [{"customer": ..., "qty": ..., "raw_prices": {...},
                         "applied_conditions": [...], "source_pages": [...]}]
            }
        - 산수는 하지 않음 — 분류·식별만
        ↓
[5b] Phase 2 역산 검증 (Claude Haiku)
        - 管理No計 불일치 행을 핀포인트 재요청 (phase2_verify.py)
        - 오류 시 경고만 남기고 계속 진행
        ↓
[6] Phase 3 (Claude + 사용자): 이름→코드 매핑 + タイプ 분류
        - OCR 거래처명 → 소매처코드·판매처코드
        - OCR 제품명 → 제품코드
        - CSV 후보 검색 + Claude 판단 → 사용자 확인
        - 확정 매핑 캐시: mappings/ocr_retailer.csv, mappings/ocr_product.csv
        - 판매처코드: retail_user.csv 후보 + form별 issuer_fingerprint tiebreak (캐시: ocr_dist.csv)
        - OCR 得意先名·商品名·条件区分 → form_XX.md 업무규칙 참조 → タイプ 확정
        - 기본값 条件, 사용자 검토 시 수정 가능
        - RAG 없음 — Claude가 파일 직접 읽고 판단
        - 상세: docs/phase3-prompt.md
        ↓
[7] Phase 4 (Python + Claude 교차검증)
        - [Python] phase4_calc.py subprocess: unit_price.csv 재조회 + form별 NET 계산
        - [Claude Haiku] form_XX.md 교차검증 섹션 기준으로 totals vs detail 합계 비교, 불일치 ⚠️ flag
        - SAP Excel 열 매핑 (C·J·M·T·U·W·AL 등)
        - v3_mappings 저장
        - 상세: docs/phase4-design.md
        ↓
[8] SSE → React 그리드 표시
        ↓
[9] 사용자 검토: 1차/2차 체크, 플래그된 행 우선
        (선택) ad-hoc 질의 → Claude 호출 (해당 PDF page MD 컨텍스트만)
        ↓
[10] SAP 내보내기 (Python, 결정적, LLM 없음)
        v3_mappings → form_types.json의 sap_quantity·sap_extra_columns → Excel
```

## 3. Claude·Python 하이브리드 분담

설계 핵심: **Claude는 분류·식별, Python은 산수·검증**.

### Claude의 일

- 페이지 역할 식별 (cover / detail / summary)
- 페이지 간 관계 추론 (related_pages, 일본어 anaphora: `上記`, `同条件`, `前記`, `当該`)
- 항목 식별 + 텍스트 정규화
- **적용 조건 분류** (`applied_conditions: ["二重条件_A_and_B", "用役費別途"]`)
- **이름→코드 매핑**: CSV 후보 제시 + 판매처 tiebreak 판단 + 사용자 확인 (Phase 3)
- **タイプ 분류**: 기본값 条件. 양식별 업무규칙 수령 후 분류 로직 추가 예정 (Phase 3)

### Python의 일

- 단가·금액 계산 (form_types.json의 calc_rules 실행, phase4_calc.py)
- SAP Excel 생성

### Claude의 일 (Phase 4)

- 교차검증: form_XX.md 교차검증 섹션 기준으로 totals vs detail 합계 비교 (Haiku 4.5)

### 결정 근거

- 70,000행 회계 결과는 *재현성*이 생명. LLM에 산수를 시키면 같은 입력에 다른 출력 가능.
- "왜 이 행이 5,500엔인가?" 질문에 *Python 한 줄*을 보여주는 게 안전. LLM 응답을 보여주면 회계 감사 불가.
- 신규 양식 cold-start 시 사용자가 검토할 룰이 *실행 가능한 데이터*(JSON)이므로 즉시 반영 가능.

### 예시 (이중조건)

Claude가 분류:

```json
{
  "customer": "○○商店", "qty": 50,
  "applied_conditions": ["二重条件_A_and_B"],
  "raw_prices": {"price_A": 120, "price_B": 100},
  "source_pages": [1, 3]
}
```

`form_types.json`에 룰 (`net` 키):

```json
"form_02": {
  "net": {
    "二重条件_A_and_B": "price_A * 0.7 + price_B * 0.3"
  }
}
```

Python (`phase4_calc.py`):

```python
unit = price_A * 0.7 + price_B * 0.3   # 114
amount = unit * qty                     # 5,700
```

## 4. 신규 양식 대응 (cold-start)

cold-start 절차는 별도 문서: [cold-start.md](cold-start.md). 핵심만:

- 양식 식별 실패 시 파이프라인이 `unknown_form` 에러로 중단 → 운영자에게 알림
- 운영자가 Claude Code 채팅에서 `/cold-start` 또는 `update-form` 스킬 실행
- Claude가 기존 `form_definitions/*.md`를 형식 예시로 보고 신규 `form_NEW.md` 초안 + `form_types.json` 룰 후보를 작성
- 운영자 검토·승인 후 `_index.md`에 등록 → 다음 분석부터 정상 파이프라인으로 처리
- 코드 배포 없음

## 5. SDK 선택

**Anthropic SDK + Prompt Caching.**

| 후보                       | 적합도               | 이유                                                                                                      |
| -------------------------- | -------------------- | --------------------------------------------------------------------------------------------------------- |
| **Anthropic SDK** ⭐ | 메인 파이프라인 정답 | 단일 호출, 결정적                                                                                         |
| Claude Agent SDK           | 부분적               | cold-start의 일부에만 고려 가능. 메인엔 과함                                                              |
| MCP                        | 미사용               | 도구 서버 표준 — 이번 과제엔 불필요                                                                      |
| Claude Code Skill          | 운영 중              | analyze-invoice·azure-ocr·update-form·sync-form-config 등 운영자 ad-hoc 도구로 사용 중. 백엔드 자동 파이프라인과는 별개 |

### 모델 라우팅

| 단계                     | 모델       | 이유                                            |
| ------------------------ | ---------- | ----------------------------------------------- |
| Phase 1 (페이지 MD 생성) | Haiku 4.5  | 페이지 단위 구조화 작업, 소규모, 병렬 다수 호출 |
| 양식 식별                | LLM 없음   | 결정적 패턴 매칭 (form_*.md 식별 패턴 키워드)   |
| Phase 2 역산 검증        | Haiku 4.5  | 管理No計 불일치 행 핀포인트 재요청              |
| Phase 3 (매핑 판단)      | Haiku 4.5  | CSV 후보 판단, 다수 호출                        |
| Phase 4 교차검증         | Haiku 4.5  | totals vs detail 합계 비교                      |
| Phase 2 (추출·분류)      | Sonnet 4.6 | 전체 MD 조합 분석, 조건 분류, 200K 컨텍스트     |

### 비용 레버

- **Prompt Caching**: 시스템 프롬프트 + form 정의는 90% 할인. 같은 양식 PDF들을 묶어 처리하면 캐시 hit 극대화.

## 6. 디렉토리 구조

이 워크스페이스(`Lecture/`)가 실제 운영 코드 저장소다.

```
Lecture/
├── CLAUDE.md                    워크스페이스 컨벤션
├── MANUAL.md                    현업 사용 가이드
├── backend/
│   ├── api/routes/              FastAPI 라우터
│   ├── core/                    설정·DB·인증·유틸
│   │   ├── auth.py
│   │   ├── config.py
│   │   ├── database.py
│   │   ├── drive_storage.py
│   │   └── stall_guard.py
│   ├── db/
│   │   ├── queries.py           DB 쿼리 모음 (토큰 사용량 포함)
│   │   └── schema.sql
│   ├── pipeline/                분석 파이프라인
│   │   ├── orchestrator.py      Phase 1→2→3→4 오케스트레이터
│   │   ├── ocr.py               Azure OCR 호출
│   │   ├── phase1.py            OCR txt → page MD (Haiku, 캐싱)
│   │   ├── phase2.py            page MD → items[] JSON (Sonnet, 스트리밍)
│   │   ├── phase2_row_anchor.py
│   │   ├── phase2_verify.py
│   │   ├── phase3.py            소매처·제품·판매처 매핑
│   │   └── phase4.py            NET 계산·교차검증·SAP 포맷
│   └── main.py
├── config/
│   └── form_types.json          양식별 NET 수식·타입분류·출력 타입 정의
├── docs/                        설계 문서 (개발자 소유)
├── form_definitions/            양식별 기준 정보 MD (현업 소유)
│   ├── _index.md
│   ├── form_01.md, form_02.md, form_04.md, form_05.md
│   └── image/                   form 정의 근거 이미지
├── frontend/
│   └── src/
│       ├── api/                 API 호출 모음
│       ├── components/          레이아웃·PdfViewer 등
│       ├── context/             AuthContext·FormsContext
│       └── pages/               Dashboard·Upload·Results 등
├── mappings/                    소매처·제품 매핑용 CSV 마스터 (현업 소유)
│   ├── domae_retail_1.csv       form_01: 도매소매처코드→소매처코드
│   ├── ocr_dist.csv             확정 캐시: (form_id,issuer,소매처코드)→판매처코드
│   ├── ocr_product.csv          확정 캐시: OCR제품명→제품코드
│   ├── ocr_retailer.csv         확정 캐시: OCR거래처명→소매처코드
│   ├── retail_user.csv          소매처코드→소매처명+판매처코드+판매처명
│   └── unit_price.csv           제품코드→시키리·본부장·JANコード
├── scripts/                     보조 스크립트 (개발자 소유)
│   ├── phase4_calc.py           NET 계산 결정적 코드 (phase4.py subprocess 호출)
│   ├── cleanup_phase1.py        page MD 후처리
│   ├── export_excel.py
│   └── upload_to_db.py
├── extracted/                   분석 산출물 (doc_id별 page MD + phase 출력)
└── samples/                     검증용 PDF
```

## 7. 의사결정 기록

| 결정                         | 채택                                                | 기각된 안                    | 근거                                                                                                  |
| ---------------------------- | --------------------------------------------------- | ---------------------------- | ----------------------------------------------------------------------------------------------------- |
| 분석 단위                    | PDF 통째 long-context                               | 페이지 단위 RAG              | 다중 페이지 합성, 신규 양식 대응                                                                      |
| Claude의 일                  | 분류·식별만                                        | 산수까지 (Option A)          | 70,000행 회계 재현성                                                                                  |
| 룰 위치                      | form 정의 MD + form_types.json (이중)               | Python 코드 (현 시스템)      | 코드 배포 없이 cold-start                                                                             |
| Phase B SDK                  | Anthropic SDK + Prompt Caching                      | Agent SDK / MCP              | 결정적, 캐싱 비용 절감                                                                                |
| Phase A 인터페이스           | Claude Code 채팅 직접 질의                          | React 웹 UI                  | API 키 없는 현재 단계                                                                                 |
| Phase 1 호출 방식            | 페이지별 병렬 호출                                  | PDF 통째 1콜                 | 시스템 프롬프트 캐싱 + 재시도 단위 축소                                                               |
| Phase 1 출력 형식            | 마크다운만                                          | JSON                         | JSON은 Phase 2 담당. Phase 1은 사람·Claude 가독성용                                                  |
| Phase 1 테이블 소스          | TSV 기준                                            | 자유 텍스트                  | 열 위치(컬럼 귀속) 보존 — 자유 텍스트는 위치 정보 없음                                               |
| Phase 1 헤더 키 언어         | 일본어 원문                                         | 한국어 번역                  | form_XX.md 필드명과 언어 통일                                                                         |
| page_role·related_pages     | Phase 2 담당                                        | Phase 1 담당                 | 단일 페이지로 신뢰성 있는 판단 불가                                                                   |
| Phase 2 출력 구조            | {pages[], items[]} 래퍼                             | items[]만                    | Phase 3 교차검증에 page_role 필요                                                                     |
| Phase 2 items[] 필드 구조    | form 정의에 위임                                    | phase2-prompt에 하드코딩     | 그룹 식별자(invoice_no vs kanri_no 등)가 양식마다 달라 프롬프트를 고정하면 양식 추가 때마다 수정 필요 |
| Phase 2 form 정의 전달 위치  | 시스템 프롬프트                                     | 사용자 메시지                | 같은 양식 PDF 묶어 처리 시 캐시 hit 극대화                                                            |
| Phase 1 모델                 | Haiku 4.5                                           | Sonnet                       | 구조화 작업, 병렬 다수 호출, 비용                                                                     |
| 검색 인프라                  | 없음 (long-context)                                 | RAG / pgvector / BM25        | 컨텍스트 충분, 단순화                                                                                 |
| Phase 3 매핑 방식            | Claude가 CSV 직접 읽고 판단 + 사용자 확인           | RAG 벡터 유사도              | CSV 총 6,000행 — long-context로 충분. RAG보다 판단 근거 투명                                         |
| 판매처코드 캐시              | ocr_dist.csv 별도 파일 (form_id + issuer_fp + 소매처코드 복합키) | ocr_retailer.csv에 함께 저장 | 동일 소매처도 발행 지점에 따라 달라짐 — 복합키로 분리 캐시 |
| domae_retail_1 소매처명 컬럼 | 무시 (코드→코드 변환만 사용)                       | 이름 매칭에 활용             | 실제 내용이 판매처명(加藤産業...)이고 소매처명(ダイレックス...)이 아님 — 기존 시스템에서 몰랐던 사실 |
| タイプ 분류 방식             | 기본값 条件, 업무규칙 수령 후 양식별 로직 추가 예정 | 하드코딩 룰 / 코드 기반 매핑 | 분류 기준 미정. 사용자가 결과 검토 시 수정 가능                                                       |
| タイプ 기본값                | 条件                                                | 판촉비 / 빈값                | 신규 조합은 条件으로 선표시 후 사용자 수정 — 자동화 범위 밖 케이스에 안전한 기본값  |
| Obsidian                     | 미사용                                              | (기존 vault 패턴)            | Claude Code만 의존                                                                                    |
| SAP Excel 생성               | Python 결정적                                       | LLM 변환                     | 회계 재현성, 비용, 검증성                                                                             |
| 신규 양식 학습               | 운영자 수동 cold-start (Claude Code 채팅)           | 파이프라인 자동 분기         | 미인식 시 파이프라인 에러 중단 → 운영자가 form 정의 작성 → 재분석                                    |
| 산식·표시 규칙 작성 주체     | **LLM이 자연어→DSL 컴파일, 결정적 엔진이 실행** (검증 게이트·동결 경유) | ① 개발자 코드만 (현 상태) ② LLM이 런타임 계산까지 | 원칙을 폐기가 아니라 *이동* — "LLM은 산식을 작성하되 실행하지 않는다". 런타임 숫자는 여전히 결정적 코드. 재현성 유지하며 자연어 확장성 확보. 설계: [nl-to-dsl-pipeline.md](nl-to-dsl-pipeline.md) |

## 8. Phase A — 채팅 기반 워크플로우 (완료)

API 키 확보 전 PoC 단계. Claude Code 채팅 인터페이스에서 직접 질의해 문서를 처리했다. **Phase B(자동 파이프라인)로 전환 완료.**

### 목표 (달성)

| 목표               | 설명                                                                   |
| ------------------ | ---------------------------------------------------------------------- |
| 목표1 (MD 생성)    | OCR txt 읽기 → 구조화 MD 생성 → 기준 문서로 자체 검증                |
| 목표2 (cold-start) | 신규 양식 발견 시 대화로 form 정의 MD 초안 작성, 기준 문서도 함께 작성 |
| 목표3 (분석)       | "이 문서 분석해줘" → 관련 MD + 기준 문서 조합 → 구조화 데이터 준비   |

### 산출물 (완료)

- `docs/phase1-prompt.md` ✅ — MD 생성 기준 + 검증 기준 통합
- `docs/phase2-prompt.md` ✅ — 항목 추출·분류 기준
- `docs/phase3-prompt.md` ✅ — 매핑 판단 기준
- `form_definitions/form_01.md, form_04.md` ✅ — 양식별 기준 정보 (등록 완료)
- `form_definitions/form_02.md, form_05.md` ⏭ — 파일 존재하나 미등록 (_index.md, form_types.json 미완)

### Phase B 전환 완료 조건

- Anthropic API 키 확보 ✅
- form_definitions/ 기존 양식 복수 작성 완료 ✅
- Phase A로 PoC 검증 완료 (MD 품질 확인) ✅

## 9. 미해결 / 다음 결정 필요

- **검토 UI 폼 설계** — raw MD 편집 vs 폼 위에 폼 (cold-start.md §5 참조)
- **양식 식별 정확도** — Haiku로 충분한지, 양식 수가 늘어나면 인덱스 분할 필요?
- **Prompt 버전 관리** — 시스템 프롬프트 변경 시 어떻게 마이그레이션
- **재처리(re-extract) 정책** — 기존 데이터를 새 모델/프롬프트로 다시 돌릴지
- **이중 룰 표기 정합성** — form_definitions/form_XX.md(자연어)와 form_types.json(실행 룰)이 어긋나지 않도록 하는 메커니즘
- **공식 단일 진실 소스** — form_types.json 공식을 프론트(JS)·백엔드(Python) 양쪽이 읽도록 설계. 사용자 条件2 입력 → 프론트 실시간 NET 계산, [저장] 클릭 → 백엔드 Phase 4 재실행 → DB 갱신. 동일 공식이 두 곳에 존재하므로 form_types.json을 단일 진실 소스로 통일 필요.

---

## 10. 두 레이어 구조

```
┌─────────────────────────────────────────────────────┐
│  가이드 레이어 (현업이 소유)                           │
│                                                      │
│  form_definitions/form_XX.md  ← 여기가 지능의 핵심   │
│  mappings/*.csv                                      │
└────────────────────┬────────────────────────────────┘
                     │ 읽는다
┌────────────────────▼────────────────────────────────┐
│  분석 레이어 (개발자가 소유)                           │
│                                                      │
│  Phase 1 → 2 → 3 → 4 파이프라인                      │
│  (.claude/skills, agents, backend/pipeline/)         │
└─────────────────────────────────────────────────────┘
```

분석 레이어는 고정된 파이프라인(코드). 가이드 레이어는 파이프라인에게 "이 양식은 이렇게 읽어라"를 가르치는 설정(MD 파일).
**새 양식이 들어오면 현업이 form_XX.md만 추가하면 된다. 코드 배포 없음.**

### form_XX.md가 파이프라인에서 하는 역할

| Phase   | form_XX.md에서 읽는 것                              |
| ------- | --------------------------------------------------- |
| Phase 1 | 영향 없음 (OCR 구조화)                              |
| Phase 2 | 추출 컬럼 정의, 페이지 역할, 함정 케이스, 출력 예시 |
| Phase 3 | タイプ 분류 규칙                                    |
| Phase 4 | NET 계산식, 교차검증 기준                           |

### 신규 양식 대응 흐름 (마중물 단계)

```
새 양식 PDF 도착 → 양식 식별 (패턴 매칭, Phase 1 이전)
     │
     ├─ 매칭 성공 ──────────────────────────► 정상 분석
     │
     └─ 매칭 실패
          │
          ├─ 방법 A: 샘플 이미지 캡처 → 채팅에 붙여넣기 → "반영해줘"
          │          → update-form 스킬이 form_XX.md 초안 생성
          └─ 방법 B: /cold-start 실행 → 대화로 form_XX.md 작성
          │
          ▼
     사용자 승인 → _index.md 등록
          │
          ▼
     파이프라인 즉시 재개 ─────────────────► Phase 2 → 3 → 4 → 결과
```

---

## 11. Claude Code 레이어 — 에이전트 구조

Claude Code 채팅에서 운영되는 오케스트레이터·워커 구조.

```
메인 Claude (오케스트레이터)
  │
  ├── Phase 1 (5페이지 이상)
  │     ├── page-md-generator (1-3페이지)  ──┐
  │     ├── page-md-generator (4-6페이지)  ──┤ 병렬
  │     └── page-md-generator (7-9페이지)  ──┘
  │     메인이 결과 수집 → 검증(C1~C7) → 다음 Phase 판단
  │
  └── Phase 3
        ├── retailer-mapper  ──┐ 동시 spawn
        └── product-mapper   ──┘
        메인이 NEEDS_CONFIRMATION 판단 → 사용자 확인 여부 결정
```

### 에이전트 목록

| 에이전트              | 역할                       | spawn 조건                  |
| --------------------- | -------------------------- | --------------------------- |
| `page-md-generator` | OCR txt → page MD         | 5페이지 이상 시 3장씩 병렬  |
| `retailer-mapper`   | OCR 거래처명 → 소매처코드 | 항상 product-mapper와 동시  |
| `product-mapper`    | OCR 제품명 → 제품코드     | 항상 retailer-mapper와 동시 |

### Agent tool을 쓰지 않는 기준

| 조건                                   | 이유                                               |
| -------------------------------------- | -------------------------------------------------- |
| Phase 2 (전체 page MD → items[] 추출) | 표지·상세·요약을 함께 봐야 해 컨텍스트 분리 불가 |
| 순차 의존 (Phase 1 → 2 → 3 → 4)     | 앞 단계 결과가 다음 단계 입력                      |
| 독립성 없는 단일 작업                  | 오버헤드만 증가, 중복 토큰 비용 발생               |

Python 백엔드의 병렬화는 `asyncio`(`orchestrator.py`)가 담당. Claude Code Agent tool과 별개.

---

| Phase 2 청크 실행 방식 | 청크별 병렬 (asyncio.gather + MAX_CONCURRENT_PHASE2_CHUNKS 세마포어, 기본 3) | 순차 루프 | detail 5페이지 이상 문서가 흔함. 세마포어로 레이트 리밋 방어. write_output=False로 중간 파일 충돌 제거 |
| Phase 3 판매처 1:N 결정 | jisho 우선 → issuer fingerprint → NEEDS_CONFIRMATION | issuer fingerprint만 사용 | jisho는 items에서 이미 추출된 값으로 판매처를 더 직접적으로 특정함 |

| PHASE2_OVERLAP | 0 고정 | overlap > 0 | overlap 시 청크 경계 페이지가 두 번 추출 → dedup 필요 → dedup 버그. 동일 제품명이 다른 管理No에 등장 시 kanri_no 없는 hash로 소실 (4월CVS① 실사례). 대신 phase2_verify가 管理No計 역산으로 결정적 복구 |
| Phase 2 row anchor 도입 (form_04) | Python anchor 생성 + LLM item/not_item 판단 + 2차 管理No計 역산 | 합계 역산 후처리만 | 반복표 LLM 누락 행을 row_id 직접 추적으로 복구. 페이지 경계 오염·cross-kanri dedup·jisho_hint 형식 불일치 버그 수정 포함 (2026-06-01) |
| Phase 3 재설계 (2026-05-19) | Claude가 phase3-prompt.md + form_XX.md 읽고 단일 호출로 판단 | Python form_id 분기 3곳 하드코딩 | form_id 분기 제거 → 새 양식 추가 시 코드 수정 불필요 원칙 복원 |
| Phase 3 Tool Use 전환 | feature flag 방식 (PHASE3_TOOL_USE_ENABLED, 기본 OFF) | 즉시 레거시 교체 | CSV 통주입 탈피·결정 과정 관찰 가능·단위 테스트 489개 확보. 기반 공사 완료(2026-06-05). 레거시를 fallback으로 유지하며 점진 검증 |
| Phase 3 Tool Use Limited Rollout Ready (2026-06-05) | E2E 통합 테스트(18개·실제 CSV 기반) + product Tool Use(캐시+Claude 결정) + Retry-After + concurrency(semaphore) + token usage 기록(success/fallback 공통) 완료 | Full Rollout | dist 1:N Claude 결정 미구현으로 Full Rollout 보류. `CONCURRENCY=1` 기본값 유지(rate limit 미확인). `_attempt_tool_use_phase` generic except가 ToolUseFallbackTrigger를 ToolUseDispatchError로 래핑하는 minor issue 잔존(동작 무해) |
| Tool Layer 역할 원칙 확정 | lookup_retailer·search_product·confirm_mapping은 조회/저장 전담, Claude는 판단(어느 코드인가)만 담당 | Claude에 CSV 직접 주입 | Tool Contract로 side_effects/idempotent 명시. allow_side_effects=False 가드로 실험과 확정을 분리. confirm_mapping은 성공 후 1회만 호출(중복 없음) |

| Phase 3 Tool Use Controlled Production Enable (2026-06-05) | tool_choice 강제(lookup_retailer), tool_not_called 감지→fallback, 전역 세마포어(PHASE3_TOOL_USE_GLOBAL_CONCURRENCY) | 다중 사용자 동시 업로드 허용 | 다중 문서 동시 처리 시 Claude API Rate Limit → legacy fallback 발생 문제 해결. 전역 세마포어로 동시 Tool Use 문서 수 제한(기본 3). Limited Rollout 3차 PASS(9건, fallback 0, avg_turns 3.0, CR 42/42) |

| Dist 1:N Claude 결정 구현 (2026-06-05) | _run_single_dist_mapping — 후보 제공 후 단일 Claude 호출로 판매처 선택 | legacy pending만 처리 | 후보 2건 이상: Claude 판단 → tool_use 확정 or needs_confirmation 유지. 후보 1건: auto_1_to_1 그대로. 후보 외 선택 거부. confirm_mapping 경유 저장. ToolUseTokenStats에 dist 필드 추가 |

| Phase 3 Tool Use 전환 완전 완료 (2026-06-05) | retailer(tool_choice 강제·decided_code 캡처) + product(cache·Claude 결정) + dist 1:N(Claude 판단·후보 외 거부) + Global Semaphore(rate limit 방지) + 전체 검증 통과 | — | 645 tests passed. FastAPI token DB 기록 확인. 4월 CVS 12건 동시 처리 성공. 운영 적용 완료. 남은 리스크: normalize_ocr_name 특수문자(P1), phase3_fallback.py 구조 분리(P2) |

| Phase 4 DSL 전환 완료 (2026-06-05) | `formula_type: "expr"` DSL 경로를 기본으로 채택. `_safe_eval` AST 평가기 구현. form_01·04 전환 완료 | eval() 직접 사용, 양식별 if 분기 하드코딩 | LLM 없음·결정적 계산·재현성 보장. Plugin은 예외 경로로 개발자 승인 필수. JSON Schema + 회귀 테스트로 안전장치 강화. 전체 테스트 681 passed |

| md-driven 강화 일괄 적용 (2026-06-12) | ① 프롬프트 로더 mtime 캐시 — docs/*.md 수정이 재시작 없이 반영 (phase1·2·3·product tool-use) ② Tool Use 제품 매핑 프롬프트를 phase3_fallback.py 인라인에서 docs/phase3-tool-use-product-prompt.md로 이동 ③ 消費税率 규칙을 config/tax_rules.json으로 분리 (코드 하드코딩 제거, 없으면 명시적 오류) ④ phase2_verify 결정적 복구 셀 인덱스를 form_types.json row_anchor.recovery_cell_map으로 이동 (미정의 양식은 Haiku 폴백 + 로그) ⑤ docs/output-format.md ↔ sap.py 컬럼 contract test 추가 | 프로세스 재시작 의존·인라인 프롬프트·세율/레이아웃 하드코딩 유지 | 출력 컬럼은 전 양식 공통 상수로 유지하되 contract test로 문서·코드 괴리를 CI에서 차단 (D→B). recovery_cell_map은 개발자 관리 필드 — sync가 보존(코드 강제 + 테스트 고정) |

| 프론트 인증 안정화 (2026-06-12) | me() 부트스트랩을 401일 때만 세션 제거 + 일시 오류는 백오프 재시도(0/2/5s) + storage 이벤트로 탭 간 세션 동기화 + Results stale 응답 가드 | 모든 에러에서 session_id 제거 (기존) | "동시분석 중 새 창 로그인 튕김"의 원인 = 타임아웃·일시 오류를 로그아웃으로 처리하던 catch-all. 백엔드는 stateless JWT라 유효 토큰은 401이 나지 않음 — 401만 진짜 만료 |

| Remaining Risks 처리 #3·#2·#4 (2026-06-15) | #3 token.pickle 조기경보: SheetsStore.probe()+fetch 에러 추적, /health에 sheets 상태·/health/sheets deep probe(503), 분석 진입 전 마스터(unit_price) 가용성 가드(빈 마스터=장애→명시적 error, '조용한 오답' 차단), phase4_calc unit_price 빈결과 sys.exit. #2 회귀 픽스처 고정: gen_regression_fixture.py로 phase3+phase2 입력·참조 마스터를 tests/fixtures/regression/<form>/에 박제, run()이 _sheets_store=None+base_dir로 Sheets/extracted 독립 실행 → form_01·form_04 둘 다 CI에서 Sheets 없이 통과(form_04는 doc③ 신규 골든으로 커버리지 복원). #4 영향 가시화: sync 시 골든 번들로 변경 전/후 NET 재계산→변동 행수·금액 delta·샘플을 sync_status.impact에 기록, FormManagement 뱃지 '수식 변경 · N행' | 차단형 자동 게이트 | #4는 차단이 아니라 가시화 — 현업이 영향 보고 판단(Phase4 교차검증이 2차 방어선). 골든 번들 없으면 available=False로 sync 안 막음 |

| 제품 매핑 용량 우선 (2026-06-15) | search_product 후보 검색에서 OCR 용량이 있으면 '용량 일치' 후보를 점수와 무관하게 1차 정렬키로 위에 올리고 컷오프(0.3) 면제. 용량 일치 판정을 ±5% 비율(0.95)에서 정수 정확 일치(±0.5g)로 좁힘 | 용량을 단일 점수에 ±가산만 | 103↔105·113↔114처럼 인접하지만 다른 용량을 '같은 용량'으로 오판해 이름 더 비슷한 틀린 제품에 가산까지 주던 버그(사용자 보고: "103인데 105 가져옴") 해소. 105 등 불일치 후보는 목록에 남되 하위로(재고 여지). 입수(24入 vs 12入) 변별·용량추출 보강(ml·단위없음)은 후속(3·4순위) |

| 제품별 집계 이중조건 분해 (2026-06-16) | DSL 어휘 확장 — `build_product_aggregate` op 추가(scripts/phase4_calc.py): 제품(jisho·customer·product_code) 단위로 定番 총수량에서 추가조건(原価引き·導入) 수량을 차감 분해, 금액은 定番 총금액을 수량 비율로 배분+추가조건 원본금액 합산(원본 총금액 보존). form_types.json `product_aggregate`(form_04) 설정으로 활성, 동적 조건 컬럼. 프론트 결과화면 '제품별 집계' 탭이 product_aggregate 있으면 동적 컬럼 분해 표시, 없으면 기존 condition_type 집계 | 청구서 조건별 행 단순 나열(이중계산) | 회계 계산은 백엔드 결정적 코드, 프론트는 표시만. 골든 단위테스트 7개(캡처 264/2352/204/2352→2352/204/60, 금액 보존). 연산 레지스트리 방식의 첫 어휘 — 같은 이중조건 패턴은 이후 다른 양식서 config로 재사용 |

| 연산 레지스트리 설계 합의 (2026-06-17, 설계만) | 현업이 고른 두 축(집계/분해 전략·조회/매핑 차원)을 `if form_id` 분기 없이 **이름 붙은 전략 레지스트리 + config 선택**으로 일반화하는 설계 문서 [registry-driven-primitives.md](registry-driven-primitives.md) 작성. cross_validation이 이미 쓰는 레지스트리 패턴을 두 축에 확장. nl-to-dsl-pipeline.md의 G3(연산 어휘 확장)·T3을 구체화 | 두 축이 phase4/phase3 코드·시트 스키마에 하드코딩 | "해석기에 없던 차원도 현업이 제어" 요구의 현실적 해 = 변화의 *축*을 1회 일반화→이후 변종은 T1/T2 설정으로. 임의 코드 실행은 영구 금지(화이트리스트만). 로드맵 P1=축A 레지스트리(form_04 무손실 이전, 위험 낮음) 먼저, P2=축B 차원 선언(캐시 합성키 마이그레이션, 시트 백엔드라 비용 큼) |

| 연산 레지스트리 P1 구현 (2026-06-17) | 축A(집계/분해 전략) 레지스트리화: `scripts/aggregate_strategies.py` 신규(register/get_strategy + `subset_subtract` 전략 = 기존 이중조건 분해 로직 이관). `build_product_aggregate`는 그룹핑·display_columns 생성 + 전략 디스패치만 하는 오케스트레이터로 축소(`if form_id` 분기 없음 유지). form_04 config `product_aggregate.strategy:"subset_subtract"` 명시, form_types.schema.json `ProductAggregate` 정의 추가 | 분해 알고리즘이 phase4_calc에 하드코딩 | 무손실 이전 — 골든 7 + 회귀 그린(730 passed, 유일 실패는 무관한 기존 phase3 retailer smoke). 전략명은 런타임 get_strategy가 단일 출처로 검증(미등록 시 명확 차단). 같은 분해 쓰는 새 양식 = config 한 줄(T1). 다음: P2 축B(조회 차원, 캐시 합성키) |

**Last Updated:** 2026-06-17 (연산 레지스트리 P1 — 집계/분해 전략 레지스트리화, form_04 무손실 이전)

---

## 12. Phase 2 청크 구성 상세 + 리스크 맵

### Phase 2 청크 구성

detail 페이지 수 > `PHASE2_CHUNK_THRESHOLD`(기본 4) 시 청크 분할.

```
청크 구성: cover 전체 + 가장 가까운 summary ≤ PHASE2_MAX_SUMMARY(기본 3)개 + detail PHASE2_CHUNK_SIZE(기본 2)페이지

예) detail 7페이지 (cover p1, detail p2~p8, summary p9):
  청크0: [p1, p9, p2, p3]
  청크1: [p1, p9, p4, p5]
  청크2: [p1, p9, p6, p7]
  청크3: [p1, p9, p8]
```

청크 병렬 실행: `asyncio.gather` + `MAX_CONCURRENT_PHASE2_CHUNKS` 세마포어(기본 3).  
중간 파일 충돌 방지: 청크 호출 시 `write_output=False`, 머지 후 1회 저장.

### 누락 항목 리스크 맵

| 단계 | 리스크 | 원인 |
|------|--------|------|
| OCR | 셀 분열 | 복잡한 표 구조 → Phase 1 복원 실패 |
| Phase 1 | page_type_hint 오분류 | cover→detail 오분류 → 청크 구성 오류 |
| Phase 2 | 추출 누락 | 청크 내 管理No 블록이 많을수록 확률 상승 |
| Phase 2 | 청크 경계 분리 | 管理No 헤더와 計 행이 다른 청크에 분리 → 추출 불완전 |
| Phase 2 Verify | 복구 실패 | Haiku가 누락 항목을 찾지 못하거나 JSON 형식 오류 |
| Phase 3 | 매핑 미확정 | 새 거래처/제품명은 캐시 미스 → pending → 수동 확인 필요 |

### Phase 2 row anchor 위험 대응 (form_04)

row anchor 원칙: Python은 감사 가능한 후보 row를 **넓게** 만들고, LLM이 item/not_item과 의미를 판단한다.

| 위험 | 대응 |
|------|------|
| false negative — Python이 상품행 미탐지 → row_id 없음 | form_04에만 적용. 管理No計 2차 역산 감사로 보완 |
| false positive — 소계·헤더행이 후보에 포함됨 | LLM이 `not_item`으로 표시. `_HEADER_KEYWORDS` 필터 사전 차단 |
| jisho_hint 형식 불일치 → template lookup 실패 | `_RE_JISHO = re.compile(r'(\S+)')` 로 짧은 형식 저장 |
| cross-page kanri 오염 — 이전 페이지 kanri 상태가 다음 페이지 헤더 행에 귀속 | 페이지 루프 시작 시 `current_kanri = None` 리셋 |
