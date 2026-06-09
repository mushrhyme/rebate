"""Admin — 사용량 모니터링 API."""
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query

from ...core.auth import require_admin
from ...db.queries import list_documents

router = APIRouter(prefix="/api/admin/usage", tags=["admin-usage"])

# 모델별 1M 토큰당 달러 단가 (input, output, cache_read, cache_write)
# 가격은 변경될 수 있음 — anthropic.com/pricing 확인 후 업데이트
_MODEL_PRICE: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-4-5-20251001": (0.80, 4.00, 0.08, 1.00),
    "claude-haiku-4-5":          (0.80, 4.00, 0.08, 1.00),
    "claude-sonnet-4-5-20251001": (3.00, 15.00, 0.30, 3.75),
    "claude-sonnet-4-5":         (3.00, 15.00, 0.30, 3.75),
    "claude-sonnet-4-6":         (3.00, 15.00, 0.30, 3.75),
}
_DEFAULT_PRICE = (3.00, 15.00, 0.30, 3.75)  # 미등록 모델 fallback


def _calc_cost_usd(model: str, input_tok: int, output_tok: int, cache_read: int, cache_write: int) -> float:
    p_in, p_out, p_cr, p_cw = _MODEL_PRICE.get(model, _DEFAULT_PRICE)
    return (
        input_tok  * p_in  / 1_000_000
        + output_tok * p_out / 1_000_000
        + cache_read * p_cr  / 1_000_000
        + cache_write * p_cw / 1_000_000
    )


def _period_range(period: str) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if period == "this_month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif period == "last_month":
        first_of_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_month_end = first_of_this - timedelta(seconds=1)
        start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = first_of_this
    elif period == "last_3_months":
        start = (now - timedelta(days=90)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    else:  # all
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = now
    return start, end


def _safe_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


@router.get("")
async def get_usage(
    period: str = Query(default="this_month"),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    _admin: dict = Depends(require_admin),
):
    """기간별 분석 실행 이력 — 관리자 전용.
    S3 meta.json의 usage_log 리스트 기반 집계."""
    if start_date and end_date:
        try:
            start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
            end = datetime.fromisoformat(end_date).replace(
                hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc,
            )
            period = "custom"
        except ValueError:
            start, end = _period_range(period)
    else:
        start, end = _period_range(period)

    docs = await list_documents()
    # doc 정보 인덱스
    doc_index = {d["doc_id"]: d for d in docs}

    # usage_log 항목을 run_id 단위로 집계
    runs_by_id: dict[str, dict] = {}
    for doc in docs:
        for entry in doc.get("usage_log", []):
            recorded_at = _safe_dt(entry.get("recorded_at"))
            if not recorded_at:
                continue
            if recorded_at < start or recorded_at >= end:
                continue
            run_id = entry.get("run_id") or f"single_{doc['doc_id']}"
            if run_id not in runs_by_id:
                runs_by_id[run_id] = {
                    "run_id": run_id,
                    "doc_id": doc["doc_id"],
                    "run_at": recorded_at.isoformat(),
                    "pdf_filename": doc.get("pdf_filename", ""),
                    "status": doc.get("status"),
                    "confirmed_at": doc.get("confirmed_at"),
                    "pages_count": doc.get("pages_count"),
                    "uploader_username": doc.get("uploaded_by_username"),
                    "uploader_name_ja": doc.get("uploaded_by_name_ja"),
                    "uploader_name": doc.get("uploaded_by_name_ja"),
                    "phases": {},
                    "total_cost_usd": 0.0,
                    "phase_timings": doc.get("phase_timings") or {},
                }
            phase = entry.get("phase", "unknown")
            input_tok  = entry.get("input_tok", 0)
            output_tok = entry.get("output_tok", 0)
            cache_read = entry.get("cache_read", 0)
            cache_write = entry.get("cache_write", 0)
            model = entry.get("model", "")
            cost = _calc_cost_usd(model, input_tok, output_tok, cache_read, cache_write)
            runs_by_id[run_id]["phases"][phase] = {
                "input": input_tok,
                "output": output_tok,
                "model": model,
                "cache_read": cache_read,
                "cache_write": cache_write,
                "cost_usd": round(cost, 6),
            }
            runs_by_id[run_id]["total_cost_usd"] = round(
                runs_by_id[run_id]["total_cost_usd"] + cost, 6
            )

    # usage_log 없는 문서는 token_usage로 폴백 (기존 데이터 표시용)
    for doc in docs:
        if doc.get("usage_log"):
            continue
        token_usage = doc.get("token_usage") or {}
        if not token_usage:
            continue
        run_id = f"legacy_{doc['doc_id']}"
        if run_id in runs_by_id:
            continue
        updated = _safe_dt(doc.get("updated_at"))
        if not updated or updated < start or updated >= end:
            continue
        runs_by_id[run_id] = {
            "run_id": run_id,
            "doc_id": doc["doc_id"],
            "run_at": (doc.get("updated_at") or ""),
            "pdf_filename": doc.get("pdf_filename", ""),
            "status": doc.get("status"),
            "confirmed_at": doc.get("confirmed_at"),
            "pages_count": doc.get("pages_count"),
            "uploader_username": doc.get("uploaded_by_username"),
            "uploader_name_ja": doc.get("uploaded_by_name_ja"),
            "uploader_name": doc.get("uploaded_by_name_ja"),
            "phases": token_usage,
        }

    runs = sorted(runs_by_id.values(), key=lambda r: r.get("run_at", ""), reverse=True)
    total_cost_usd = round(sum(r.get("total_cost_usd", 0.0) for r in runs), 4)
    return {
        "runs": runs,
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total_cost_usd": total_cost_usd,
    }
