# 분석 파이프라인 전체 흐름

> 작성: 2026-06-01  
> 대상: 파이프라인이 어떻게 동작하는지 이해하고 싶은 사람

---

## 한 줄 요약

```
PDF 업로드 → OCR → Page MD 변환(×페이지) → 항목 추출 → 코드 매핑 → NET 계산
```

---

## 전체 흐름도

```
[사용자: PDF 업로드]
        │
        ▼
┌───────────────────┐
│  OCR (Azure)      │  PDF → page_NNN.ocr.txt (페이지별 텍스트 + 테이블 구조)
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│  양식 식별        │  page_001~003.ocr.txt의 키워드를 form_XX.md 패턴과 비교
│  (결정적 코드)    │  → form_01 / form_04 / unknown
└────────┬──────────┘
         │ unknown → 에러 종료
         ▼
┌───────────────────┐
│  Phase 1          │  OCR txt → page MD  (Claude Haiku × 페이지 수)
│  (Claude Haiku)   │  비정형 OCR 텍스트를 구조화된 마크다운 표로 변환
│                   │  출력: page_NNN.md (page_type_hint 포함)
└────────┬──────────┘
         │
         ▼
┌───────────────────────────────────────────┐
│  Phase 2  — 항목 추출                     │
│  (Claude Sonnet)                          │
│                                           │
│  ① 번들 감지: cover가 2개 이상?           │
│     YES → 번들별로 분리 호출              │
│     NO  → 아래 청크 판단으로              │
│                                           │
│  ② 청크 판단: detail 페이지 > 4개?        │
│     YES → 2페이지씩 청크 분할 호출        │
│     NO  → 전 페이지 단일 호출             │
│                                           │
│  입력: page MD들 + form_XX.md 정의        │
│  출력: {pages[], items[]} JSON            │
└────────┬──────────────────────────────────┘
         │
         │ 청크가 여러 개라면 → _merge_phase2_results()
         │   (내용 해시 dedup 포함)
         ▼
┌───────────────────┐
│  Phase 2 Verify   │  page MD를 결정적으로 파싱해 管理No計를 추출
│  (결정적 코드     │  items[]의 金額 합산과 불일치 시 →
│   + Claude Haiku) │  해당 管理No 블록만 Haiku에 재요청
│                   │  복구 후 content-hash dedup 재실행
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│  Phase 3          │  소매처 / 판매처 / 제품 코드 매핑
│  (Python캐시      │
│   + Claude Sonnet)│  ① Python: ocr_retailer.csv, ocr_product.csv 캐시 히트
│                   │  ② 캐시 미스 → Claude Sonnet (retailer/product 병렬 2호출)
│                   │  ③ 미확정 항목 → DB에 pending 등록 → 사용자 확인 대기
└────────┬──────────┘
         │ pending 있음 → 상태 "pending", 사용자가 매핑 확인 후 resume
         │ 전부 자동확정 → 바로 Phase 4
         ▼
┌───────────────────┐
│  Phase 4          │  NET 계산 + 교차검증
│  (Python 결정적   │
│   + Claude Haiku) │  ① phase4_calc.py (subprocess, LLM 없음)
│                   │     시키리 × 수량 → NET = 시키리 - 미수조건 수식
│                   │  ② 교차검증: Python calc xv 있으면 그걸 사용
│                   │              없으면 Claude Haiku가 커버 합계와 비교
└────────┬──────────┘
         │
         ▼
   [결과 화면 표시]
   + Google Drive 동기화 (옵션)
```

---

## 각 Phase 상세

### OCR (Azure Computer Vision)
- **입력**: PDF 파일
- **출력**: `page_NNN.ocr.txt` — 각 페이지의 OCR 텍스트 + 테이블 구조(탭 구분)
- **특징**: 텍스트 레이아웃 정보(bounding box) 포함. 일본어 OCR 정확도는 높지만 표 셀 분열 아티팩트 발생 가능

---

### 양식 식별 (결정적 코드)
- `form_XX.md`의 `## 식별 패턴` 섹션에서 backtick 패턴을 추출
- 첫 3페이지 OCR 텍스트에 **모든 패턴이 포함**되면 해당 양식으로 확정
- 실패 시 `unknown_form` 에러로 종료

---

### Phase 1 (Claude Haiku)
- **입력**: `page_NNN.ocr.txt`
- **출력**: `page_NNN.md` — 마크다운 표 + `page_type_hint: cover/detail/summary`
- **특징**:
  - 첫 페이지 먼저 순차 처리 → 나머지 페이지 병렬 처리 (Prompt Caching 최적화)
  - Azure OCR의 셀 분열(管理No가 "管", "理 No"로 쪼개지는 등)을 어느 정도 복원
  - Haiku 사용 → 빠르고 저렴

---

### Phase 2 (Claude Sonnet)

항목 추출의 핵심. 가장 복잡한 단계.

#### 호출 구조 결정

```
detail 페이지 수 ≤ 4   → 전 페이지 단일 Sonnet 호출
detail 페이지 수 > 4   → 2페이지씩 청크 분할

  청크 구성: cover 전체 + 가장 가까운 summary ≤3개 + detail 2페이지
  예) 9페이지 문서 (cover p1, detail p2~p8, summary p9):
    청크0: [p1(cover), p9(summary), p2, p3]
    청크1: [p1(cover), p9(summary), p4, p5]
    청크2: [p1(cover), p9(summary), p6, p7]
    청크3: [p1(cover), p9(summary), p8]
```

#### 입력
```
시스템: docs/phase2-prompt.md + form_XX.md 정의  (캐시 대상 — 같은 양식이면 재사용)
사용자: === Page N === 구분자로 이어붙인 page MD들
```

#### 출력 (JSON)
```json
{
  "pages": [{"page": 1, "role": "cover", "totals": {...}}, ...],
  "items": [
    {
      "jisho": "R営業九州",
      "kanri_no": "1565543",
      "customer": "(株)ファミリーマート",
      "product": "農心 辛ラーメン",
      "columns": {"数量": 100, "金額": 50000},
      "item_type": "条件",
      "source_pages": [5]
    }, ...
  ]
}
```

#### 청크 병합 (`_merge_phase2_results`)
- 여러 청크 결과를 하나로 합침
- pages[]: 페이지 번호 기준 dedup
- items[]: **invoice_no 또는 내용 해시(customer+product+columns) 기준 dedup**

---

### Phase 2 Verify (결정적 코드 + Claude Haiku)

Phase 2 출력의 품질 검증 + 자동 복구.

#### 동작 원리
1. `page_NNN.md` 파일을 순서대로 읽으며 `管理No計: 6,486` 형식의 소계 행을 파싱
2. `items[]`에서 동일 `kanri_no`의 `金額` 합산
3. **불일치 발견 시**: 해당 管理No의 블록 텍스트만 Haiku에 전달해 누락 항목 재추출
4. 복구 항목 삽입 후 **content-hash dedup 재실행** (가비지 kanri_no 항목 제거)

#### 한계
- `管理No計` 형식이 없는 양식(form_01 등)은 검증 불가 → 스킵
- Haiku가 복구 실패하면 누락이 그대로 잔존 (`복구 실패 (누락 N 잔존)` 로그)

---

### Phase 3 (Python 캐시 + Claude Sonnet)

OCR 명칭 → 코드 매핑.

#### 처리 순서
```
items[].customer  →  ① ocr_retailer.csv 캐시 조회 (normalize 후 비교)
                     ② 미스 → Claude에게 domae_retail_2.csv 제공해 추론
                     ③ 확정 → ocr_retailer.csv에 저장 (다음부터 캐시 히트)
                     ④ 불확실 → pending 등록

items[].product   →  ① ocr_product.csv 캐시 조회
                     ② 미스 → Claude에게 unit_price.csv 제공해 추론
                     ③ 확정 → ocr_product.csv에 저장
                     ④ 불확실 → pending 등록

판매처(受注先)    →  ① ocr_dist.csv 캐시 조회 (form_id + issuer + 소매처코드 기준)
                     ② 미스 → retail_user.csv 기본값 조회
                     ③ 불확실 → pending 등록
```

#### pending 상태
- 미확정 매핑이 1건이라도 있으면 → `status: pending`
- 사용자가 결과 화면에서 매핑 확인 클릭 → `resume_phase4()` 호출 → Phase 4 실행

---

### Phase 4 (Python 결정적 코드 + Claude Haiku)

#### NET 계산 (`phase4_calc.py`, LLM 없음)
```
NET = 仕切価格 - 未収条件  (수식은 config/form_types.json에서 읽음)
未収金額 = NET × 数量
```
- 모든 수식 분기는 `config/form_types.json`에 JSON으로 정의
- Python subprocess로 실행 → 재현성 100%

#### 교차검증
- Python calc가 xv를 생성했으면 그걸 그대로 사용 (Claude 호출 안 함)
- Python calc xv가 비어있을 때만 Claude Haiku 호출
- 커버 페이지 합계 vs Phase 4 집계 비교 → 차이가 있으면 `xv[]`에 기록
- 결과 화면 우측 하단 "교차검증" 패널에 표시

---

## 누락 항목이 발생하는 지점 (리스크 맵)

| 단계 | 리스크 | 원인 |
|------|--------|------|
| **OCR** | 셀 분열 | 표 구조가 복잡할 때 셀이 쪼개져서 Phase 1이 복원 못 함 |
| **Phase 1** | page_type_hint 오분류 | cover를 detail로 잘못 분류 → 청크 구성 오류 |
| **Phase 2** | 추출 누락 | 청크 내 管理No 블록이 많을수록 누락 확률 상승 |
| **Phase 2** | 청크 경계 분리 | 管理No 헤더와 計 행이 서로 다른 청크에 들어가면 추출 불완전 |
| **Phase 2 Verify** | 복구 실패 | Haiku가 누락 항목을 찾지 못하거나 JSON 형식 오류 |
| **Phase 3** | 매핑 미확정 | 새 거래처/제품명은 캐시 미스 → pending → 수동 확인 필요 |

---

## 현재 설정값 (`.env` + 코드 기본값)

| 환경변수 | 기본값 | 설명 |
|---------|--------|------|
| `PHASE2_CHUNK_THRESHOLD` | 4 | detail 페이지가 이 수를 초과하면 청크 분할 |
| `PHASE2_CHUNK_SIZE` | 2 | 청크당 detail 페이지 수 |
| `PHASE2_OVERLAP` | 0 | 청크 간 overlap 페이지 수 (0 = 없음) |
| `PHASE2_MAX_SUMMARY` | 3 | 청크당 포함할 summary 페이지 최대 수 |
| `MAX_CONCURRENT_ANALYSES` | 5 | 동시 처리 문서 수 상한 |
| `MAX_CONCURRENT_PHASE1_PAGES` | 5 | Phase 1 페이지 병렬 처리 상한 |

---

## 사용 모델 요약

| Phase | 모델 | 용도 |
|-------|------|------|
| Phase 1 | Claude Haiku 4.5 | OCR txt → page MD (빠르고 저렴) |
| Phase 2 | Claude Sonnet 4.6 | page MD → items[] (핵심 추출, 고성능) |
| Phase 2 Verify | Claude Haiku 4.5 | 누락 행 핀포인트 복구 |
| Phase 3 | Claude Sonnet 4.6 | 캐시 미스 매핑 추론 |
| Phase 4 (교차검증) | Claude Haiku 4.5 | cover 합계 vs 집계 비교 (Python xv 없을 때만) |

---

## 파일 경로 정리

```
samples/<doc_id>.pdf                     ← 원본 PDF
samples/<doc_id>_pages/page_NNN.ocr.txt  ← OCR 결과 (Azure)
extracted/<doc_id>/page_NNN.md           ← Phase 1 출력
extracted/<doc_id>/phase2_output.json    ← Phase 2 출력 (verify 후 갱신됨)
extracted/<doc_id>/phase3_output.json    ← Phase 3 출력 (매핑 확정 후 갱신됨)
extracted/<doc_id>/phase4_output.json    ← Phase 4 출력 (최종 결과)
```
