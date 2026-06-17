"""DB 쿼리 모음 — S3 JSON 기반 구현 (PostgreSQL 제거).

데이터 모델:
  documents/{doc_id}/meta.json     — 문서 상태·에러·토큰·run_id·confirmed_at
  documents/{doc_id}/mappings.json — 매핑 목록 (id 필드로 순서 보장)
  documents/{doc_id}/reviews.json  — 리뷰 목록
  config/users.json                — 사용자 목록
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import uuid
from datetime import datetime, timezone

log = logging.getLogger(__name__)

from ..core.s3_store import (
    delete_key,
    get_doc_lock,
    list_doc_ids,
    mappings_key,
    meta_key,
    read_json,
    reviews_key,
    write_json,
)


# ── 유틸리티 ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_meta(doc_id: str) -> dict:
    return read_json(meta_key(doc_id)) or {}


async def _arw_meta(doc_id: str, fn) -> None:
    """S3 meta.json을 스레드풀에서 읽고 fn으로 수정한 뒤 저장.
    이벤트 루프를 블로킹하지 않는다.
    호출 전에 get_doc_lock(doc_id)를 보유해야 한다.
    """
    meta = await asyncio.to_thread(read_json, meta_key(doc_id)) or {}
    fn(meta)
    await asyncio.to_thread(write_json, meta_key(doc_id), meta)


def _read_mappings(doc_id: str) -> list[dict]:
    return read_json(mappings_key(doc_id)) or []


def _read_reviews(doc_id: str) -> list[dict]:
    return read_json(reviews_key(doc_id)) or []


def _read_users() -> list[dict]:
    return read_json("config/users.json") or []


def _write_users(users: list[dict]) -> None:
    write_json("config/users.json", users)


def _find_user(users: list[dict], **kwargs) -> dict | None:
    for u in users:
        if all(u.get(k) == v for k, v in kwargs.items()):
            return u
    return None


# ── 문서 상태 ──────────────────────────────────────────────────────────────────

async def update_document_status(doc_id: str, status: str) -> None:
    _status = status
    async with get_doc_lock(doc_id):
        await _arw_meta(doc_id, lambda m: m.update({"status": _status, "updated_at": _now_iso()}))


async def update_document_error(
    doc_id: str, error_type: str, error_phase: str, message: str
) -> None:
    _et, _ep, _em = error_type, error_phase, message
    async with get_doc_lock(doc_id):
        await _arw_meta(doc_id, lambda m: m.update({
            "status": "error",
            "error_type": _et,
            "error_phase": _ep,
            "error_message": _em,
            "updated_at": _now_iso(),
        }))


async def get_document(doc_id: str) -> dict | None:
    meta = await asyncio.to_thread(read_json, meta_key(doc_id))
    if meta is None:
        return None
    meta.setdefault("token_usage", {})
    return meta


async def list_documents() -> list[dict]:
    import asyncio as _aio
    doc_ids = list_doc_ids()

    async def _fetch_one(doc_id: str) -> dict | None:
        meta = await _aio.to_thread(read_json, meta_key(doc_id))
        if meta is None:
            return None
        if not meta.get("doc_id"):
            log.warning("list_documents: doc_id 없는 meta 스킵 (S3 key: documents/%s/meta.json)", doc_id)
            return None
        meta.setdefault("token_usage", {})
        return meta

    results = await _aio.gather(*[_fetch_one(d) for d in doc_ids])
    docs = [m for m in results if m is not None]
    docs.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return docs


# ── 매핑 ───────────────────────────────────────────────────────────────────────

async def save_pending_mappings(doc_id: str, pending: list[dict]) -> None:
    async with get_doc_lock(doc_id):
        existing = await asyncio.to_thread(_read_mappings, doc_id)
        # 확정된 매핑(confirmed_code 있음)만 보존
        confirmed = [m for m in existing if m.get("confirmed_code")]
        next_id = max((m["id"] for m in confirmed), default=0) + 1

        new_items = []
        for p in pending:
            new_items.append({
                "id": next_id,
                "mapping_type": p["mapping_type"],
                "ocr_name": p["ocrName"],
                "candidates": p.get("candidates", []),
                "page_number": p.get("page_number"),
                "confirmed_code": None,
                "confirmed_name": None,
                "confirmed_by": None,
                "confirmed_at": None,
            })
            next_id += 1

        _count = sum(1 for m in confirmed + new_items if not m.get("confirmed_code"))
        await asyncio.to_thread(write_json, mappings_key(doc_id), confirmed + new_items)
        await _arw_meta(doc_id, lambda m: m.update({"pending_count": _count}))


async def has_pending_mappings(doc_id: str) -> bool:
    mappings = await asyncio.to_thread(_read_mappings, doc_id)
    return any(not m.get("confirmed_code") for m in mappings)


def _normalize_candidate(c: dict, mapping_type: str) -> dict:
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
    mappings = await asyncio.to_thread(_read_mappings, doc_id)
    result = []
    for m in mappings:
        if m.get("confirmed_code"):
            continue
        m = dict(m)
        m["candidates"] = [_normalize_candidate(c, m["mapping_type"]) for c in m.get("candidates", [])]
        result.append(m)
    return result


async def get_all_mappings(doc_id: str) -> list[dict]:
    mappings = await asyncio.to_thread(_read_mappings, doc_id)
    result = []
    for m in mappings:
        m = dict(m)
        m["candidates"] = [_normalize_candidate(c, m["mapping_type"]) for c in m.get("candidates", [])]
        result.append(m)
    return result


async def confirm_mapping(doc_id: str, mapping_id: int, confirmed_code: str, confirmed_name: str, user_id: int) -> dict | None:
    async with get_doc_lock(doc_id):
        mappings = await asyncio.to_thread(_read_mappings, doc_id)
        for m in mappings:
            if m["id"] == mapping_id:
                m["confirmed_code"] = confirmed_code
                m["confirmed_name"] = confirmed_name
                m["confirmed_by"] = user_id
                m["confirmed_at"] = _now_iso()
                await asyncio.to_thread(write_json, mappings_key(doc_id), mappings)
                # meta.pending_count 갱신
                _count = sum(1 for x in mappings if not x.get("confirmed_code"))
                await _arw_meta(doc_id, lambda meta: meta.update({"pending_count": _count}))
                # Sheets 캐시 업데이트 (다음 문서에서 자동 매핑에 활용)
                _write_mapping_cache(m["mapping_type"], m["ocr_name"], confirmed_code, confirmed_name, doc_id)
                return {"mapping_type": m["mapping_type"], "ocr_name": m["ocr_name"]}
    return None


def _write_mapping_cache(
    mapping_type: str, ocr_name: str, confirmed_code: str, confirmed_name: str, doc_id: str = ""
) -> None:
    """확정된 매핑을 Sheets 캐시 탭에 기록."""
    try:
        from ..core.sheets_store import get_sheets_store
        store = get_sheets_store()
        if store is None:
            return
        if mapping_type == "retailer":
            store.append_row("ocr_retailer.csv", [ocr_name, confirmed_code, confirmed_name])
        elif mapping_type == "product":
            store.append_row("ocr_product.csv", [ocr_name, confirmed_code, confirmed_name])
        elif mapping_type == "dist" and doc_id:
            try:
                import json as _json_m
                from ..core.config import get_settings
                from ..pipeline.phase3 import _build_issuer_fingerprint, _parse_fingerprint_fields
                _s = get_settings()
                p3_path = _s.extracted_dir / doc_id / "phase3_output.json"
                if p3_path.exists():
                    p3 = _json_m.loads(p3_path.read_text(encoding="utf-8"))
                    form_id = p3.get("form_id", "")
                    form_path = _s.form_definitions_dir / f"{form_id}.md"
                    form_md = form_path.read_text(encoding="utf-8") if form_path.exists() else ""
                    issuer_fp = _build_issuer_fingerprint(
                        p3.get("issuer", {}), _parse_fingerprint_fields(form_md)
                    )
                    rc = (p3.get("confirmed_retailers", {}).get(ocr_name) or {}).get("retailer_code", "")
                    if rc and form_id and issuer_fp:
                        # ocr_dist 키에 jisho 컬럼(4번째) 추가됨 → jisho="" 로 컬럼 정합성 유지
                        store.append_row("ocr_dist.csv", [form_id, issuer_fp, rc, "", confirmed_code, confirmed_name])
            except Exception:
                pass
    except Exception:
        pass  # Sheets 쓰기 실패는 무시 (캐시는 best-effort)



async def upsert_remap_mapping(
    doc_id: str, mapping_type: str, ocr_name: str,
    confirmed_code: str, confirmed_name: str, user_id: int,
) -> None:
    async with get_doc_lock(doc_id):
        mappings = await asyncio.to_thread(_read_mappings, doc_id)
        for m in mappings:
            if m["mapping_type"] == mapping_type and m["ocr_name"] == ocr_name:
                m["confirmed_code"] = confirmed_code
                m["confirmed_name"] = confirmed_name
                m["confirmed_by"] = user_id
                m["confirmed_at"] = _now_iso()
                await asyncio.to_thread(write_json, mappings_key(doc_id), mappings)
                # meta.pending_count 갱신
                _count = sum(1 for x in mappings if not x.get("confirmed_code"))
                await _arw_meta(doc_id, lambda meta: meta.update({"pending_count": _count}))
                return
        # 없으면 새로 추가
        next_id = max((m["id"] for m in mappings), default=0) + 1
        mappings.append({
            "id": next_id,
            "mapping_type": mapping_type,
            "ocr_name": ocr_name,
            "candidates": [],
            "page_number": None,
            "confirmed_code": confirmed_code,
            "confirmed_name": confirmed_name,
            "confirmed_by": user_id,
            "confirmed_at": _now_iso(),
        })
        await asyncio.to_thread(write_json, mappings_key(doc_id), mappings)


# ── 토큰 사용량 ────────────────────────────────────────────────────────────────

async def accumulate_token_usage(
    doc_id: str, phase: str, input_tokens: int, output_tokens: int, model: str,
    cache_read_tokens: int = 0, cache_creation_tokens: int = 0,
    run_id: str = "",
) -> None:
    _phase = phase; _in = input_tokens; _out = output_tokens; _model = model
    _cr = cache_read_tokens; _cc = cache_creation_tokens; _rid = run_id

    def _update(meta):
        if not meta.get("doc_id"):
            return
        token_usage = meta.get("token_usage") or {}
        existing = token_usage.get(_phase, {})
        token_usage[_phase] = {
            "input":          (existing.get("input")          or 0) + _in,
            "output":         (existing.get("output")         or 0) + _out,
            "cache_read":     (existing.get("cache_read")     or 0) + _cr,
            "cache_creation": (existing.get("cache_creation") or 0) + _cc,
            "model":          _model,
        }
        meta["token_usage"] = token_usage
        meta["updated_at"] = _now_iso()
        if _rid:
            usage_log = meta.setdefault("usage_log", [])
            usage_log.append({
                "run_id":      _rid,
                "phase":       _phase,
                "model":       _model,
                "input_tok":   _in,
                "output_tok":  _out,
                "cache_read":  _cr,
                "cache_write": _cc,
                "recorded_at": _now_iso(),
            })

    async with get_doc_lock(doc_id):
        await _arw_meta(doc_id, _update)


async def set_current_run_id(doc_id: str, run_id: str) -> None:
    _rid = run_id
    async with get_doc_lock(doc_id):
        await _arw_meta(doc_id, lambda m: m.update({"current_run_id": _rid}))


async def get_current_run_id(doc_id: str) -> str:
    meta = await asyncio.to_thread(read_json, meta_key(doc_id)) or {}
    return meta.get("current_run_id") or ""


# ── 확정 ───────────────────────────────────────────────────────────────────────

async def get_document_confirmed(doc_id: str) -> bool:
    meta = await asyncio.to_thread(_read_meta, doc_id)
    return bool(meta.get("confirmed_at"))


async def set_confirmed(doc_id: str) -> None:
    _now = _now_iso()
    async with get_doc_lock(doc_id):
        await _arw_meta(doc_id, lambda m: m.update({"confirmed_at": _now, "updated_at": _now}))


async def unset_confirmed(doc_id: str) -> None:
    async with get_doc_lock(doc_id):
        await _arw_meta(doc_id, lambda m: m.update({"confirmed_at": None, "updated_at": _now_iso()}))


# ── 리뷰 ───────────────────────────────────────────────────────────────────────

async def upsert_review(doc_id: str, retailer_code: str, review_type: str, reviewer_id: int) -> dict:
    users = await asyncio.to_thread(_read_users)
    reviewer = _find_user(users, user_id=reviewer_id) or {}

    async with get_doc_lock(doc_id):
        reviews = await asyncio.to_thread(_read_reviews, doc_id)
        existing = next(
            (r for r in reviews if r["retailer_code"] == retailer_code and r["review_type"] == review_type),
            None,
        )
        now = _now_iso()
        if existing:
            existing["reviewer_id"] = reviewer_id
            existing["reviewer_name"] = reviewer.get("display_name")
            existing["reviewer_name_ja"] = reviewer.get("display_name_ja")
            existing["reviewer_username"] = reviewer.get("username")
            existing["reviewed_at"] = now
            await asyncio.to_thread(write_json, reviews_key(doc_id), reviews)
            return dict(existing)
        else:
            new_review = {
                "id": str(uuid.uuid4()),
                "doc_id": doc_id,
                "retailer_code": retailer_code,
                "review_type": review_type,
                "reviewer_id": reviewer_id,
                "reviewer_name": reviewer.get("display_name"),
                "reviewer_name_ja": reviewer.get("display_name_ja"),
                "reviewer_username": reviewer.get("username"),
                "reviewed_at": now,
            }
            reviews.append(new_review)
            await asyncio.to_thread(write_json, reviews_key(doc_id), reviews)
            return dict(new_review)


async def delete_review(doc_id: str, retailer_code: str, review_type: str, reviewer_id: int) -> str:
    """'ok' | 'not_found' | 'not_owner'"""
    async with get_doc_lock(doc_id):
        reviews = await asyncio.to_thread(_read_reviews, doc_id)
        idx = next(
            (i for i, r in enumerate(reviews) if r["retailer_code"] == retailer_code and r["review_type"] == review_type),
            None,
        )
        if idx is None:
            return "not_found"
        if reviews[idx]["reviewer_id"] != reviewer_id:
            return "not_owner"
        reviews.pop(idx)
        await asyncio.to_thread(write_json, reviews_key(doc_id), reviews)
        return "ok"


async def get_reviews(doc_id: str) -> list[dict]:
    reviews = await asyncio.to_thread(_read_reviews, doc_id)
    reviews.sort(key=lambda r: (r.get("review_type", ""), r.get("retailer_code", "")))
    return reviews


# ── 문서 생성·삭제·재설정 ──────────────────────────────────────────────────────

async def create_document(
    doc_id: str,
    pdf_filename: str,
    hatsu_month: str,
    user_id: int,
    uploaded_by_username: str = "",
    uploaded_by_name_ja: str = "",
) -> None:
    """문서 신규 등록 (upsert — error 상태에서 재시도 허용)."""
    async with get_doc_lock(doc_id):
        now = _now_iso()
        existing = await asyncio.to_thread(_read_meta, doc_id)
        meta = {
            **existing,  # 기존 값이 있으면 보존 (재시도 시)
            "doc_id": doc_id,
            "pdf_filename": pdf_filename,
            "hatsu_month": hatsu_month or None,
            "status": "ocr",
            "error_type": None,
            "error_phase": None,
            "error_message": None,
            "form_id": None,
            "token_usage": {},
            "current_run_id": "",
            "confirmed_at": None,
            "pending_count": 0,
            "uploaded_by": user_id,
            "uploaded_by_username": uploaded_by_username,
            "uploaded_by_name_ja": uploaded_by_name_ja,
            "analysis_started_at": now,
            "updated_at": now,
        }
        if not existing:
            meta["created_at"] = now
        await asyncio.to_thread(write_json, meta_key(doc_id), meta)


async def reset_document_for_retry(doc_id: str) -> None:
    """재분석 시 상태·에러·토큰 초기화."""
    async with get_doc_lock(doc_id):
        await _arw_meta(doc_id, lambda m: m.update({
            "status": "ocr", "error_type": None, "error_phase": None,
            "error_message": None, "token_usage": {}, "phase_timings": {},
            "analysis_started_at": _now_iso(), "updated_at": _now_iso(),
        }))


async def clear_mappings(doc_id: str) -> None:
    """매핑 전체 삭제 (재분석 시)."""
    async with get_doc_lock(doc_id):
        await asyncio.to_thread(write_json, mappings_key(doc_id), [])
        await _arw_meta(doc_id, lambda m: m.update({"pending_count": 0}))


async def set_form_id(doc_id: str, form_id: str) -> None:
    _fid = form_id
    async with get_doc_lock(doc_id):
        await _arw_meta(doc_id, lambda m: m.update({"form_id": _fid, "updated_at": _now_iso()}))


async def set_pipeline_hashes(doc_id: str, hashes: dict) -> None:
    """파이프라인 실행 시점의 form 정의·form_types·프롬프트 해시 묶음을 meta에 기록.
    재분석 시 어떤 규칙 버전으로 계산됐는지 추적·비교하는 용도."""
    _h = {**hashes, "recorded_at": _now_iso()}
    async with get_doc_lock(doc_id):
        await _arw_meta(doc_id, lambda m: m.update({"pipeline_hashes": _h}))


async def set_pages_count(doc_id: str, pages_count: int) -> None:
    _pc = pages_count
    async with get_doc_lock(doc_id):
        await _arw_meta(doc_id, lambda m: m.update({"pages_count": _pc, "updated_at": _now_iso()}))


async def record_phase_timing(doc_id: str, phase: str, duration_sec: float) -> None:
    _phase = phase; _dur = round(duration_sec, 1)

    def _update(meta):
        if not meta.get("doc_id"):
            return
        meta.setdefault("phase_timings", {})[_phase] = _dur
        meta["updated_at"] = _now_iso()

    async with get_doc_lock(doc_id):
        await _arw_meta(doc_id, _update)


async def save_phase3_tool_use_stats(doc_id: str, stats: dict) -> None:
    _stats = stats

    def _update(meta):
        if not meta.get("doc_id"):
            return
        meta["phase3_tool_use_stats"] = _stats
        meta["updated_at"] = _now_iso()

    async with get_doc_lock(doc_id):
        await _arw_meta(doc_id, _update)


async def delete_document_data(doc_id: str) -> None:
    """S3에서 문서 관련 JSON 모두 삭제."""
    from ..core.s3_store import list_keys, delete_key
    for key in list_keys(f"documents/{doc_id}/"):
        delete_key(key)


async def get_user_password_hash(user_id: int) -> str | None:
    users = _read_users()
    user = _find_user(users, user_id=user_id)
    return user.get("password_hash") if user else None
