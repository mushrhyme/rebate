# Architecture — React Rebate v2

새 시스템의 설계 합의 사항. 결정 근거(왜 X 대신 Y인지)도 함께 기록한다.

## 1. 목적

기존 시스템([current-system.md](current-system.md))은 RAG로 페이지마다 유사 예시를 검색해서 LLM에 던지는 구조. 두 가지 한계가 리팩을 트리거한다:

1. **신규 양식 cold-start 불가** — 처음 보는 양식은 RAG가 *유사 페이지 0건* → 엉뚱한 예시 검색 → LLM이 의미 없는 추출. 개발자가 수동으로 정답지(`is_answer_key_document`)를 만들어야 비로소 학습 시작. 그동안 사용자는 결과를 받지 못함.
2. **다중 페이지 합성 불가** — 페이지 단위 분석이라 표지의 계약 조건과 상세 페이지의 항목, 마지막 총합 페이지를 종합 해석하지 못함.

새 시스템은 Claude long-context로 PDF 통째 분석 + 신규 양식 자동 학습을 *메인 파이프라인의 일급 분기*로 편입한다.

## 2. 유지·제거·갈아끼움

### 유지
- React 19 프론트 (탭 구조, `ItemsGridRdg`, `OcrOverlay`, 1차/2차 검토 체크)
- DB 스키마 (`documents_*`, `page_data_*`, `items_*`, `item_locks_*`, users/sessions, current/archive 패턴)
- 인증, WebSocket 진행률, APScheduler 아카이브 마이그레이션
- SAP Excel 생성 (form_types.json 기반 결정적 변환) — *전혀 건드리지 않음*

### 제거
- RAG 인프라 전체: `rag_manager.py`, `build_pgvector_db.py`, `build_faiss_db.py`, FAISS 인덱스, BM25
- 테이블: `rag_page_embeddings`, `rag_vector_index`
- 정적 프롬프트 묶음: `prompts/rag_with_example_v1`~`v11`, `prompt_v1`~`v5`, `zero_shot`
- 양식별 후처리 모듈: `form2_rebate_utils.py`, `form04_mishu_utils.py`, `finet01_cs_utils.py` 등 — *데이터로 외부화*
- 의존성: `sentence-transformers`, `faiss-cpu`, `rank-bm25`. (pgvector extension은 다른 용도 없으면 함께 제거)

### 갈아끼움
| 기존 | 신규 |
|------|------|
| `rag_pages_extractor.py` (페이지별 OCR→RAG→LLM) | `claude_extractor.py` (Phase 1: 페이지별 병렬 호출 / Phase 2: 전체 MD 묶어 1콜) |
| 양식별 Python utils | `form_definitions/form_XX.md` (Claude 읽기용) + `config/form_types.json` (Python 실행용) |
| 정적 프롬프트 파일들 | 시스템 프롬프트 + 양식 MD 동적 조립 + Prompt Caching |
| `is_answer_key_document` + RAG 학습 루프 | cold-start 분기 + 사용자 검토 UI |

## 3. 자동 파이프라인

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
[4] 양식 식별 (Claude Haiku, 가벼운 호출)
        - form_definitions/_index.md (양식 1줄 요약 인덱스) 참조
        - confidence + form_id 반환
        ├── 매칭 (confidence ≥ 0.7) → [5] 정상 흐름
        └── 실패 → cold-start 분기 ([cold-start.md](cold-start.md))
        ↓
[5] Phase 2 (Claude): 페이지 역할 판단 + 항목 추출 + 조건 분류
        - 모든 page MD 묶어 1콜
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
[6] Phase 3 (Claude + 사용자): 이름→코드 매핑 + タイプ 분류
        - OCR 거래처명 → 소매처코드·판매처코드
        - OCR 제품명 → 제품코드
        - CSV 후보 검색 + Claude 판단 → 사용자 확인
        - 확정 매핑 캐시: mappings/ocr_retailer.csv, mappings/ocr_product.csv
        - 판매처코드: retail_user.csv 후보 + form별 issuer_fingerprint tiebreak (캐시: ocr_dist.csv)
        - OCR 得意先名·商品名·条件区分·消費税率 → form_XX_type.xlsx 유사도 검색 → タイプ 확정
        - 기본값 条件, 사용자 검토 시 수정 가능
        - RAG 없음 — Claude가 파일 직접 읽고 판단
        - 상세: docs/phase3-mapping.md
        ↓
[7] Phase 4 (Python): NET 계산·교차검증·SAP 포맷 생성
        - unit_price.csv에서 시키리·본부장 재조회
        - form별 NET 계산 (form_types_reference.md 규칙)
        - cover/summary totals vs detail 합계 교차검증, 불일치 ⚠️ flag
        - SAP Excel 열 매핑 (C·J·M·T·U·W·AL 등)
        - items_current 저장
        - 상세: docs/phase4-design.md
        ↓
[8] WebSocket → React 그리드 표시
        ↓
[9] 사용자 검토: 1차/2차 체크, 플래그된 행 우선
        (선택) ad-hoc 질의 → Claude 호출 (해당 PDF page MD 컨텍스트만)
        ↓
[10] SAP 내보내기 (Python, 결정적, LLM 없음)
        items_current → form_types.json의 sap_quantity·sap_extra_columns → Excel
```

## 4. C 하이브리드 분담

설계 핵심: **Claude는 분류·식별, Python은 산수·검증**.

### Claude의 일
- 페이지 역할 식별 (cover / detail / summary)
- 페이지 간 관계 추론 (related_pages, 일본어 anaphora: `上記`, `同条件`, `前記`, `当該`)
- 항목 식별 + 텍스트 정규화
- **적용 조건 분류** (`applied_conditions: ["二重条件_A_and_B", "用役費別途"]`)
- **이름→코드 매핑**: CSV 후보 제시 + 판매처 tiebreak 판단 + 사용자 확인 (Phase 3)
- **タイプ 분류**: 기본값 条件. 양식별 업무규칙 수령 후 분류 로직 추가 예정 (Phase 3)

### Python의 일
- 단가·금액 계산 (form_types.json의 calc_rules 실행)
- 페이지 총합 vs 항목 합계 검증
- SAP Excel 생성

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

`form_types.json`에 룰:
```json
"form_02": {
  "calc_rules": {
    "二重条件_A_and_B": "price_A * 0.7 + price_B * 0.3"
  }
}
```

Python:
```python
unit = price_A * 0.7 + price_B * 0.3   # 114
amount = unit * qty                     # 5,700
```

## 5. 신규 양식 자동 대응

cold-start 분기는 별도 문서: [cold-start.md](cold-start.md). 핵심만:

- 양식 식별 실패 시 *자동 진입* (메인 파이프라인의 일급 분기)
- Claude가 기존 `form_definitions/*.md`를 *형식 예시*로 보고 신규 `form_NEW.md` 초안 + `form_types.json` 룰 후보를 같은 호출에서 자동 생성
- 같은 흐름에서 시험 추출까지 수행해 사용자에게 결과 + 정의 초안 함께 표시
- 코드 배포 없이 사용자가 검토·승인하면 다음부터 정상 분기로 자동 처리

## 6. SDK 선택

**Anthropic SDK + Prompt Caching + Message Batches API.**

| 후보 | 적합도 | 이유 |
|------|--------|------|
| **Anthropic SDK** ⭐ | 메인 파이프라인 정답 | 단일 호출, 결정적, 배치 친화 |
| Claude Agent SDK | 부분적 | cold-start의 일부에만 고려 가능. 메인엔 과함 |
| MCP | 미사용 | 도구 서버 표준 — 이번 과제엔 불필요 |
| Claude Code Skill | 미사용 | CLI 사용자가 슬래시로 부르는 기능. 백엔드 자동 처리와 무관. 운영자 ad-hoc 분석 도구로 *나중에* 보태도 됨 |

### 모델 라우팅

| 단계 | 모델 | 이유 |
|------|------|------|
| Phase 1 (페이지 MD 생성) | Haiku 4.5 | 페이지 단위 구조화 작업, 소규모, 병렬 다수 호출 |
| 양식 식별 | Haiku 4.5 | 가벼운 분류, confidence 반환만 |
| Phase 2 (추출·분류) | Sonnet 4.6 | 전체 MD 조합 분석, 조건 분류, 200K 컨텍스트 |
| 큰 PDF (>200K) 또는 cold-start | Opus 4.7 | 복잡한 추론, 1M 컨텍스트 |

### 비용 레버
- **Prompt Caching**: 시스템 프롬프트 + form 정의는 90% 할인. 같은 양식 PDF들을 묶어 처리하면 캐시 hit 극대화.
- **Message Batches API**: 월말 배치 처리에 50% 추가 할인.
- 가정 (Sonnet, batch, caching): 월 ~$200 수준. Opus만 쓰면 ~$1,000+.

## 7. 디렉토리 구조 (실제 코드 저장소)

이 워크스페이스(`Lecture/`)는 *기획용*. 실제 코드는 별도 저장소에서 작업.

권장 구조:
```
react_rebate_v2/
├── backend/
│   ├── api/routes/                   기존과 동일
│   ├── core/
│   │   ├── claude_extractor.py       Phase 1 + Phase 2
│   │   ├── deterministic_calc.py     Phase 3 계산·검증
│   │   ├── form_identifier.py        양식 분류 (Haiku)
│   │   └── form_learner.py           cold-start 자동 생성
│   └── main.py
├── frontend/                         기존과 동일 + cold-start 화면 추가
├── form_definitions/                 양식별 참조 MD (Claude 읽기용)
│   ├── form_01.md … form_05.md
│   ├── _drafts/                      cold-start 미승인 초안
│   └── _index.md                     양식 1줄 요약 (식별기 입력)
├── extracted/                        PDF별 추출 산출물 (디버그·재실행)
│   └── <doc_id>/page_001.md …
├── config/
│   ├── form_types.json               계산식·SAP 매핑 (Python 실행용)
│   └── llm.json                      모델·라우팅 설정
└── database/                         기존과 동일, RAG 테이블만 제거
```

## 8. 의사결정 기록

| 결정 | 채택 | 기각된 안 | 근거 |
|------|------|-----------|------|
| 분석 단위 | PDF 통째 long-context | 페이지 단위 RAG | 다중 페이지 합성, 신규 양식 대응 |
| Claude의 일 | 분류·식별만 | 산수까지 (Option A) | 70,000행 회계 재현성 |
| 룰 위치 | form 정의 MD + form_types.json (이중) | Python 코드 (현 시스템) | 코드 배포 없이 cold-start |
| Phase B SDK | Anthropic SDK + Batches API | Agent SDK / MCP | 자동 배치, 결정적, 비용 |
| Phase A 인터페이스 | Claude Code 채팅 직접 질의 | React 웹 UI | API 키 없는 현재 단계 |
| Phase 1 호출 방식 | 페이지별 병렬 호출 | PDF 통째 1콜 | 시스템 프롬프트 캐싱 + 재시도 단위 축소 |
| Phase 1 출력 형식 | 마크다운만 | JSON | JSON은 Phase 2 담당. Phase 1은 사람·Claude 가독성용 |
| Phase 1 테이블 소스 | TSV 기준 | 자유 텍스트 | 열 위치(컬럼 귀속) 보존 — 자유 텍스트는 위치 정보 없음 |
| Phase 1 헤더 키 언어 | 일본어 원문 | 한국어 번역 | form_XX.md 필드명과 언어 통일 |
| page_role·related_pages | Phase 2 담당 | Phase 1 담당 | 단일 페이지로 신뢰성 있는 판단 불가 |
| Phase 2 출력 구조 | {pages[], items[]} 래퍼 | items[]만 | Phase 3 교차검증에 page_role 필요 |
| Phase 2 items[] 필드 구조 | form 정의에 위임 | phase2-prompt에 하드코딩 | 그룹 식별자(invoice_no vs kanri_no 등)가 양식마다 달라 프롬프트를 고정하면 양식 추가 때마다 수정 필요 |
| Phase 2 form 정의 전달 위치 | 시스템 프롬프트 | 사용자 메시지 | 같은 양식 PDF 묶어 처리 시 캐시 hit 극대화 |
| Phase 1 모델 | Haiku 4.5 | Sonnet | 구조화 작업, 병렬 다수 호출, 비용 |
| 검색 인프라 | 없음 (long-context) | RAG / pgvector / BM25 | 컨텍스트 충분, 단순화 |
| Phase 3 매핑 방식 | Claude가 CSV 직접 읽고 판단 + 사용자 확인 | RAG 벡터 유사도 | CSV 총 6,000행 — long-context로 충분. RAG보다 판단 근거 투명 |
| 판매처코드 캐시 | 캐시 안 함 | ocr_retailer.csv에 함께 저장 | 동일 소매처도 발행 지점에 따라 달라짐 — 문서별 런타임 판단 |
| domae_retail_1 소매처명 컬럼 | 무시 (코드→코드 변환만 사용) | 이름 매칭에 활용 | 실제 내용이 판매처명(加藤産業...)이고 소매처명(ダイレックス...)이 아님 — 기존 시스템에서 몰랐던 사실 |
| タイプ 분류 방식 | 기본값 条件, 업무규칙 수령 후 양식별 로직 추가 예정 | 하드코딩 룰 / 코드 기반 매핑 | 분류 기준 미정. 사용자가 결과 검토 시 수정 가능 |
| タイプ 기본값 | 条件 | 판촉비 / 빈값 | xlsx에 매칭 없는 신규 조합은 条件으로 선표시 후 사용자 수정 — 자동화 범위 밖 케이스에 안전한 기본값 |
| Obsidian | 미사용 | (기존 vault 패턴) | Claude Code만 의존 |
| SAP Excel 생성 | Python 결정적 | LLM 변환 | 회계 재현성, 비용, 검증성 |
| 신규 양식 학습 | 메인 파이프라인 분기 | 곁가지 운영 도구 | 리팩의 존재 이유 |

## 9. Phase A — 채팅 기반 워크플로우 (현재)

API 키 확보 전 단계. Claude Code 채팅 인터페이스에서 직접 질의해 문서를 처리한다.

### 목표

| 목표 | 설명 |
|------|------|
| 목표1 (MD 생성) | OCR txt 읽기 → 구조화 MD 생성 → 기준 문서로 자체 검증 |
| 목표2 (cold-start) | 신규 양식 발견 시 대화로 form 정의 MD 초안 작성, 기준 문서도 함께 작성 |
| 목표3 (분석) | "이 문서 분석해줘" → 관련 MD + 기준 문서 조합 → 구조화 데이터 준비 |

### 필요 산출물

- `docs/phase1-prompt.md` ✅ — MD 생성 기준 + 검증 기준 통합
- `form_definitions/form_XX.md` ⏭ — 양식별 기준 정보 (목표3 분석의 핵심 참조)
- `docs/phase2-prompt.md` ⏭ — 항목 추출·분류 기준

### Phase B 전환 조건

- Anthropic API 키 확보
- form_definitions/ 기존 양식 5개 작성 완료
- Phase A로 PoC 검증 완료 (MD 품질 확인)

## 10. 미해결 / 다음 결정 필요

- **Phase A → Phase B 전환 시점** — API 키 확보 후 어떤 순서로 자동화 파이프라인으로 전환할지
- **검토 UI 폼 설계** — raw MD 편집 vs 폼 위에 폼 (cold-start.md §5 참조)
- **양식 식별 정확도** — Haiku로 충분한지, 양식 수가 늘어나면 인덱스 분할 필요?
- **Prompt 버전 관리** — 시스템 프롬프트 변경 시 어떻게 마이그레이션
- **재처리(re-extract) 정책** — 기존 데이터를 새 모델/프롬프트로 다시 돌릴지
- **이중 룰 표기 정합성** — form_definitions/form_XX.md(자연어)와 form_types.json(실행 룰)이 어긋나지 않도록 하는 메커니즘
- **공식 단일 진실 소스 (Phase B)** — form_types.json 공식을 프론트(JS)·백엔드(Python) 양쪽이 읽도록 설계. 사용자 条件2 입력 → 프론트 실시간 NET 계산, [저장] 클릭 → 백엔드 Phase 4 재실행 → DB 갱신. 동일 공식이 두 곳에 존재하므로 Phase B 구현 시 form_types.json을 단일 진실 소스로 통일 필요.

---

**Last Updated:** 2026-04-29 (タイプ 분류 설계 반영)
