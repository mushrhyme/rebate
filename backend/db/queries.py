"""DB 쿼리 모음 — S3 JSON 기반 구현 (PostgreSQL 제거).

데이터 모델:
  documents/{doc_id}/meta.json     — 문서 상태·에러·토큰·run_id·confirmed_at
  documents/{doc_id}/mappings.json — 매핑 목록 (id 필드로 순서 보장)
  documents/{doc_id}/reviews.json  — 리뷰 목록
  config/users.json                — 사용자 목록
"""
from __future__ import annotations

import json as _json
import uuid
from datetime import datetime, timezone

from ..core.s3_store import (
    delete_key,
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
    meta = _read_meta(doc_id)
    meta["status"] = status
    meta["updated_at"] = _now_iso()
    write_json(meta_key(doc_id), meta)


async def update_document_error(
    doc_id: str, error_type: str, error_phase: str, message: str
) -> None:
    meta = _read_meta(doc_id)
    meta["status"] = "error"
    meta["error_type"] = error_type
    meta["error_phase"] = error_phase
    meta["error_message"] = message
    meta["updated_at"] = _now_iso()
    write_json(meta_key(doc_id), meta)


async def get_document(doc_id: str) -> dict | None:
    meta = read_json(meta_key(doc_id))
    if meta is None:
        return None
    meta.setdefault("token_usage", {})
    return meta


async def list_documents() -> list[dict]:
    doc_ids = list_doc_ids()
    docs = []
    for doc_id in doc_ids:
        meta = read_json(meta_key(doc_id))
        if meta is None:
            continue
        meta.setdefault("token_usage", {})
        # pending_count는 meta에 캐시된 값 사용 (save_pending_mappings에서 업데이트)
        docs.append(meta)
    docs.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return docs


# ── 매핑 ───────────────────────────────────────────────────────────────────────

async def save_pending_mappings(doc_id: str, pending: list[dict]) -> None:
    existing = _read_mappings(doc_id)
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

    all_mappings = confirmed + new_items
    write_json(mappings_key(doc_id), all_mappings)

    # meta.pending_count 갱신
    meta = _read_meta(doc_id)
    meta["pending_count"] = sum(1 for m in all_mappings if not m.get("confirmed_code"))
    write_json(meta_key(doc_id), meta)


async def has_pending_mappings(doc_id: str) -> bool:
    mappings = _read_mappings(doc_id)
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
    mappings = _read_mappings(doc_id)
    result = []
    for m in mappings:
        if m.get("confirmed_code"):
            continue
        m = dict(m)
        m["candidates"] = [_normalize_candidate(c, m["mapping_type"]) for c in m.get("candidates", [])]
        result.append(m)
    return result


async def get_all_mappings(doc_id: str) -> list[dict]:
    mappings = _read_mappings(doc_id)
    result = []
    for m in mappings:
        m = dict(m)
        m["candidates"] = [_normalize_candidate(c, m["mapping_type"]) for c in m.get("candidates", [])]
        result.append(m)
    return result


async def confirm_mapping(mapping_id: int, confirmed_code: str, confirmed_name: str, user_id: int) -> dict | None:
    doc_id = _find_doc_by_mapping_id(mapping_id)
    mappings = _read_mappings(doc_id)
    for m in mappings:
        if m["id"] == mapping_id:
            m["confirmed_code"] = confirmed_code
            m["confirmed_name"] = confirmed_name
            m["confirmed_by"] = user_id
            m["confirmed_at"] = _now_iso()
            write_json(mappings_key(doc_id), mappings)
            # meta.pending_count 갱신
            meta = _read_meta(doc_id)
            meta["pending_count"] = sum(1 for x in mappings if not x.get("confirmed_code"))
            write_json(meta_key(doc_id), meta)
            return {"mapping_type": m["mapping_type"], "ocr_name": m["ocr_name"]}
    return None


def _find_doc_by_mapping_id(mapping_id: int) -> str:
    """mapping_id로 doc_id 찾기 — 모든 문서의 mappings.json 검색."""
    for doc_id in list_doc_ids():
        mappings = _read_mappings(doc_id)
        if any(m["id"] == mapping_id for m in mappings):
            return doc_id
    raise ValueError(f"mapping_id {mapping_id} not found")


async def upsert_remap_mapping(
    doc_id: str, mapping_type: str, ocr_name: str,
    confirmed_code: str, confirmed_name: str, user_id: int,
) -> None:
    mappings = _read_mappings(doc_id)
    for m in mappings:
        if m["mapping_type"] == mapping_type and m["ocr_name"] == ocr_name:
            m["confirmed_code"] = confirmed_code
            m["confirmed_name"] = confirmed_name
            m["confirmed_by"] = user_id
            m["confirmed_at"] = _now_iso()
            write_json(mappings_key(doc_id), mappings)
            # meta.pending_count 갱신
            meta = _read_meta(doc_id)
            meta["pending_count"] = sum(1 for x in mappings if not x.get("confirmed_code"))
            write_json(meta_key(doc_id), meta)
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
    write_json(mappings_key(doc_id), mappings)


# ── 토큰 사용량 ────────────────────────────────────────────────────────────────

async def accumulate_token_usage(
    doc_id: str, phase: str, input_tokens: int, output_tokens: int, model: str,
    cache_read_tokens: int = 0, cache_creation_tokens: int = 0,
    run_id: str = "",
) -> None:
    meta = _read_meta(doc_id)
    token_usage = meta.get("token_usage") or {}
    data: dict = {"input": input_tokens, "output": output_tokens, "model": model}
    if cache_read_tokens:
        data["cache_read"] = cache_read_tokens
    if cache_creation_tokens:
        data["cache_creation"] = cache_creation_tokens
    token_usage[phase] = data
    meta["token_usage"] = token_usage
    meta["updated_at"] = _now_iso()

    if run_id:
        # usage_log는 meta에 리스트로 누적 (선택적)
        log = meta.setdefault("usage_log", [])
        log.append({
            "run_id": run_id,
            "phase": phase,
            "model": model,
            "input_tok": input_tokens,
            "output_tok": output_tokens,
            "cache_read": cache_read_tokens,
            "cache_write": cache_creation_tokens,
            "recorded_at": _now_iso(),
        })

    write_json(meta_key(doc_id), meta)


async def set_current_run_id(doc_id: str, run_id: str) -> None:
    meta = _read_meta(doc_id)
    meta["current_run_id"] = run_id
    write_json(meta_key(doc_id), meta)


async def get_current_run_id(doc_id: str) -> str:
    meta = _read_meta(doc_id)
    return meta.get("current_run_id") or ""


# ── 확정 ───────────────────────────────────────────────────────────────────────

async def get_document_confirmed(doc_id: str) -> bool:
    meta = _read_meta(doc_id)
    return bool(meta.get("confirmed_at"))


async def set_confirmed(doc_id: str) -> None:
    meta = _read_meta(doc_id)
    meta["confirmed_at"] = _now_iso()
    meta["updated_at"] = _now_iso()
    write_json(meta_key(doc_id), meta)


async def unset_confirmed(doc_id: str) -> None:
    meta = _read_meta(doc_id)
    meta["confirmed_at"] = None
    meta["updated_at"] = _now_iso()
    write_json(meta_key(doc_id), meta)


# ── 리뷰 ───────────────────────────────────────────────────────────────────────

async def upsert_review(doc_id: str, retailer_code: str, review_type: str, reviewer_id: int) -> dict:
    users = _read_users()
    reviewer = _find_user(users, user_id=reviewer_id) or {}

    reviews = _read_reviews(doc_id)
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
        write_json(reviews_key(doc_id), reviews)
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
        write_json(reviews_key(doc_id), reviews)
        return dict(new_review)


async def delete_review(doc_id: str, retailer_code: str, review_type: str, reviewer_id: int) -> str:
    """'ok' | 'not_found' | 'not_owner'"""
    reviews = _read_reviews(doc_id)
    idx = next(
        (i for i, r in enumerate(reviews) if r["retailer_code"] == retailer_code and r["review_type"] == review_type),
        None,
    )
    if idx is None:
        return "not_found"
    if reviews[idx]["reviewer_id"] != reviewer_id:
        return "not_owner"
    reviews.pop(idx)
    write_json(reviews_key(doc_id), reviews)
    return "ok"


async def get_reviews(doc_id: str) -> list[dict]:
    reviews = _read_reviews(doc_id)
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
    now = _now_iso()
    existing = _read_meta(doc_id)
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
    write_json(meta_key(doc_id), meta)


async def reset_document_for_retry(doc_id: str) -> None:
    """재분석 시 상태·에러·토큰 초기화."""
    meta = _read_meta(doc_id)
    meta["status"] = "ocr"
    meta["error_type"] = None
    meta["error_phase"] = None
    meta["error_message"] = None
    meta["token_usage"] = {}
    meta["analysis_started_at"] = _now_iso()
    meta["updated_at"] = _now_iso()
    write_json(meta_key(doc_id), meta)


async def clear_mappings(doc_id: str) -> None:
    """매핑 전체 삭제 (재분석 시)."""
    write_json(mappings_key(doc_id), [])
    meta = _read_meta(doc_id)
    meta["pending_count"] = 0
    write_json(meta_key(doc_id), meta)


async def set_form_id(doc_id: str, form_id: str) -> None:
    meta = _read_meta(doc_id)
    meta["form_id"] = form_id
    meta["updated_at"] = _now_iso()
    write_json(meta_key(doc_id), meta)


async def set_pages_count(doc_id: str, pages_count: int) -> None:
    meta = _read_meta(doc_id)
    meta["pages_count"] = pages_count
    meta["updated_at"] = _now_iso()
    write_json(meta_key(doc_id), meta)


async def delete_document_data(doc_id: str) -> None:
    """S3에서 문서 관련 JSON 모두 삭제."""
    from ..core.s3_store import list_keys, delete_key
    for key in list_keys(f"documents/{doc_id}/"):
        delete_key(key)


async def get_user_password_hash(user_id: int) -> str | None:
    users = _read_users()
    user = _find_user(users, user_id=user_id)
    return user.get("password_hash") if user else None
