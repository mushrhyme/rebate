"""DB 쿼리 모음."""
import json as _json

from ..core.database import get_pool


async def update_document_status(doc_id: str, status: str) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE v3_documents SET status = $1, updated_at = NOW() WHERE doc_id = $2",
        status, doc_id,
    )


async def update_document_error(
    doc_id: str, error_type: str, error_phase: str, message: str
) -> None:
    pool = get_pool()
    await pool.execute(
        """UPDATE v3_documents
           SET status = 'error', error_type = $1, error_phase = $2, error_message = $3, updated_at = NOW()
           WHERE doc_id = $4""",
        error_type, error_phase, message, doc_id,
    )


async def save_pending_mappings(doc_id: str, pending: list[dict]) -> None:
    pool = get_pool()
    import json
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 재실행 시 누적 방지: 미확정 매핑만 삭제하고 재삽입 (확정된 건은 보존)
            await conn.execute(
                "DELETE FROM v3_mappings WHERE doc_id = $1 AND confirmed_code IS NULL",
                doc_id,
            )
            await conn.executemany(
                """INSERT INTO v3_mappings (doc_id, mapping_type, ocr_name, candidates, page_number)
                   VALUES ($1, $2, $3, $4, $5)""",
                [
                    (
                        doc_id,
                        p["mapping_type"],
                        p["ocrName"],
                        json.dumps(p.get("candidates", []), ensure_ascii=False),
                        p.get("page_number"),
                    )
                    for p in pending
                ],
            )


async def has_pending_mappings(doc_id: str) -> bool:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM v3_mappings WHERE doc_id = $1 AND confirmed_code IS NULL",
        doc_id,
    )
    return row["cnt"] > 0


async def get_document(doc_id: str) -> dict | None:
    pool = get_pool()
    row = await pool.fetchrow("SELECT * FROM v3_documents WHERE doc_id = $1", doc_id)
    return _parse_token_usage(dict(row)) if row else None


def _parse_token_usage(d: dict) -> dict:
    tu = d.get("token_usage")
    if isinstance(tu, str):
        d["token_usage"] = _json.loads(tu)
    elif tu is None:
        d["token_usage"] = {}
    return d


async def list_documents() -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT d.*,
               u.username        AS uploaded_by_username,
               u.display_name_ja AS uploaded_by_name_ja,
               COUNT(m.id) FILTER (WHERE m.confirmed_code IS NULL) AS pending_count
        FROM v3_documents d
        LEFT JOIN users u ON u.user_id = d.uploaded_by
        LEFT JOIN v3_mappings m ON m.doc_id = d.doc_id
        GROUP BY d.doc_id, u.username, u.display_name_ja
        ORDER BY d.created_at DESC
        """
    )
    return [_parse_token_usage(dict(r)) for r in rows]


def _normalize_candidate(c: dict, mapping_type: str) -> dict:
    """DB에 저장된 candidates 키(product_code/retailer_code/dist_code)를 프론트가 기대하는 code/name으로 통일."""
    if mapping_type == "product":
        return {
            "code": c.get("product_code") or c.get("code", ""),
            "name": c.get("name", ""),
            **{k: v for k, v in c.items() if k not in ("product_code", "code", "name")},
        }
    if mapping_type == "retailer":
        return {
            "code": c.get("retailer_code") or c.get("code", ""),
            "name": c.get("name", ""),
            **{k: v for k, v in c.items() if k not in ("retailer_code", "code", "name")},
        }
    if mapping_type == "dist":
        return {
            "code": c.get("dist_code") or c.get("code", ""),
            "name": c.get("dist_name") or c.get("name", ""),
            **{k: v for k, v in c.items() if k not in ("dist_code", "dist_name", "code", "name")},
        }
    return c


async def get_pending_mappings(doc_id: str) -> list[dict]:
    pool = get_pool()
    import json as _json
    rows = await pool.fetch(
        "SELECT * FROM v3_mappings WHERE doc_id = $1 AND confirmed_code IS NULL ORDER BY id",
        doc_id,
    )
    results = []
    for r in rows:
        d = dict(r)
        d["candidates"] = _json.loads(d["candidates"]) if isinstance(d["candidates"], str) else d["candidates"]
        d["candidates"] = [_normalize_candidate(c, d["mapping_type"]) for c in d["candidates"]]
        results.append(d)
    return results


async def get_all_mappings(doc_id: str) -> list[dict]:
    """확정 여부와 무관하게 문서의 모든 매핑 항목 반환."""
    pool = get_pool()
    import json as _json
    rows = await pool.fetch(
        "SELECT * FROM v3_mappings WHERE doc_id = $1 ORDER BY id",
        doc_id,
    )
    results = []
    for r in rows:
        d = dict(r)
        d["candidates"] = _json.loads(d["candidates"]) if isinstance(d["candidates"], str) else d["candidates"]
        d["candidates"] = [_normalize_candidate(c, d["mapping_type"]) for c in d["candidates"]]
        results.append(d)
    return results


async def accumulate_token_usage(
    doc_id: str, phase: str, input_tokens: int, output_tokens: int, model: str,
    cache_read_tokens: int = 0, cache_creation_tokens: int = 0,
    run_id: str = "",
) -> None:
    """Phase별 토큰 사용량 기록.
    - v3_documents.token_usage: 최신 실행값으로 덮어씀 (대시보드 표시용)
    - v3_usage_log: 실행마다 INSERT (이력 추적용, run_id 있을 때만)
    """
    pool = get_pool()
    data: dict = {"input": input_tokens, "output": output_tokens, "model": model}
    if cache_read_tokens:
        data["cache_read"] = cache_read_tokens
    if cache_creation_tokens:
        data["cache_creation"] = cache_creation_tokens
    entry = _json.dumps({phase: data})

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE v3_documents
               SET token_usage = token_usage || $1::jsonb,
                   updated_at = NOW()
               WHERE doc_id = $2""",
            entry, doc_id,
        )
        if run_id:
            await conn.execute(
                """INSERT INTO v3_usage_log
                       (doc_id, run_id, phase, model, input_tok, output_tok, cache_read, cache_write)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                doc_id, run_id, phase, model,
                input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
            )


async def set_current_run_id(doc_id: str, run_id: str) -> None:
    """분석 시작 시 현재 run_id를 문서에 저장 (resume_phase4 연결용)."""
    pool = get_pool()
    await pool.execute(
        "UPDATE v3_documents SET current_run_id = $1 WHERE doc_id = $2",
        run_id, doc_id,
    )


async def get_current_run_id(doc_id: str) -> str:
    """현재 문서의 run_id 조회 (resume_phase4에서 phase4 이력 연결용)."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT current_run_id FROM v3_documents WHERE doc_id = $1", doc_id
    )
    return (row["current_run_id"] or "") if row else ""


async def get_document_confirmed(doc_id: str) -> bool:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT confirmed_at FROM v3_documents WHERE doc_id = $1", doc_id
    )
    return bool(row and row["confirmed_at"])


async def set_confirmed(doc_id: str) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE v3_documents SET confirmed_at = NOW(), updated_at = NOW() WHERE doc_id = $1",
        doc_id,
    )


async def unset_confirmed(doc_id: str) -> None:
    pool = get_pool()
    await pool.execute(
        "UPDATE v3_documents SET confirmed_at = NULL, updated_at = NOW() WHERE doc_id = $1",
        doc_id,
    )


async def upsert_review(doc_id: str, retailer_code: str, review_type: str, reviewer_id: int) -> dict:
    pool = get_pool()
    row = await pool.fetchrow(
        """
        WITH ins AS (
            INSERT INTO v3_reviews (doc_id, retailer_code, review_type, reviewer_id, reviewed_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (doc_id, retailer_code, review_type)
            DO UPDATE SET reviewer_id = $4, reviewed_at = NOW()
            RETURNING id, doc_id, retailer_code, review_type, reviewer_id, reviewed_at
        )
        SELECT ins.*, u.display_name AS reviewer_name,
               u.display_name_ja AS reviewer_name_ja,
               u.username AS reviewer_username
        FROM ins
        JOIN users u ON u.user_id = ins.reviewer_id
        """,
        doc_id, retailer_code, review_type, reviewer_id,
    )
    return dict(row)


async def delete_review(doc_id: str, retailer_code: str, review_type: str, reviewer_id: int) -> str:
    """'ok' | 'not_found' | 'not_owner'"""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT reviewer_id FROM v3_reviews WHERE doc_id = $1 AND retailer_code = $2 AND review_type = $3",
            doc_id, retailer_code, review_type,
        )
        if not row:
            return "not_found"
        if row["reviewer_id"] != reviewer_id:
            return "not_owner"
        await conn.execute(
            "DELETE FROM v3_reviews WHERE doc_id = $1 AND retailer_code = $2 AND review_type = $3",
            doc_id, retailer_code, review_type,
        )
        return "ok"


async def get_reviews(doc_id: str) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT r.id, r.doc_id, r.retailer_code, r.review_type,
               r.reviewer_id, r.reviewed_at,
               u.display_name AS reviewer_name,
               u.display_name_ja AS reviewer_name_ja,
               u.username AS reviewer_username
        FROM v3_reviews r
        LEFT JOIN users u ON u.user_id = r.reviewer_id
        WHERE r.doc_id = $1
        ORDER BY r.review_type, r.retailer_code
        """,
        doc_id,
    )
    return [dict(r) for r in rows]


async def upsert_remap_mapping(
    doc_id: str, mapping_type: str, ocr_name: str,
    confirmed_code: str, confirmed_name: str, user_id: int,
) -> None:
    """결과 화면에서 매핑 수정 시 v3_mappings를 upsert (pending 여부 무관)."""
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO v3_mappings (doc_id, mapping_type, ocr_name, candidates,
                                 confirmed_code, confirmed_name, confirmed_by, confirmed_at)
        VALUES ($1, $2, $3, '[]', $4, $5, $6, NOW())
        ON CONFLICT (doc_id, mapping_type, ocr_name)
        DO UPDATE SET confirmed_code = $4, confirmed_name = $5,
                      confirmed_by = $6, confirmed_at = NOW()
        """,
        doc_id, mapping_type, ocr_name, confirmed_code, confirmed_name, user_id,
    )


async def confirm_mapping(mapping_id: int, confirmed_code: str, confirmed_name: str, user_id: int) -> dict | None:
    """매핑 확정. 확정된 행(mapping_type, ocr_name 포함)을 반환 — CSV 캐시 쓰기용."""
    pool = get_pool()
    row = await pool.fetchrow(
        """UPDATE v3_mappings
           SET confirmed_code = $1, confirmed_name = $2, confirmed_by = $3, confirmed_at = NOW()
           WHERE id = $4
           RETURNING mapping_type, ocr_name""",
        confirmed_code, confirmed_name, user_id, mapping_id,
    )
    return dict(row) if row else None
