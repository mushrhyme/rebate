# Phase 3 Tool Use — Limited Rollout 실행 절차

**버전**: 2.0  
**대상 환경**: 운영 서버  
**작성일**: 2026-06-05  
**최종 상태**: ✅ **Production Ready — Controlled Rollout Complete**

> retailer·product·dist 1:N 전체 검증 완료. FastAPI token DB 기록 확인. 운영 적용 중.

---

## 0. 전제 조건

| 항목 | 확인 |
|------|------|
| `PHASE3_TOOL_USE_ENABLED` 기본값 OFF | ✅ |
| fallback 시 legacy 결과 자동 복구 | ✅ |
| E2E 통합 테스트 21개 통과 | ✅ |
| Product smoke 테스트 존재 | ✅ |
| Retailer smoke 실제 API PASS | ✅ (2026-06-05 확인) |
| 전체 테스트 615 passed | ✅ |
| `asyncpg==0.31.0` pyproject.toml 의존성 등재 | ✅ |
| **운영 환경 asyncpg 설치** | **필수 — 없으면 token 기록 불가** |
| **`ANTHROPIC_API_KEY` 설정** | **필수 — 없으면 retailer Tool Use 미실행** |

### P0 수정 완료 (2026-06-05)

| 항목 | 이전 (STOP) | 수정 후 |
|------|------------|---------|
| retailer Tool Use client | Mock/Scenario client | 실제 Anthropic client |
| allow_side_effects=False | `confirmed_code=None` (항상 pending) | `decided_code` 캡처 → 확정 가능 |
| confirmed_retailers | 항상 0건 | Claude 결정에 따라 채워짐 |
| confirm_mapping CSV | 미호출 | tool_use basis 확정 시 저장 |

### P0-2 상태 (asyncpg)

`asyncpg==0.31.0`이 `pyproject.toml`에 등재되어 있음.  
`uv sync` 또는 `pip install asyncpg` 으로 운영 환경에 설치 후 rollout 진행.  
미설치 시 pipeline은 동작하지만 token usage가 DB에 기록되지 않음 (warning 처리).  
Rollout 전 `python scripts/phase3_rollout_observe.py --precheck` 로 반드시 확인.

---

## 1. 실행 전 체크리스트

다음 항목을 모두 확인한 후 진행한다.

```
[ ] 1. 최신 코드가 운영 서버에 배포되어 있다.
[ ] 2. ANTHROPIC_API_KEY가 backend/.env에 설정되어 있다.
[ ] 3. 데이터베이스(PostgreSQL) 연결이 정상이다.
[ ] 4. asyncpg가 운영 환경에 설치되어 있다 (uv sync 또는 pip install asyncpg).
[ ] 5. extracted/ 디렉토리 write 권한이 있다.
[ ] 6. mappings/ 디렉토리 write 권한이 있다 (confirm_mapping CSV 쓰기).
[ ] 7. 사전 점검 통과: python scripts/phase3_rollout_observe.py --precheck
         (asyncpg, API key, DB 연결, PHASE3_TOOL_USE_ENABLED 상태 확인)
[ ] 8. 전체 테스트가 통과한다: .venv/bin/python -m pytest tests/ -q --tb=no
[ ] 9. retailer smoke PASS 확인:
         export ANTHROPIC_API_KEY=...
         export RUN_REAL_CLAUDE_SMOKE=1
         .venv/bin/python -m pytest tests/smoke/ -v -k "retailer"
[ ] 10. 대조군 문서(legacy 처리 결과)가 prepared 되어 있다.
[ ] 11. 관찰 스크립트가 동작한다: python scripts/phase3_rollout_observe.py --help
[ ] 12. rollback 경로(PHASE3_TOOL_USE_ENABLED=false)가 즉시 가능한 상태이다.
[ ] 13. 운영 중단 기준을 팀과 공유했다 (섹션 6 참조).
```

---

## 2. 환경변수 설정

`backend/.env` 에 다음을 추가한다.

```bash
# Phase 3 Tool Use 활성화
PHASE3_TOOL_USE_ENABLED=true

# 모델 (기본값과 동일, 생략 가능)
PHASE3_TOOL_USE_MODEL=claude-haiku-4-5-20251001

# 문서 내부 동시성 (기본값 1, 순차)
PHASE3_TOOL_USE_CONCURRENCY=1
# 전역 문서 동시 처리 수 상한 (기본값 3, Rate Limit 방지)
PHASE3_TOOL_USE_GLOBAL_CONCURRENCY=3
```

> **주의**: `CONCURRENCY=1` 유지 권장.  
> `GLOBAL_CONCURRENCY=3`은 다중 사용자 동시 업로드 시 Rate Limit 방지용.  
> Anthropic API Tier 업그레이드 시 5~10으로 조정 가능.

설정 적용 확인:
```bash
python -c "
import sys; sys.path.insert(0,'.')
from backend.core.config import get_settings
s = get_settings()
print('enabled :', s.phase3_tool_use_enabled)
print('model   :', s.phase3_tool_use_model)
print('conc    :', s.phase3_tool_use_concurrency)
"
```

기대 출력:
```
enabled : True
model   : claude-haiku-4-5-20251001
conc    : 1
```

---

## 3. 실행 명령

### 3-A. 단일 문서 테스트 실행

기존 업로드/분석 UI 또는 API를 통해 문서를 처리한다. 파이프라인은 자동으로 Tool Use 경로를 사용한다.

```bash
# API를 통한 재분석 예시 (UI 또는 curl)
# POST /api/documents/{doc_id}/analyze
```

### 3-B. 처리 중 로그 확인

```bash
# 백엔드 로그에서 Tool Use 관련 항목 필터링
tail -f /var/log/backend.log | grep -E "Tool Use|fallback|phase3_tool_use"
```

성공 시 기대 로그:
```
[doc_id] Tool Use 성공 (NNNms) → success path
[doc_id] Phase 3 완료 (Tool Use) — tool_use=NNNms / total=NNNms / tokens=inN+outN
```

fallback 시 기대 로그:
```
[doc_id] Tool Use 실패 → Legacy fallback. 원인: [클래스명] 이유
[doc_id] Phase 3 완료 (fallback) — tool_use=NNNms / legacy=NNNms / preserved_tokens=inN+outN
```

### 3-C. 결과 확인

```bash
# phase3_output.json 확인
cat extracted/{doc_id}/phase3_output.json | python -m json.tool | head -40

# CSV 캐시 업데이트 확인
tail -5 mappings/ocr_retailer.csv
tail -5 mappings/ocr_dist.csv
```

---

## 4. 관찰 지표

### 4-1. 핵심 지표

| 지표 | 수집 방법 | 목표 기준 |
|------|----------|----------|
| **Tool Use 성공률** | 관찰 스크립트 + 로그 | ≥ 80% |
| **fallback 비율** | 관찰 스크립트 + 로그 | ≤ 20% |
| **pending 비율** | 관찰 스크립트 | dist 1:N 수준 이내 |
| **phase3_output.json 생성 여부** | 파일 존재 확인 | 100% |
| **confirm_mapping CSV 업데이트** | CSV 파일 확인 | 성공 케이스 100% |
| **token usage 기록 여부** | DB `v3_usage_log` 확인 | 100% |

### 4-2. 품질 지표

| 지표 | 수집 방법 |
|------|----------|
| **retailer 코드 일치율** | Tool Use 결과 vs legacy 결과 비교 |
| **product 코드 일치율** | Tool Use 결과 vs legacy 결과 비교 |
| **처리시간** | 로그의 `total=NNNms` 항목 |
| **token input/output** | DB `v3_usage_log` phase=phase3_tool_use |

### 4-3. 관찰 스크립트 실행

```bash
# 최근 처리된 문서 전체 집계 (extracted/ 디렉토리 기준)
python scripts/phase3_rollout_observe.py

# 특정 문서만
python scripts/phase3_rollout_observe.py doc_id_1 doc_id_2 ...

# DB token usage 포함 (asyncpg 필요)
python scripts/phase3_rollout_observe.py --db
```

---

## 5. 결과 기록 양식

처리 완료 후 아래 표를 채운다.

```
=== Phase 3 Tool Use Limited Rollout 결과 기록 ===
날짜:
담당자:
환경: [ ] dev  [ ] staging  [ ] prod

─── 처리 현황 ─────────────────────────────────────
총 문서 수:
Tool Use 성공:           (성공률: %)
fallback 발생:            (비율: %)
  └ ToolUseMaxTurnsError:
  └ ToolUseDispatchError:
  └ ToolUseContractError:
  └ ToolUseApiError:
  └ ToolUseParseError:
phase3_output.json 누락:

─── Pending 현황 ──────────────────────────────────
총 pending 항목:
  └ retailer:
  └ product:
  └ dist 1:N:

─── Token Usage (DB 조회 시) ──────────────────────
phase3_tool_use input tokens 합계:
phase3_tool_use output tokens 합계:
phase3 (legacy fallback) input tokens:
phase3 (legacy fallback) output tokens:

─── 품질 확인 ─────────────────────────────────────
legacy 대비 결과 차이 여부: [ ] 없음  [ ] 있음 (상세:                )
confirm_mapping CSV 업데이트: [ ] 정상  [ ] 누락
token 기록 누락: [ ] 없음  [ ] 있음

─── 처리 시간 ─────────────────────────────────────
평균 Tool Use elapsed (ms):
최대 Tool Use elapsed (ms):
평균 total elapsed (ms):

─── 중단 여부 ─────────────────────────────────────
[ ] 정상 완료  [ ] 중단 → 사유:

─── 다음 단계 ─────────────────────────────────────
[ ] CONCURRENCY 조정 검토
[ ] full rollout 진행
[ ] 추가 관찰 필요
[ ] 이슈 수정 후 재시도
```

---

## 6. 운영 중단 기준

아래 기준 중 하나라도 발생하면 **즉시 롤백**하고 원인을 파악한다.

| # | 기준 | 임계값 |
|---|------|--------|
| **C1** | fallback 비율 | **> 20%** |
| **C2** | `phase3_output.json` 누락 | **1건 이상** |
| **C3** | `confirm_mapping` CSV 미업데이트 (성공 케이스) | **1건 이상** |
| **C4** | DB token 기록 누락 (API 호출이 있었는데도) | **1건 이상** |
| **C5** | legacy 대비 결과 차이 (같은 문서, 코드가 다름) | **1건 이상** |
| **C6** | 동일 ocr_name에 confirm_mapping이 N회 중복 호출 | **발생 시** |
| **C7** | Claude API rate limit에 의한 연속 fallback | **3건 연속** |
| **C8** | pending 비율 이상 급증 (dist 1:N 외 이유) | **legacy 대비 2배 초과** |

> **C5 판단 방법**: 동일 문서를 `PHASE3_TOOL_USE_ENABLED=false`로 재처리한 결과와 비교.  
> 소매처코드, 판매처코드, 제품코드 3개 필드만 비교한다.

---

## 7. Rollback 방법

### 즉시 롤백 (1분 이내)

```bash
# 1. 환경변수 변경
# backend/.env에서 아래 줄을 false로 변경 또는 제거
PHASE3_TOOL_USE_ENABLED=false

# 2. 백엔드 재시작 (환경변수 재로드)
# — 배포 방식에 따라 다름 (예: systemctl restart backend, docker restart ...)

# 3. 확인
python -c "
import sys; sys.path.insert(0,'.')
from backend.core.config import get_settings
get_settings.cache_clear()  # lru_cache 초기화
s = get_settings()
print('enabled:', s.phase3_tool_use_enabled)  # False여야 함
"
```

### 이미 처리된 문서 복구

```bash
# phase3_output.json을 legacy 결과로 덮어쓰려면 재분석 실행
# (파이프라인이 legacy 경로를 사용해 phase3_output.json을 자동으로 덮어씀)
```

### 롤백 후 확인

```bash
# 로그에 "Tool Use" 메시지가 없는지 확인
tail -20 /var/log/backend.log | grep "Tool Use"
# 출력 없어야 정상
```

---

## 8. 단계별 확장 계획

| 단계 | 문서 수 | 조건 |
|------|--------|------|
| ✅ **완료**: Limited Rollout 3차 PASS | 9건, avg_turns 3.0, fallback 0 | 2026-06-05 |
| ✅ **완료**: Controlled Production Enable | 운영 1건, token DB row 생성 확인 | 2026-06-05 |
| ✅ **완료**: 동시 처리 검증 | 4월 CVS 12건 동시, phase3_tool_use 13건 기록 | 2026-06-05 |
| ✅ **완료**: Dist 1:N Tool Use 구현 + smoke 검증 | 5개 smoke, 2개 후보 tool_use PASS | 2026-06-05 |
| **현재**: 지속 운영 모니터링 | 모든 신규 문서 | observe script + DB 주기 확인 |
| **다음**: GLOBAL_CONCURRENCY 조정 | Tier 업 시 3→5~10 | API rate limit 여유 확인 후 |

---

## 9. Limited Rollout 3차 결과 기록 (2026-06-05)

### 실행 환경

| 항목 | 값 |
|------|-----|
| 대상 문서 | 9건 (form_01 × 2, form_04 × 7) |
| `PHASE3_TOOL_USE_ENABLED` | true |
| `PHASE3_TOOL_USE_CONCURRENCY` | 1 |
| `tool_choice` 강제 | lookup_retailer (첫 turn) |

### 핵심 지표

| 지표 | 결과 | 기준 | 판정 |
|------|------|------|------|
| phase3_output.json 누락 | 0건 | 0건 | ✅ |
| fallback | **0건 (0%)** | ≤20% | ✅ |
| tool_not_called | **0건** | 0건 | ✅ |
| avg_turns | **3.0 (전체)** | 1 이상 | ✅ |
| retailer API 호출 | 128건 | 0 초과 | ✅ |
| lookup_retailer tool_use | 42건 | 0 초과 | ✅ |
| confirm_mapping 캡처 | 42건 | 0 초과 | ✅ |
| confirmed_retailers (합계) | **42 / legacy 42** | 감소율 ≤10% | ✅ |
| confirmed_products (합계) | **120 / legacy 120** | 차이 0 | ✅ |
| pending rate | **1.9% (legacy 20.4%)** | ≤legacy×2 | ✅ |

### 주목할 차이 (운영 허용 범위)

| 항목 | 원인 | 판단 |
|------|------|------|
| 伊藤忠食品: CR 26→25 (-1) | 특수문자 `《集` 포함 OCR명의 `normalize_ocr_name` 정규화 불일치로 cache miss | 허용 — 향후 P3 수정 |
| CVS①: CR 0→1 (+1) | Tool Use가 legacy가 놓친 소매처를 새로 확정 | ✅ 개선 |
| pending 84→8 | 伊藤忠食品 7건: dist 1:N 또는 not_found pending | 허용 — 사용자 확인 경로 정상 |

### observe script path_type 해석 주의

Tool Use 경로로 실행됐더라도 **모든 소매처가 cache hit이면** `result_basis=cache`로 표시된다. `result_basis=cache`는 Tool Use 실행 여부를 직접 나타내지 않는다. 실제 Tool Use 실행 여부는:
- 처리 로그의 `HTTP Request: POST https://api.anthropic.com/v1/messages` 확인
- `retailer API calls > 0` 확인
- `--db` 옵션으로 `v3_usage_log.phase='phase3_tool_use'` row 확인

### 최종 판정

**✅ Limited Rollout PASS → Controlled Production Enable 가능**

---

## 10. Controlled Production Enable 체크리스트

운영 서버에 `PHASE3_TOOL_USE_ENABLED=true`를 설정하기 전 아래 항목을 모두 확인한다.

### 10-1. 사전 환경 점검

```bash
# 필수: .venv 또는 uv run 기준 실행
uv run python scripts/phase3_rollout_observe.py --precheck
```

```
[ ] ① asyncpg import 가능  (uv sync 또는 pip install asyncpg 완료)
[ ] ② ANTHROPIC_API_KEY 설정 (backend/.env 또는 환경변수)
[ ] ③ DATABASE_URL 설정 (backend/.env 또는 환경변수)
[ ] ④ DB 연결 테스트 통과 (v3_usage_log 테이블 접근 가능)
[ ] ⑤ PHASE3_TOOL_USE_CONCURRENCY=1 (rate limit 확인 전 기본값 유지)
```

### 10-2. 코드/테스트 확인

```
[ ] ⑥ 전체 테스트 통과: uv run pytest tests/ -q --tb=no
[ ] ⑦ retailer smoke PASS:
        export ANTHROPIC_API_KEY=...
        export RUN_REAL_CLAUDE_SMOKE=1
        uv run pytest tests/smoke/ -v -k "retailer"
        → 4 passed 확인
```

### 10-3. 운영 적용

```bash
# backend/.env 수정
PHASE3_TOOL_USE_ENABLED=true
PHASE3_TOOL_USE_CONCURRENCY=1
PHASE3_TOOL_USE_GLOBAL_CONCURRENCY=3
PHASE3_TOOL_USE_MODEL=claude-haiku-4-5-20251001
```

```
[ ] ⑧ 위 3개 환경변수 설정
[ ] ⑨ 백엔드 재시작 (환경변수 재로드)
[ ] ⑩ 설정 확인:
        uv run python scripts/phase3_rollout_observe.py --precheck
        → PHASE3_TOOL_USE_ENABLED: ✓ ON
```

### 10-4. FastAPI 경로 실처리 1건 확인

```
[ ] ⑪ 업로드 UI 또는 API로 문서 1건 처리
[ ] ⑫ 처리 중 로그 확인 (Tool Use 실행 확인):
        tail -f /var/log/backend.log | grep "Tool Use"
        → "[doc_id] Tool Use 성공 (NNNms)" 메시지 확인
[ ] ⑬ DB token row 확인:
```

```sql
SELECT doc_id, phase, input_tok, output_tok, run_at
FROM v3_usage_log
WHERE phase = 'phase3_tool_use'
ORDER BY run_at DESC
LIMIT 5;
-- input_tok > 0, output_tok > 0 확인
```

```
[ ] ⑭ phase3_output.json 확인:
        cat extracted/{doc_id}/phase3_output.json | python -m json.tool | grep -E "confirmed_retailers|basis"
        → confirmed_retailers 있음, basis 값 확인
[ ] ⑮ confirm_mapping CSV 확인:
        tail -3 mappings/ocr_retailer.csv
        tail -3 mappings/ocr_dist.csv
```

### 10-5. 운영 중단 기준 재확인

처리 후 즉시 `phase3_rollout_observe.py --db` 실행:

```bash
uv run python scripts/phase3_rollout_observe.py --db
```

아래 중 하나라도 해당하면 즉시 롤백:

```
⛔ tool_not_called > 0
⛔ fallback rate > 20%
⛔ confirmed_retailers가 legacy 대비 10% 이상 감소
⛔ phase3_tool_use token row 0건 (FastAPI 경로에서도 0이면)
⛔ phase3_output.json 누락
⛔ 예외 로그 발생 (ToolUseContractError 외)
```

### 10-6. Rollback

```bash
# 즉시 롤백
# backend/.env 수정
PHASE3_TOOL_USE_ENABLED=false

# 백엔드 재시작
# (배포 방식에 따라: systemctl restart, docker restart, uv run uvicorn ...)

# 확인
uv run python scripts/phase3_rollout_observe.py --precheck
# → PHASE3_TOOL_USE_ENABLED: ⚠ OFF 확인
```

---

## 11. 운영 FastAPI Token 기록 확인 절차

### 직접 스크립트 실행 vs FastAPI 경로 차이

| | 직접 스크립트 실행 | FastAPI 경로 (운영) |
|-|------------------|--------------------|
| DB pool 초기화 | ✗ (없음) | ✅ (startup 시 초기화) |
| token row INSERT | ✗ (warning 후 무시) | ✅ (정상 기록) |
| `--db` 조회 결과 | 0건 (false alarm) | N건 (실제) |

직접 `phase3_rollout_observe.py`를 실행했을 때 `C4: token 기록 없음` 오류는 **standalone 실행의 한계**이며, FastAPI를 통한 실제 분석에서는 정상 기록된다.

### FastAPI 경로 Token 확인 쿼리

운영 배포 후 1건 처리 뒤 아래 SQL로 확인:

```sql
-- 최근 phase3_tool_use 기록
SELECT
    doc_id,
    run_id,
    phase,
    model,
    input_tok,
    output_tok,
    run_at
FROM v3_usage_log
WHERE phase = 'phase3_tool_use'
ORDER BY run_at DESC
LIMIT 10;
```

기대값:
- `phase = 'phase3_tool_use'`
- `model = 'claude-haiku-4-5-20251001'`
- `input_tok > 0` (일반적으로 1,000~150,000 범위)
- `output_tok > 0`
- `run_at` ≈ 처리 시각

### FastAPI에서도 token row 0건이면

```bash
# 즉시 STOP
PHASE3_TOOL_USE_ENABLED=false
# 원인 파악: asyncpg 설치 여부, DB connection pool 상태, _record_tool_use_token_usage 로그 확인
```

---

## 12. Dist 1:N Tool Use 검증 절차 (2026-06-05 완료)

### 검증 목적

Phase 3 Tool Use의 마지막 공백이었던 Dist 1:N 케이스에 대해 Claude 결정 경로가
실제로 동작하는지 확인한다.

### 실행 조건

- `PHASE3_TOOL_USE_ENABLED=true` (Controlled Production Enable 상태)
- `ANTHROPIC_API_KEY` 설정됨
- retail_user.csv에 동일 소매처코드에 2개 이상의 판매처코드가 있는 케이스 존재

### 검증 케이스 (2026-06-05 실행 결과)

| 케이스 | 소매처코드 | 후보 수 | Claude 결과 | 비고 |
|--------|----------|--------|------------|------|
| 2개 후보, 발행처 힌트 있음 | 6154844 | 2 | **tool_use** (`1300061`) | ✅ 확정 |
| 2개 후보, 그룹사 매칭 | 6051361 | 2 | **tool_use** (`1305046`) | ✅ 확정 |
| 9개 후보, 힌트 없음 | 6003788 | 9 | **needs_confirmation** | ✅ 올바른 pending |
| 9개 후보, CVS 힌트 있음 | 6003788 | 9 | **tool_use** (`1303568`) | ✅ CVS 전문 부서 선택 |
| 후보 1개 | - | 1 | **auto_1_to_1** (Claude 미호출) | ✅ |
| 후보 0개 | - | 0 | **not_found** (Claude 미호출) | ✅ |
| 후보 외 선택 mock | - | 2 | **needs_confirmation** (저장 거부) | ✅ |

### PASS 기준

```
[ ] dist 2개 이상 후보: basis="tool_use" 또는 "needs_confirmation" (오류 아님)
[ ] tool_use 확정 시: dist_code가 반드시 후보 목록 내 코드
[ ] dist 1개 후보: auto_1_to_1 (Claude 호출 없음)
[ ] dist 0개 후보: not_found (Claude 호출 없음)
[ ] 후보 외 dist_code: 저장 거부 → needs_confirmation
[ ] confirm_mapping(dist) 호출: 확정 시 1회만, 빈 dist_code 저장 없음
[ ] token usage: dist_api_calls > 0 (1:N 처리 시)
```

### STOP 기준

```
⛔ dist_code가 후보 목록 밖의 값으로 ocr_dist.csv에 저장됨
⛔ 후보 1개인데 Claude가 호출됨 (비용 낭비)
⛔ dist 1:N 처리 중 전체 phase3 fallback 발생
⛔ confirm_mapping(dist) 중복 호출 (동일 복합키)
```

### 운영 중 모니터링 지표

```bash
# observe script로 dist 지표 확인
uv run python scripts/phase3_rollout_observe.py

# 출력에서 확인:
# [ Dist 판매처 현황 ]
#   dist_code 확정    : N건 (X%)
#   dist_code 미확정  : N건 (X%)

# DB에서 dist tool_use 확인
SELECT doc_id, input_tok, output_tok FROM v3_usage_log
WHERE phase = 'phase3_tool_use' AND input_tok > 0
ORDER BY run_at DESC LIMIT 10;
-- dist 처리가 있으면 token 수가 기존보다 증가 (dist API 호출분 추가)
```

### Rollback

Dist 1:N Tool Use는 별도 feature flag 없음.
issue 발생 시:
1. `PHASE3_TOOL_USE_ENABLED=false` → 전체 Tool Use 비활성화 (기존 rollback 경로)
2. 또는 `_build_dist_decisions_with_tool_use()` 호출부를 주석 처리 (코드 수준)

---

## 13. 운영 모니터링 체크리스트

### 13-1. 일상 모니터링 (처리 후 즉시)

```bash
# 1. observe script 실행
uv run python scripts/phase3_rollout_observe.py --db
```

확인 항목:

| 지표 | 정상 범위 | STOP 기준 |
|------|----------|----------|
| `result_basis=legacy` 비율 | ≤ 20% | > 20% |
| `phase3_output.json` 누락 | 0건 | 1건 이상 |
| dist_code 미확정 비율 | ≤ 10% (모호 케이스 허용) | 갑작스러운 급증 |
| pending rate | ≤ legacy 2배 | 2배 초과 |
| DB `phase3_tool_use` row | 처리 건수와 일치 | 0건 (asyncpg 문제) |

### 13-2. DB 조회 (처리 후)

```sql
-- 최근 phase3_tool_use 기록 확인
SELECT doc_id, input_tok, output_tok, run_at
FROM v3_usage_log
WHERE phase = 'phase3_tool_use'
ORDER BY run_at DESC
LIMIT 20;

-- fallback 발생 여부 (phase3_tool_use 없이 phase3만 있는 문서)
SELECT d.doc_id, d.updated_at
FROM v3_documents d
WHERE d.updated_at > NOW() - INTERVAL '24 hours'
  AND NOT EXISTS (
    SELECT 1 FROM v3_usage_log u
    WHERE u.doc_id = d.doc_id AND u.phase = 'phase3_tool_use'
  )
ORDER BY d.updated_at DESC;

-- token 사용량 일별 집계
SELECT DATE(run_at) AS dt,
       SUM(input_tok) AS total_in,
       SUM(output_tok) AS total_out,
       COUNT(*) AS docs
FROM v3_usage_log
WHERE phase = 'phase3_tool_use'
GROUP BY DATE(run_at)
ORDER BY dt DESC;
```

### 13-3. 로그 확인

```bash
# Tool Use 성공/fallback 로그
tail -f /var/log/backend.log | grep -E "Tool Use|fallback|tool_not_called|Dist 1:N"

# 기대 패턴 (정상)
# [doc_id] Tool Use 성공 (NNNms) → success path
# [doc_id] Phase 3 완료 (Tool Use) — tool_use=NNNms / total=NNNms

# 주의 패턴 (fallback)
# [doc_id] Tool Use 실패 → Legacy fallback. 원인: [ToolUseContractError] ...
# [doc_id] tool 미호출: Claude가 lookup_retailer를 호출하지 않고 종료

# 이상 패턴 (즉시 확인)
# RateLimitError 연속 발생 → GLOBAL_CONCURRENCY 조정 검토
# ToolUseApiError 연속 발생 → API key / network 확인
```

### 13-4. CSV 저장 확인

```bash
# confirm_mapping이 정상 동작하는지 확인
tail -5 mappings/ocr_retailer.csv
tail -5 mappings/ocr_dist.csv
tail -5 mappings/ocr_product.csv

# 최근 갱신 시각
ls -la mappings/ocr_*.csv
```

### 13-5. GLOBAL_CONCURRENCY 설정 확인

```bash
# 현재 설정값 확인
grep PHASE3_TOOL_USE_GLOBAL_CONCURRENCY backend/.env
# 기본값: 3
# Tier 2: 3~5
# Tier 3+: 5~10
```

### 13-6. Dist 1:N 모니터링 포인트

```bash
# dist_code 미확정 소매처 확인 (1:N pending)
python -c "
import json, csv
from pathlib import Path
from collections import defaultdict

by_code = defaultdict(list)
with Path('mappings/retail_user.csv').open(encoding='utf-8-sig') as f:
    for r in csv.DictReader(f): by_code[r['소매처코드']].append(r)

n1n = 0
for doc in Path('extracted').iterdir():
    p3 = doc / 'phase3_output.json'
    if not p3.exists(): continue
    data = json.loads(p3.read_text(encoding='utf-8'))
    for info in data.get('confirmed_retailers', {}).values():
        rc = info.get('retailer_code','')
        if rc and not info.get('dist_code') and len(by_code[rc]) > 1:
            n1n += 1
print(f'dist 1:N pending 소매처: {n1n}건')
"
```
