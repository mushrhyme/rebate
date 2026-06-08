# Phase 3 매핑 설계

Phase 2 JSON의 OCR 추출값(거래처명·제품명)을 시스템 코드로 변환하고, 각 청구 항목의 タイプ를 분류한다.
코드 확정 후 Phase 4에서 NET 계산·Excel 출력을 수행한다.

---

## 왜 어려운가

OCR이 뽑는 명칭과 CSV에 등록된 명칭은 거의 exact match가 일어나지 않는다.

```
OCR 원문:  "ダイレックス(株) (32423)"
CSV 등록:  "ダイレックス株式会社"
```

Claude가 유사도로 후보를 고를 수 있지만, **유사도만으로는 CSV 데이터 자체가 틀렸는지를 증명할 수 없다.**
신뢰의 근거는 Claude의 추론이 아니라 **사람이 확인한 캐시가 쌓이는 것**이다.

---

## 설계 원칙

| 상황 | 처리 |
|------|------|
| **cache hit** | 사용자가 이미 확인한 값 → 무조건 자동 확정 |
| **cache miss** | Claude가 후보 제시 → 사용자 확인 → 확정 후 캐시 저장 |

```
첫 번째 문서: cache miss → Claude best-guess → 사용자 확인 → 캐시 저장
두 번째 이후: cache hit → 자동 확정
```

**unique 단위 처리**: item마다 묻지 않는다. 문서 내 unique 거래처명·제품명만 추출해서 한 번에 처리하고, 확정 후 전체 items에 일괄 적용한다.

---

## 처리 흐름

```
Phase 2 JSON items[]
  ↓
unique 거래처명 목록 추출        unique 제품명 목록 추출
  ↓ (Claude 소매처)                 ↓ (Claude 제품)       ← 동시 실행
① 소매처코드 매핑                ③ 제품코드 매핑
  ↓ 확정 후
② 판매처코드 매핑                ④ タイプ 분류
  ↓
전체 items에 코드 일괄 적용 → phase3_output.json 저장
```

---

## 1. 소매처코드 매핑

### form_01 (FINET)

OCR 거래처명에 도매소매처코드가 괄호로 포함됨: `ダイレックス(株) (32423)`

```
Step 1: ocr_retailer.csv cache hit → 소매처코드 확정

Step 2: OCR에서 괄호 코드 추출 (예: 32423)
        → domae_retail_1.csv 코드 직접 매칭
        → hit: 소매처코드 확정

Step 3 (miss 시): OCR 이름(괄호 제거) → Claude가 retail_user.csv 후보 + OCR 원문 비교 추론
        → 고신뢰도: 자동 확정
        → 저신뢰도 또는 후보 다수: 후보 목록 + OCR 원문을 사용자 확인 의뢰
        → 확정 시 ocr_retailer.csv 저장
```

### form_02~05

OCR에 코드 없음. Step 2 생략.

```
Step 1: ocr_retailer.csv cache hit → 확정
Step 2: OCR 이름 → Claude가 retail_user.csv·domae_retail_2.csv 후보 + OCR 원문 비교 추론
        → 고신뢰도: 자동 확정
        → 저신뢰도: 후보 목록을 사용자 확인 의뢰 → 확정 시 ocr_retailer.csv 저장
```

---

## 2. 판매처코드 매핑

소매처코드 확정 후 실행. 캐시 키: `(form_id, issuer_fingerprint, 소매처코드)`.

같은 소매처라도 발행처가 다르면 담당 판매처가 다를 수 있으므로 3-tuple로 구분한다.
`issuer_fingerprint`는 `form_XX.md`의 `fingerprint_fields`(예: `name` 또는 `name, tel`)를
`|` 구분자로 연결한 문자열. 양식마다 cover에서 추출 가능한 필드가 다르므로 form별로 정의한다.

```
Step 0: ocr_dist.csv에서 (form_id, issuer_fingerprint, 소매처코드) 조회
        → hit: 판매처코드 확정, 끝

Step 1: retail_user.csv에서 소매처코드로 판매처 후보 조회
        → 1건: 판매처코드 자동 확정
        → 2건 이상(1:N): form_XX.md 판매처 결정 규칙 적용 (아래 참조)
        → 0건: NOT_FOUND
```

### 1:N 판매처 결정 규칙

후보가 2건 이상일 때, 아래 순서로 판단한다.

**우선순위 1 — items 그룹 필드 참조** (양식에 그룹 식별 필드가 있는 경우)

Phase 2가 추출한 items[]에서 해당 소매처의 그룹 식별 필드 값을 수집해 Claude에 전달한다.
그룹 필드는 발행처의 담당 부문·지역을 직접 나타내므로 판매처를 특정하는 가장 신뢰할 수 있는 근거다.
**어떤 필드를 쓸지, 값과 판매처의 대응 규칙은 각 `form_XX.md`의 "판매처 결정 규칙" 섹션에 정의한다.**

**우선순위 2 — issuer fingerprint** (그룹 필드가 없는 양식, 또는 그룹 필드로 구분 불가 시)

cover 페이지의 issuer(발행처명·전화번호)를 판매처 후보 명칭과 대조한다.
`form_XX.md`의 `fingerprint_fields`에 정의된 필드를 사용한다.

**우선순위 3 — NEEDS_CONFIRMATION**

위 두 방법으로 특정 불가 시: 후보 목록과 cover issuer를 사용자에게 표시하고 선택을 요청한다.
확정 후 ocr_dist.csv에 저장한다.

**현재 구현 상태**: Python이 retail_user.csv에서 판매처 후보를 미리 조회한다.
1:1 케이스는 Python이 자동 확정 + 캐시 저장. 1:N 케이스는 후보 목록 + 그룹 필드 값을 Claude에 전달하고
Claude가 그룹 필드 → issuer → NEEDS_CONFIRMATION 순으로 판단한다.

---

## 3. 제품코드 매핑

```
Step 1: ocr_product.csv cache hit → 제품코드 확정

Step 2: OCR 제품명을 이름·용량으로 분리
        → unit_price.csv 유사도 검색 → 최고 후보 반환

Step 3: 저신뢰도 → Claude가 후보 목록 + OCR 원문 제시 → 사용자 확인
        → 확정 시 ocr_product.csv 저장
```

---

## 4. タイプ 분류

タイプ분류는 **Phase 2 Claude**가 담당한다. Phase 3은 Phase 2 출력의 `item_type` 필드를 그대로 전달할 뿐이다.

Phase 2 시스템 프롬프트에 `form_XX.md` 전체가 포함되어 있으므로, Claude가 양식별 タイプ분류 규칙을 직접 읽고 판단한다. 판단 불가 또는 규칙 미확정 항목은 기본값 `条件`으로 출력한다.

### タイプ 종류

| タイプ | 설명 |
|--------|------|
| 条件 | **기본값** — 아래 어느 것도 해당하지 않을 때 |
| 販促費8% | 판촉 항목, 軽減税率 8% |
| 販促費10% | 판촉 항목, 標準税率 10% |
| CF8% | センターフィ 등 물류·서비스 항목, 8% |
| CF10% | センターフィ 등 물류·서비스 항목, 10% |
| 非課税 | 消費税計上 행 (得意先コード=0000000) — 현업 확인 필요 |
| 消費税8% | 소비세 8% 세액 항목 — 현업 확인 필요 |
| 消費税10% | 소비세 10% 세액 항목 — 현업 확인 필요 |
| ロットアウト | (현업 확인 필요) |

---

## CSV 파일 역할

### 소매처·판매처

| 파일 | 키 | 값 | 용도 | 비고 |
|------|----|----|------|------|
| `domae_retail_1.csv` | 도매소매처코드 | 소매처코드 | form_01 코드 직접 매칭 | 소매처명 컬럼은 실제로 판매처명 — 이름 매칭 불가 |
| `domae_retail_2.csv` | 도매소매처명 | 소매처코드 | form_02~05 이름 매칭 | 411행, 제한적 커버리지 |
| `retail_user.csv` | 소매처명 / 소매처코드 | 소매처코드 + 판매처코드 + 판매처명 | 이름 기반 소매처 검색 + 판매처 후보 목록 | 1:N 구조 (dist_retail 통합 후). 1,783행 |

### 제품

| 파일 | 키 | 값 | 용도 |
|------|----|----|------|
| `unit_price.csv` | 제품명+용량 | 제품코드, 시키리, 본부장 | 제품코드 매핑 + 단가 조회 (113행) |

### 캐시 (사람이 확인한 정답만 쌓임)

| 파일 | 키 | 값 | 특징 |
|------|----|----|------|
| `ocr_retailer.csv` | OCR 거래처명 원문 | 소매처코드 | 사용자 확인 후 추가. 틀린 행은 삭제 후 재매핑 |
| `ocr_product.csv` | OCR 제품명 원문 | 제품코드 | 사용자 확인 후 추가. 틀린 행은 삭제 후 재매핑 |
| `ocr_dist.csv` | (form_id, issuer_fingerprint, 소매처코드) | 판매처코드 | 사용자 확인 후 추가. fingerprint는 form별 fingerprint_fields 조합 |

```
ocr_retailer.csv 예시:
ocr_name,소매처코드
ダイレックス(株) (32423),6003851
イオン琉球(株) (32943),6003685

ocr_product.csv 예시:
ocr_name,제품코드
農心 辛ラーメン 3P,101000491
農心 辛ラーメンミニカップ 49G,101003042
```

---

## 미해결

- **消費税·ロットアウト 출현 조건**: 현업 확인 필요. form_02~05에서 나올 가능성.
- **form_02~05 매핑 로직**: 해당 양식 form_XX.md 작성 후 검증 필요.
- ~~**판매처 1:N 자동 판단 규칙**~~ → jisho 우선 → issuer fallback → NEEDS_CONFIRMATION 순서로 결정. §2 참조.
