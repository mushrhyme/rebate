"""Admin — 사용량 모니터링 API."""
from datetime import datetime, timezone, timedelta
import json as _json

from fastapi import APIRouter, Depends, Query

from ...core.auth import require_admin
from ...core.database import get_pool

router = APIRouter(prefix="/api/admin/usage", tags=["admin-usage"])


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


@router.get("")
async def get_usage(
    period: str = Query(default="this_month"),
    start_date: str | None = Query(default=None),   # YYYY-MM-DD
    end_date:   str | None = Query(default=None),   # YYYY-MM-DD
    _admin: dict = Depends(require_admin),
):
    """기간별 분석 실행 이력 (run 단위, 재분석 포함) — 관리자 전용."""
    pool = get_pool()
    if start_date and end_date:
        try:
            start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
            end   = datetime.fromisoformat(end_date).replace(
                hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc,
            )
            period = "custom"
        except ValueError:
            start, end = _period_range(period)
    else:
        start, end = _period_range(period)

    # phase별 합산 → run_id 단위로 JSONB 집계
    rows = await pool.fetch(
        """
        WITH phase_totals AS (
            SELECT
                l.run_id,
                l.doc_id,
                MIN(l.run_at)    AS run_at,
                l.phase,
                MAX(l.model)     AS model,
                SUM(l.input_tok) AS input_tok,
                SUM(l.output_tok) AS output_tok,
                SUM(l.cache_read) AS cache_read,
                SUM(l.cache_write) AS cache_write
            FROM v3_usage_log l
            WHERE l.run_at >= $1 AND l.run_at < $2
            GROUP BY l.run_id, l.doc_id, l.phase
        )
        SELECT
            pt.run_id,
            pt.doc_id,
            MIN(pt.run_at)                       AS run_at,
            d.pdf_filename,
            d.status,
            d.confirmed_at,
            d.pages_count,
            u.username                           AS uploader_username,
            u.display_name_ja                    AS uploader_name_ja,
            u.display_name                       AS uploader_name,
            jsonb_object_agg(
                pt.phase,
                jsonb_build_object(
                    'input',       pt.input_tok,
                    'output',      pt.output_tok,
                    'model',       pt.model,
                    'cache_read',  pt.cache_read,
                    'cache_write', pt.cache_write
                )
            ) AS phases
        FROM phase_totals pt
        LEFT JOIN v3_documents d ON d.doc_id = pt.doc_id
        LEFT JOIN users u ON u.user_id = d.uploaded_by
        GROUP BY pt.run_id, pt.doc_id,
                 d.pdf_filename, d.status, d.confirmed_at, d.pages_count,
                 u.username, u.display_name_ja, u.display_name
        ORDER BY MIN(pt.run_at) DESC
        """,
        start,
        end,
    )

    runs = []
    for r in rows:
        d = dict(r)
        phases = d.get("phases")
        if isinstance(phases, str):
            d["phases"] = _json.loads(phases)
        elif phases is None:
            d["phases"] = {}
        for k in ("run_at", "confirmed_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        runs.append(d)

    return {
        "runs": runs,
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
