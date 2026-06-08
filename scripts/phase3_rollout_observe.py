#!/usr/bin/env python3
"""phase3_rollout_observe.py — Phase 3 Tool Use Limited Rollout 관찰 스크립트

사용법:
  # Rollout 전 필수 환경 점검 (asyncpg, API key, DB)
  python scripts/phase3_rollout_observe.py --precheck

  # extracted/ 디렉토리의 전체 문서 집계
  python scripts/phase3_rollout_observe.py

  # 특정 문서만
  python scripts/phase3_rollout_observe.py doc_id_1 doc_id_2

  # DB token usage 포함 및 token 기록 검증 (asyncpg + DATABASE_URL 필요)
  python scripts/phase3_rollout_observe.py --db

  # 기준 경로 명시
  python scripts/phase3_rollout_observe.py --extracted-dir /path/to/extracted

출력:
  - asyncpg / API key / DB 연결 상태 (--precheck)
  - 총 문서 수 / Tool Use 성공 / fallback / pending 비율
  - token input/output 합계 + 기록 누락 경고 (--db 시)
  - 문서별 상세 테이블
  - 운영 중단 기준 경고

주의:
  이 스크립트는 파이프라인 코드를 호출하지 않는다.
  phase3_output.json과 DB 읽기만 수행한다.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# ── 경로 설정 ─────────────────────────────────────────────────────────────────

_ROOT        = Path(__file__).parent.parent
_EXTRACTED   = _ROOT / "extracted"
_MAPPINGS    = _ROOT / "mappings"

# ── 운영 중단 기준 ─────────────────────────────────────────────────────────────

_STOP_FALLBACK_RATE      = 0.20   # fallback 비율 20% 초과 시 경고
_STOP_MISSING_JSON       = 1      # phase3_output.json 누락 1건 이상 시 경고
_STOP_LEGACY_DIFF_DOCS   = 1      # legacy 대비 결과 차이 1건 이상 시 경고
_STOP_CR_DROP_RATE       = 0.10   # confirmed_retailers 감소율 10% 초과 시 경고
_LEGACY_BASELINE_DIR     = _ROOT / "rollout_baseline" / "phase3_legacy"


# ── 데이터 수집 ───────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _detect_result_basis(data: dict) -> str:
    """확정 결과의 basis 값만 분류한다 (실행 경로와 무관).

    반환:
      "tool_use"  — basis="tool_use" 있음
      "cache"     — basis="cache"/"bracket_code"만 있음
      "legacy"    — basis가 위 외의 값 (고신뢰도 CSV 경로)
      "pending"   — confirmed 없음 (전부 pending)
    """
    retailers = data.get("confirmed_retailers", {})
    products  = data.get("confirmed_products", {})
    bases: set = set()
    for v in retailers.values():
        if isinstance(v, dict):
            bases.add(v.get("basis", ""))
    for v in products.values():
        if isinstance(v, dict):
            bases.add(v.get("basis", ""))

    if "tool_use" in bases:
        return "tool_use"
    if bases and bases <= {"cache", "bracket_code", ""}:
        return "cache"
    if bases:
        return "legacy"
    return "pending"


def _detect_path_type(data: dict) -> str:
    """phase3_output.json 에서 실행 경로를 추정한다.

    주의:
      Tool Use 경로로 실행됐더라도 모든 소매처가 cache hit이면 result_basis="cache"가 된다.
      이 경우 path_type도 "cache"로 보이므로 Tool Use 실행 여부와 혼동될 수 있다.
      실제 Tool Use 실행 여부는 token 기록(--db)이나 처리 로그로 확인해야 한다.

    반환:
      "tool_use"  — basis="tool_use" 있음 → Tool Use가 실행되고 새로 확정됨
      "cache"     — basis가 모두 cache/bracket_code → Tool Use 또는 legacy 양쪽 가능
                    (Tool Use에서 cache hit 100%면 이 값이 나온다)
      "legacy"    — basis가 cache 외의 legacy 값 → legacy CSV 주입 경로
      "unknown"   — confirmed 없음
    """
    basis = _detect_result_basis(data)
    if basis == "pending":
        return "unknown"
    return basis


def _count_pending_items(data: dict) -> int:
    """items 중 unconfirmed=True 수."""
    return sum(1 for item in data.get("items", []) if item.get("unconfirmed"))


def _count_unique_customers(data: dict) -> int:
    return len({item.get("customer", "") for item in data.get("items", [])
                if item.get("customer")})


def _load_legacy_confirmed_retailers(doc_id: str) -> int:
    """Legacy baseline에서 confirmed_retailers 수를 읽는다."""
    p = _LEGACY_BASELINE_DIR / doc_id / "phase3_output.json"
    if not p.exists():
        return -1  # unknown
    try:
        data = _load_json(p)
        return len(data.get("confirmed_retailers", {})) if data else -1
    except Exception:
        return -1


def analyze_doc(doc_id: str, extracted_dir: Path) -> dict:
    """단일 문서의 phase3_output.json을 분석한다."""
    json_path = extracted_dir / doc_id / "phase3_output.json"

    if not json_path.exists():
        return {
            "doc_id":   doc_id,
            "status":   "MISSING_JSON",
            "path_type": "-",
            "form_id":  "-",
            "confirmed_retailers": 0,
            "confirmed_products":  0,
            "total_items":    0,
            "pending_items":  0,
            "total_customers": 0,
            "mtime": "-",
            "alert": "⚠ MISSING_JSON",
        }

    data = _load_json(json_path)
    if data is None:
        return {
            "doc_id":   doc_id,
            "status":   "PARSE_ERROR",
            "path_type": "-",
            "form_id":  "-",
            "confirmed_retailers": 0,
            "confirmed_products":  0,
            "total_items":    0,
            "pending_items":  0,
            "total_customers": 0,
            "mtime": "-",
            "alert": "⚠ PARSE_ERROR",
        }

    mtime = datetime.fromtimestamp(json_path.stat().st_mtime).strftime("%m-%d %H:%M")
    path_type      = _detect_path_type(data)
    result_basis   = _detect_result_basis(data)
    confirmed_r    = len(data.get("confirmed_retailers", {}))
    confirmed_p    = len(data.get("confirmed_products", {}))
    items          = data.get("items", [])
    pending_items  = _count_pending_items(data)
    total_customers = _count_unique_customers(data)
    legacy_cr      = _load_legacy_confirmed_retailers(doc_id)

    # path_type이 "cache"일 때 실행 경로 보조 표시
    # → Tool Use 경로에서 cache hit 100%이면 path_type="cache"이지만 실제론 Tool Use 실행됨
    execution_note = ""
    if result_basis == "cache":
        execution_note = "cache(TU?/legacy?)"
    elif result_basis == "tool_use":
        execution_note = "tool_use확정"
    elif result_basis == "legacy":
        execution_note = "legacy확정"

    # confirmed_retailers=0이면서 items>0인 경우: 의심
    alert = ""
    if confirmed_r == 0 and total_customers > 0 and path_type in ("tool_use", "cache"):
        if legacy_cr > 0:
            alert = f"⚠ CR=0 (legacy={legacy_cr})"
        elif legacy_cr == 0:
            alert = "(legacy도 0)"

    # dist 지표 (confirmed_retailers에서 파생)
    cr_dict = data.get("confirmed_retailers", {})
    dist_confirmed = sum(
        1 for v in cr_dict.values()
        if isinstance(v, dict) and v.get("dist_code")
    )
    dist_empty = sum(
        1 for v in cr_dict.values()
        if isinstance(v, dict) and not v.get("dist_code")
    )

    return {
        "doc_id":              doc_id,
        "status":              "OK",
        "path_type":           path_type,
        "result_basis":        result_basis,
        "execution_note":      execution_note,
        "form_id":             data.get("form_id", ""),
        "confirmed_retailers": confirmed_r,
        "confirmed_products":  confirmed_p,
        "dist_confirmed":      dist_confirmed,    # dist_code 있는 confirmed_retailers 수
        "dist_empty":          dist_empty,        # dist_code 없는 confirmed_retailers 수 (1:N pending or not_found)
        "total_items":         len(items),
        "pending_items":       pending_items,
        "total_customers":     total_customers,
        "legacy_cr":           legacy_cr,
        "mtime":               mtime,
        "alert":               alert,
    }


def collect_docs(doc_ids: list[str] | None, extracted_dir: Path) -> list[str]:
    """분석 대상 doc_id 목록을 수집한다."""
    if doc_ids:
        return sorted(doc_ids)
    if not extracted_dir.exists():
        return []
    return sorted(
        d.name for d in extracted_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )


# ── 사전 점검 ─────────────────────────────────────────────────────────────────

async def run_precheck() -> bool:
    """Rollout 전 필수 환경 점검. True = 모든 항목 통과."""
    print()
    print("=" * 60)
    print("  Phase 3 Tool Use — Rollout 사전 점검")
    print("=" * 60)
    print()

    all_ok = True

    # 1. asyncpg
    try:
        import asyncpg as _apg
        print(f"  asyncpg {_apg.__version__:<12} : ✓")
    except ImportError:
        print("  asyncpg                : ⛔ STOP — pip install asyncpg 또는 uv sync")
        all_ok = False

    # 2. ANTHROPIC_API_KEY
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        # fallback: try from .env
        env_path = _ROOT / "backend" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break
    if api_key:
        print(f"  ANTHROPIC_API_KEY      : ✓ (sk-ant-...{api_key[-4:]})")
    else:
        print("  ANTHROPIC_API_KEY      : ⛔ STOP — backend/.env에 설정 필요")
        all_ok = False

    # 3. DATABASE_URL
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        env_path = _ROOT / "backend" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("DATABASE_URL="):
                    db_url = line.split("=", 1)[1].strip()
                    break
    if db_url:
        print(f"  DATABASE_URL           : ✓ (설정됨)")
    else:
        print("  DATABASE_URL           : ⛔ STOP — backend/.env에 설정 필요")
        all_ok = False

    # 4. DB 연결 및 v3_usage_log 존재
    if db_url:
        try:
            import asyncpg as _apg
            conn = await _apg.connect(db_url)
            try:
                row_count = await conn.fetchval("SELECT COUNT(*) FROM v3_usage_log")
                print(f"  DB 연결 (v3_usage_log) : ✓ ({row_count:,}행)")
            finally:
                await conn.close()
        except ImportError:
            print("  DB 연결                : ⛔ asyncpg 없음")
            all_ok = False
        except Exception as e:
            print(f"  DB 연결                : ⛔ STOP — {type(e).__name__}: {str(e)[:60]}")
            all_ok = False

    # 5. PHASE3_TOOL_USE_ENABLED 확인
    enabled_env = os.getenv("PHASE3_TOOL_USE_ENABLED", "")
    if not enabled_env:
        env_path = _ROOT / "backend" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("PHASE3_TOOL_USE_ENABLED="):
                    enabled_env = line.split("=", 1)[1].strip()
                    break
    enabled = enabled_env.lower() in ("true", "1", "yes")
    status = "✓ ON" if enabled else "⚠ OFF (rollout 전에 true로 변경 필요)"
    print(f"  PHASE3_TOOL_USE_ENABLED: {status}")

    print()
    if all_ok:
        print("  ✅ 사전 점검 통과 — Rollout 진행 가능")
    else:
        print("  ⛔ 사전 점검 실패 — 위 항목 해결 후 재시도")
    print("=" * 60)
    print()
    return all_ok


# ── DB 조회 (선택적) ──────────────────────────────────────────────────────────

async def fetch_token_stats(doc_ids: list[str]) -> tuple[dict[str, dict], list[str]]:
    """v3_usage_log에서 phase3_tool_use 토큰을 집계한다.

    Returns:
        (token_stats_dict, warnings)
        token_stats: {doc_id: {"input": int, "output": int, "api_calls": int}}
        warnings:    token 기록 이상 시 경고 메시지 목록
    """
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        # fallback from .env
        env_path = _ROOT / "backend" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("DATABASE_URL="):
                    db_url = line.split("=", 1)[1].strip()
                    break

    if not db_url:
        raise RuntimeError("DATABASE_URL 미설정 (backend/.env 확인)")

    import asyncpg
    conn = await asyncpg.connect(db_url)
    try:
        rows = await conn.fetch(
            """
            SELECT doc_id,
                   SUM(input_tok)  AS input_tok,
                   SUM(output_tok) AS output_tok,
                   COUNT(*)        AS api_calls
            FROM v3_usage_log
            WHERE phase = 'phase3_tool_use'
              AND doc_id = ANY($1::text[])
            GROUP BY doc_id
            """,
            doc_ids,
        )
        result: dict[str, dict] = {}
        for row in rows:
            result[row["doc_id"]] = {
                "input":     row["input_tok"],
                "output":    row["output_tok"],
                "api_calls": row["api_calls"],
            }

        warnings: list[str] = []
        if result:
            zero_input  = [d for d, v in result.items() if v["input"] == 0]
            zero_output = [d for d, v in result.items() if v["output"] == 0]
            if zero_input:
                warnings.append(f"⚠ input_tokens=0인 문서: {len(zero_input)}건")
            if zero_output:
                warnings.append(f"⚠ output_tokens=0인 문서: {len(zero_output)}건")

        return result, warnings
    finally:
        await conn.close()


# ── 출력 ──────────────────────────────────────────────────────────────────────

def _bar(rate: float, width: int = 20) -> str:
    filled = int(round(rate * width))
    return "█" * filled + "░" * (width - filled)


def print_summary(results: list[dict], token_stats: dict | None = None) -> None:
    total = len(results)
    if total == 0:
        print("처리된 문서가 없습니다.")
        return

    # 집계
    missing       = sum(1 for r in results if r["status"] != "OK")
    tool_use_docs = sum(1 for r in results if r["path_type"] == "tool_use")
    cache_docs    = sum(1 for r in results if r["path_type"] == "cache")
    legacy_docs   = sum(1 for r in results if r["path_type"] == "legacy")
    unknown_docs  = sum(1 for r in results if r["path_type"] == "unknown")

    # fallback 판정:
    # DB token 있는 경우 phase3_tool_use 시도 후 basis="tool_use" 없음 → fallback
    # DB 없는 경우: path_type이 "legacy"이면 fallback 가능성 (단, 최초 Tool Use가 아닐 수도)
    fallback_docs = legacy_docs  # 근사치 (DB 없을 때)

    ok_docs = [r for r in results if r["status"] == "OK"]
    total_pending  = sum(r["pending_items"]  for r in ok_docs)
    total_items    = sum(r["total_items"]    for r in ok_docs)
    total_customers = sum(r["total_customers"] for r in ok_docs)

    fallback_rate  = fallback_docs / total if total else 0
    pending_rate   = total_pending / total_items if total_items else 0

    # ── Legacy 대비 confirmed_retailers 비교 ─────────────────────────────────
    total_tu_cr     = sum(r.get("confirmed_retailers", 0) for r in ok_docs)
    total_legacy_cr = sum(r.get("legacy_cr", 0) for r in ok_docs if r.get("legacy_cr", -1) >= 0)
    cr_drop_docs    = sum(1 for r in ok_docs
                         if r.get("legacy_cr", -1) > 0 and r.get("confirmed_retailers", 0) == 0)
    cr_drop_rate    = (1 - total_tu_cr / total_legacy_cr) if total_legacy_cr > 0 else 0

    # DB token 집계 (token_stats는 이제 (dict, warnings) 튜플)
    total_input = total_output = total_api_calls = 0
    if token_stats is not None:
        ts_dict_agg, _ = token_stats
        for v in ts_dict_agg.values():
            total_input     += v.get("input", 0)
            total_output    += v.get("output", 0)
            total_api_calls += v.get("api_calls", 0)

    # ── 헤더 ──────────────────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("  Phase 3 Tool Use — Limited Rollout 관찰 결과")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)

    # ── 처리 현황 ─────────────────────────────────────────────────────────────
    print()
    print("[ 처리 현황 ]")
    print(f"  총 문서 수          : {total:>5}")
    print(f"  phase3_output 누락  : {missing:>5}  {'⚠ STOP C1' if missing >= _STOP_MISSING_JSON else '✓'}")
    print()
    print(f"  result_basis=tool_use : {tool_use_docs:>4}  ({tool_use_docs/total*100:5.1f}%)"
          f"  {_bar(tool_use_docs/total)}")
    print(f"  result_basis=cache    : {cache_docs:>4}  ({cache_docs/total*100:5.1f}%)"
          f"  {_bar(cache_docs/total)}")
    print(f"    ↑ Tool Use cache hit 또는 legacy cache — 로그/DB로 실행경로 확인 필요")
    print(f"  result_basis=legacy   : {legacy_docs:>4}  ({legacy_docs/total*100:5.1f}%)"
          f"  {_bar(legacy_docs/total)}"
          f"  {'⚠ STOP C2' if fallback_rate > _STOP_FALLBACK_RATE else ''}")
    print(f"  unknown               : {unknown_docs:>4}  ({unknown_docs/total*100:5.1f}%)")
    print()
    print("[ confirmed_retailers 추이 ]")
    print(f"  Tool Use 확정       : {total_tu_cr:>5}")
    if total_legacy_cr > 0:
        drop_flag = f"  {'⚠ STOP CR감소' if cr_drop_rate > _STOP_CR_DROP_RATE else '✓'}"
        print(f"  Legacy 기준         : {total_legacy_cr:>5}  (감소율: {cr_drop_rate*100:.1f}%{drop_flag})")
    else:
        print(f"  Legacy 기준         : (baseline 없음)")
    if cr_drop_docs > 0:
        print(f"  CR=0 (legacy>0)     : {cr_drop_docs:>5}건  ⚠ STOP — tool_not_called 가능성")

    # ── Dist 1:N 지표 ─────────────────────────────────────────────────────────
    total_dist_confirmed = sum(r.get("dist_confirmed", 0) for r in ok_docs)
    total_dist_empty     = sum(r.get("dist_empty",     0) for r in ok_docs)
    total_cr_all         = sum(r.get("confirmed_retailers", 0) for r in ok_docs)
    print()
    print("[ Dist 판매처 현황 ]")
    print(f"  confirmed_retailers 합계 : {total_cr_all:>5}")
    print(f"  dist_code 확정           : {total_dist_confirmed:>5}  "
          f"({total_dist_confirmed/total_cr_all*100:5.1f}%  ← auto + tool_use)" if total_cr_all else
          f"  dist_code 확정           :     0")
    print(f"  dist_code 미확정(pending): {total_dist_empty:>5}  "
          f"({total_dist_empty/total_cr_all*100:5.1f}%  ← 1:N pending or not_found)" if total_cr_all else
          f"  dist_code 미확정(pending):     0")
    print(f"  ※ tool_use 확정 vs auto 구분은 처리 로그 또는 DB phase3_tool_use 기록으로 확인")

    # ── Pending ───────────────────────────────────────────────────────────────
    print()
    print("[ Pending 현황 ]")
    print(f"  총 items            : {total_items:>5}")
    print(f"  총 pending items    : {total_pending:>5}  ({pending_rate*100:5.1f}%)")
    print(f"  (미확정 거래처 수   : {total_customers:>5})")

    # ── Token (DB 있을 때) ────────────────────────────────────────────────────
    if token_stats is not None:
        ts_dict, ts_warnings = token_stats
        print()
        print("[ Token Usage (phase3_tool_use) ]")
        if ts_dict:
            print(f"  input tokens 합계   : {total_input:>10,}")
            print(f"  output tokens 합계  : {total_output:>10,}")
            print(f"  API 호출 수 합계    : {total_api_calls:>10,}")
            docs_with_tokens = len(ts_dict)
            print(f"  token 기록 문서     : {docs_with_tokens:>4} / {total}")
            expected = tool_use_docs + cache_docs  # Tool Use 시도한 문서 수
            missing_token_docs = max(0, expected - docs_with_tokens)
            if missing_token_docs > 0:
                print(f"  ⛔ token 미기록 문서: {missing_token_docs}건")
        else:
            print(f"  ⛔ phase3_tool_use 기록 없음 (DB에 0행)")
        for w in ts_warnings:
            print(f"  {w}")
    else:
        print()
        print("[ Token Usage ] (--db 옵션으로 DB 조회 + C4 검증 가능)")

    # ── 운영 중단 기준 체크 ───────────────────────────────────────────────────
    print()
    print("[ 운영 중단 기준 체크 ]")
    stops = []
    if missing >= _STOP_MISSING_JSON:
        stops.append(f"  ⛔ C1: phase3_output 누락 {missing}건 (기준: ≥{_STOP_MISSING_JSON})")
    if fallback_rate > _STOP_FALLBACK_RATE:
        stops.append(f"  ⛔ C2: fallback 비율 {fallback_rate*100:.1f}% (기준: >{_STOP_FALLBACK_RATE*100:.0f}%)")
    # CR 감소율 / tool_not_called 의심
    if cr_drop_rate > _STOP_CR_DROP_RATE and total_legacy_cr > 0:
        stops.append(
            f"  ⛔ C9: confirmed_retailers 감소율 {cr_drop_rate*100:.1f}% "
            f"(기준: >{_STOP_CR_DROP_RATE*100:.0f}%, legacy={total_legacy_cr} → TU={total_tu_cr})"
        )
    if cr_drop_docs > 0:
        stops.append(
            f"  ⛔ C10: legacy CR>0인데 TU CR=0 문서 {cr_drop_docs}건 "
            f"(tool_not_called 또는 Claude tool 미호출)"
        )
    # C4: DB token 기록 검증 (--db 사용 시)
    if token_stats is not None:
        ts_dict, _ts_warn = token_stats
        if not ts_dict:
            stops.append("  ⛔ C4: phase3_tool_use token 기록 없음 — Tool Use가 활성화되었는지 확인")
        else:
            expected_tu = tool_use_docs + cache_docs
            missing_tok = max(0, expected_tu - len(ts_dict))
            if missing_tok > 0:
                stops.append(f"  ⛔ C4: token 미기록 문서 {missing_tok}건")

    if not stops:
        if token_stats is not None:
            print("  ✓ 모든 자동 기준 통과 (C4 token 기록 검증 포함)")
            print("  수동 확인 필요: C3 confirm_mapping CSV, C5 legacy 대비 결과")
        else:
            print("  ✓ JSON 기반 기준 통과 (C4 token: --db 옵션 필요, C3/C5 수동 확인)")
    else:
        for s in stops:
            print(s)
        print()
        print("  → 즉시 롤백:")
        print("    backend/.env  PHASE3_TOOL_USE_ENABLED=false 로 변경 후 재시작")

    # ── 문서별 상세 ───────────────────────────────────────────────────────────
    print()
    print("[ 문서별 상세 ]")
    print(f"  {'doc_id':<32} {'basis':9} {'form':8} {'conf_R':>6} {'conf_P':>6} {'pend':>5} {'mtime':>12} {'note'}")
    print("  " + "-" * 100)
    for r in results:
        note = r.get("alert", "")
        rb = r.get("result_basis", r.get("path_type", "-"))
        if rb == "legacy" and not note:
            note = "(fallback?)"
        if rb == "cache" and not note:
            note = r.get("execution_note", "")
        print(
            f"  {r['doc_id']:<32} "
            f"{rb:9} "
            f"{r['form_id']:8} "
            f"{r['confirmed_retailers']:>6} "
            f"{r['confirmed_products']:>6} "
            f"{r['pending_items']:>5} "
            f"{r['mtime']:>12}  "
            f"{note}"
        )
    print()
    print("  ※ result_basis='cache': Tool Use 경로 또는 legacy 경로 모두 가능.")
    print("    Tool Use 실행 여부는 처리 로그 또는 --db 옵션으로 확인.")

    print()
    print("=" * 70)
    print("  결과 기록 양식: docs/rollout-phase3-tool-use.md 섹션 5 참조")
    print("=" * 70)
    print()


# ── 엔트리포인트 ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 3 Tool Use Limited Rollout 관찰 스크립트"
    )
    parser.add_argument(
        "doc_ids", nargs="*",
        help="분석할 doc_id 목록. 생략 시 extracted/ 전체 대상.",
    )
    parser.add_argument(
        "--db", action="store_true",
        help="DB에서 token usage 조회 및 C4 기준 검증 (DATABASE_URL + asyncpg 필요)",
    )
    parser.add_argument(
        "--precheck", action="store_true",
        help="Rollout 전 환경 사전 점검 (asyncpg, API key, DB 연결)",
    )
    parser.add_argument(
        "--extracted-dir", default=str(_EXTRACTED),
        help=f"extracted/ 디렉토리 경로 (기본: {_EXTRACTED})",
    )
    args = parser.parse_args()

    import asyncio

    # ── 사전 점검 모드 ────────────────────────────────────────────────────────
    if args.precheck:
        ok = asyncio.run(run_precheck())
        sys.exit(0 if ok else 1)

    extracted_dir = Path(args.extracted_dir)
    doc_ids = collect_docs(args.doc_ids or None, extracted_dir)

    if not doc_ids:
        print(f"분석할 문서가 없습니다. 경로: {extracted_dir}")
        sys.exit(0)

    print(f"\n분석 대상: {len(doc_ids)}건  (경로: {extracted_dir})")
    results = [analyze_doc(doc_id, extracted_dir) for doc_id in doc_ids]

    token_stats: tuple | None = None
    if args.db:
        try:
            token_stats = asyncio.run(fetch_token_stats(doc_ids))
            ts_dict, _ = token_stats
            print(f"DB token 조회 완료: {len(ts_dict)}건")
        except ImportError:
            print("⛔ asyncpg 미설치 — token C4 기준 검증 불가 (uv sync 또는 pip install asyncpg)")
        except RuntimeError as e:
            print(f"⛔ DB 연결 실패: {e}")
        except Exception as e:
            print(f"⛔ token 조회 오류: {type(e).__name__}: {e}")

    print_summary(results, token_stats)


if __name__ == "__main__":
    main()
