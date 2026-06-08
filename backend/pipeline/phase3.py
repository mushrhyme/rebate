"""Phase 3 — 소매처·판매처·제품 코드 매핑 (캐시 Python + 판단 Claude).

처리 순서:
  ① Python — 캐시 조회 (ocr_retailer.csv / ocr_product.csv / ocr_dist.csv)
  ② Claude — 캐시 미스 항목을 두 개의 병렬 호출로 처리
              call A: 소매처·판매처 매핑 (form_XX.md의 データソース CSV)
              call B: 제품 매핑 (unit_price.csv만)
  ③ Python — 캐시 저장 + 아이템에 코드 적용 + pending 목록 생성

タイプ분류는 Phase 2가 item_type 필드로 확정해서 넘겨줌.
"""
import asyncio
import json
import logging
from pathlib import Path

import anthropic

from ..core.config import get_settings
from ..tools.claude_retry import call_with_retry
from ..tools.mapping import (
    _read_csv,
    confirm_mapping,
    lookup_retailer,
    normalize_ocr_name,  # re-export: phase3에서 직접 임포트하는 테스트와의 호환성 유지
    parse_retailer_csv_sources,
    search_product,
)

log = logging.getLogger(__name__)

_SYSTEM_PROMPT_CACHE: str | None = None


# ── 캐시 로더 ─────────────────────────────────────────────────────────────────

def _load_dist_cache(path: Path) -> dict[tuple, str]:
    """(form_id, issuer_fingerprint, retailer_code) → dist_code"""
    if not path.exists():
        return {}
    result = {}
    for r in _read_csv(path):
        key = (r.get("form_id", ""), r.get("issuer_fingerprint", ""), r.get("retailer_code", ""))
        result[key] = r["dist_code"]
    return result


# ── fingerprint 헬퍼 ──────────────────────────────────────────────────────────

def _parse_fingerprint_fields(form_md: str) -> list[str]:
    """form_XX.md의 ## issuer 식별 섹션에서 fingerprint_fields 추출."""
    lines = form_md.splitlines()
    in_section = False
    for line in lines:
        if line.strip().startswith("## issuer 식별"):
            in_section = True
            continue
        if in_section:
            if line.startswith("##"):
                break
            stripped = line.strip()
            if stripped.startswith("fingerprint_fields:"):
                fields_str = stripped[len("fingerprint_fields:"):].strip()
                return [f.strip() for f in fields_str.split(",")]
    return ["name"]


def _build_issuer_fingerprint(issuer: dict, fields: list[str]) -> str:
    """fingerprint_fields에 해당하는 issuer 값을 '|' 구분자로 연결."""
    return "|".join(issuer.get(f, "") for f in fields)


# ── 시스템 프롬프트 ────────────────────────────────────────────────────────────

def _get_system_prompt() -> str:
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is None:
        path = get_settings().workspace_root / "docs" / "phase3-prompt.md"
        _SYSTEM_PROMPT_CACHE = path.read_text(encoding="utf-8")
    return _SYSTEM_PROMPT_CACHE


# ── CSV 컨텍스트 구축 ──────────────────────────────────────────────────────────

def _build_retailer_csv_context(form_md: str, mappings_dir: Path) -> str:
    """form_XX.md의 データソース 섹션에 명시된 CSV만 로드 (소매처·판매처 매핑용)."""
    from ..core.sheets_store import get_sheets_store, TAB_MAP
    store = get_sheets_store()
    parts = []
    for fname in parse_retailer_csv_sources(form_md):
        if store and fname in TAB_MAP:
            text = store.to_csv_text(fname)
            if text:
                parts.append(f"### {fname}\n{text}")
        else:
            p = mappings_dir / fname
            if p.exists():
                parts.append(f"### {fname}\n{p.read_text(encoding='utf-8-sig')}")
    return "\n\n".join(parts)


def _build_product_csv_context(mappings_dir: Path) -> str:
    """제품 매핑용 CSV (unit_price.csv만)."""
    from ..core.sheets_store import get_sheets_store
    store = get_sheets_store()
    if store:
        text = store.to_csv_text("unit_price.csv")
        if text:
            return f"### unit_price.csv\n{text}"
    p = mappings_dir / "unit_price.csv"
    return f"### unit_price.csv\n{p.read_text(encoding='utf-8-sig')}" if p.exists() else ""


# ── Claude 공통 호출 ──────────────────────────────────────────────────────────

def _call_claude(
    client: anthropic.Anthropic,
    system: list[dict],
    user_payload: dict,
) -> tuple[dict, int, int, int, int]:
    """Claude API 호출 및 JSON 파싱 공통 로직. retry/backoff 포함."""
    message = call_with_retry(
        client.messages.create,
        model="claude-haiku-4-5-20251001",
        max_tokens=8192,
        system=system,
        messages=[{"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
        timeout=300.0,
    )
    raw = message.content[0].text if message.content else ""
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()
    raw = raw.strip()
    brace = raw.find("{")
    if brace > 0:
        raw = raw[brace:]
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Phase 3 Claude JSON 파싱 실패: {e}\nraw: {raw[:300]}")
    usage = message.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return result, usage.input_tokens, usage.output_tokens, cache_read, cache_creation


def _call_retailer_claude(
    client: anthropic.Anthropic,
    form_md: str,
    issuer: dict,
    uncached_retailers: list[str],
    cached_retailers_needing_dist: list[dict],
    mappings_dir: Path,
) -> tuple[dict, int, int, int, int]:
    """소매처·판매처 매핑 전용 Claude 호출."""
    system = [
        {
            "type": "text",
            "text": _get_system_prompt(),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"## 양식 정의\n\n{form_md}",
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"## CSV 데이터\n\n{_build_retailer_csv_context(form_md, mappings_dir)}",
            "cache_control": {"type": "ephemeral"},
        },
    ]
    user_payload = {
        "issuer": issuer,
        "uncached_retailers": uncached_retailers,
        "cached_retailers_needing_dist": cached_retailers_needing_dist,
        "uncached_products": [],
    }
    return _call_claude(client, system, user_payload)


def _call_product_claude(
    client: anthropic.Anthropic,
    form_md: str,
    uncached_products: list[dict],
    mappings_dir: Path,
) -> tuple[dict, int, int, int, int]:
    """제품 매핑 전용 Claude 호출 (unit_price.csv만 로드)."""
    system = [
        {
            "type": "text",
            "text": _get_system_prompt(),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"## 양식 정의\n\n{form_md}",
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"## CSV 데이터\n\n{_build_product_csv_context(mappings_dir)}",
            "cache_control": {"type": "ephemeral"},
        },
    ]
    user_payload = {
        "issuer": {},
        "uncached_retailers": [],
        "cached_retailers_needing_dist": [],
        "uncached_products": uncached_products,
    }
    return _call_claude(client, system, user_payload)


async def _noop_result() -> tuple[dict, int, int, int, int]:
    return {}, 0, 0, 0, 0


# ── 아이템에 코드 적용 ────────────────────────────────────────────────────────

def _apply_mappings(
    items: list[dict],
    confirmed_retailers: dict[str, dict],
    confirmed_products: dict[str, dict],
) -> list[dict]:
    """phase2 items에 retailer_code / dist_code / product_code 쓰기.
    item_type은 Phase 2가 확정해서 넘겨주므로 읽기만 한다."""
    out = []
    for item in items:
        i = dict(item)
        ocr_customer = i.get("customer", "")
        ocr_product  = i.get("product", "")

        i.setdefault("customer_ocr", ocr_customer)
        i.setdefault("product_ocr",  ocr_product)

        rc = confirmed_retailers.get(ocr_customer, {})
        i["retailer_code"] = rc.get("retailer_code", "")
        i["dist_code"]     = rc.get("dist_code", "")
        i["unconfirmed"]   = not bool(rc.get("retailer_code"))

        pc = confirmed_products.get(ocr_product, {})
        i["product_code"] = pc.get("code") if pc else None

        out.append(i)
    return out


# ── cover totals 추출 (form_id 분기 없음) ────────────────────────────────────

def _extract_cover_totals(phase2_result: dict) -> dict:
    """pages[]에서 cover 페이지 totals를 추출.
    cover가 1개면 dict, 복수면 {"0": ..., "1": ...} — form_id 무관하게 동작."""
    covers = [
        p for p in phase2_result.get("pages", [])
        if p.get("role") == "cover" and p.get("totals")
    ]
    if len(covers) == 1:
        return covers[0]["totals"]
    if len(covers) > 1:
        return {str(i): c["totals"] for i, c in enumerate(covers)}
    return phase2_result.get("cover_totals", {})  # 구 포맷 하위 호환


# ── 공개 진입점 ───────────────────────────────────────────────────────────────

async def run_phase3(
    doc_id: str,
    phase2_result: dict,
    output_dir: Path,
    form_id: str,
    hatsu_month: str = "",
    run_id: str = "",
) -> tuple[dict, list[dict]]:
    """
    Returns:
        phase3_result: 확정 매핑 포함 결과 → phase3_output.json 저장
        pending: NEEDS_CONFIRMATION / NOT_FOUND 항목 목록 (DB 저장용)
    """
    settings = get_settings()
    mappings_dir = settings.mappings_dir

    # form_XX.md 로드 (두 Claude 호출 공통)
    form_path = settings.form_definitions_dir / f"{form_id}.md"
    form_md = form_path.read_text(encoding="utf-8") if form_path.exists() else ""

    # form_types.json에서 1:N 판매처 결정 시 사용할 그룹 식별 필드명 조회
    # cross_validation의 cover_breakdown_vs_detail 타입에 detail_group_field가 정의된 양식만 해당
    _form_types_path = settings.workspace_root / "config" / "form_types.json"
    _dist_group_field: str | None = None
    if _form_types_path.exists():
        import json as _json_tmp
        _form_cfg = _json_tmp.loads(_form_types_path.read_text(encoding="utf-8")).get(form_id, {})
        for _xv in _form_cfg.get("cross_validation", []):
            if _xv.get("type") == "cover_breakdown_vs_detail":
                _dist_group_field = _xv.get("detail_group_field")
                break

    items = phase2_result.get("items", [])

    # cover 페이지에서 issuer 추출
    issuer: dict = {}
    for page in phase2_result.get("pages", []):
        if page.get("role") == "cover" and page.get("issuer"):
            issuer = page["issuer"]
            break

    unique_retailers = list({i["customer"] for i in items if i.get("customer")})
    # 첫 등장 item_type 보존 (동일 OCR명 복수 타입 시 마지막값 overwrite 방지)
    product_type_map: dict[str, str] = {}
    for i in items:
        p = i.get("product")
        if p and p not in product_type_map:
            product_type_map[p] = i.get("item_type", "条件")
    unique_products = list(product_type_map.keys())

    # OCR 명칭 → 첫 등장 페이지 번호 (bbox 하이라이트용)
    customer_page: dict[str, int] = {}
    product_page: dict[str, int] = {}
    for item in items:
        sp = item.get("source_pages")
        pg = sp[0] if sp else item.get("page")
        if not pg:
            continue
        c = item.get("customer", "")
        p = item.get("product", "")
        if c and c not in customer_page:
            customer_page[c] = pg
        if p and p not in product_page:
            product_page[p] = pg

    # ── ① 소매처·제품 조회 준비 ───────────────────────────────────────────────
    cache_d = _load_dist_cache(mappings_dir / "ocr_dist.csv")

    fingerprint_fields = _parse_fingerprint_fields(form_md)
    issuer_fingerprint = _build_issuer_fingerprint(issuer, fingerprint_fields)

    # retail_user.csv — dist 1:1 자동확정 + 캐시 저장 시 이름 조회용
    retail_user_rows = _read_csv(mappings_dir / "retail_user.csv") if (mappings_dir / "retail_user.csv").exists() else []
    retailer_name_by_code: dict[str, str] = {r["소매처코드"]: r["소매처명"] for r in retail_user_rows}
    dist_name_by_code: dict[str, str] = {r["판매처코드"]: r["판매처명"] for r in retail_user_rows}

    confirmed_retailers: dict[str, dict] = {}
    for name in unique_retailers:
        result = await lookup_retailer(
            ocr_name=name,
            form_id=form_id,
            mappings_dir=mappings_dir,
            form_definitions_dir=settings.form_definitions_dir,
        )

        if result.basis in ("cache", "bracket_code"):
            retailer_code = result.retailer_code
            dist_key = (form_id, issuer_fingerprint, retailer_code)
            dist_code = cache_d.get(dist_key, "")
            confirmed_retailers[name] = {
                "retailer_code": retailer_code,
                "dist_code":     dist_code,
                "basis":         result.basis,
            }
            if result.basis == "bracket_code":
                await confirm_mapping(
                    mapping_type="retailer",
                    ocr_name=name,
                    confirmed_code=retailer_code,
                    context={"retailer_name": retailer_name_by_code.get(retailer_code, "")},
                    mappings_dir=mappings_dir,
                )
                if dist_code:
                    await confirm_mapping(
                        mapping_type="dist",
                        ocr_name=name,
                        confirmed_code=dist_code,
                        context={
                            "form_id": form_id,
                            "issuer_fingerprint": issuer_fingerprint,
                            "retailer_code": retailer_code,
                            "dist_name": dist_name_by_code.get(dist_code, ""),
                        },
                        mappings_dir=mappings_dir,
                    )

    # ── ① 제품 캐시 조회 (search_product: 캐시→유사도 후보) ─────────────────
    confirmed_products: dict[str, dict] = {}
    for name in unique_products:
        sp_result = await search_product(ocr_name=name, mappings_dir=mappings_dir)
        if sp_result.basis == "cache":
            confirmed_products[name] = {"code": sp_result.product_code, "basis": "cache"}

    miss_retailers = [n for n in unique_retailers if n not in confirmed_retailers]

    # ── pending은 아래 dist 루프에서도 사용하므로 여기서 초기화 ─────────────────
    pending: list[dict] = []

    # dist 캐시 미스 처리: Python이 1:1 케이스를 먼저 자동 확정
    # 1:N 케이스만 Claude에 넘기되, 후보 목록을 미리 추려서 전달
    cached_retailers_needing_dist: list[dict] = []

    for _name in unique_retailers:
        if _name not in confirmed_retailers or confirmed_retailers[_name].get("dist_code"):
            continue
        _rc = confirmed_retailers[_name]["retailer_code"]
        _candidates = [
            {"dist_code": r["판매처코드"], "dist_name": r["판매처명"]}
            for r in retail_user_rows
            if r.get("소매처코드") == _rc
        ]
        if len(_candidates) == 1:
            # 1:1 → Python 자동 확정 + 캐시 저장
            _dc = _candidates[0]["dist_code"]
            _dn = _candidates[0]["dist_name"]
            confirmed_retailers[_name]["dist_code"] = _dc
            await confirm_mapping(
                mapping_type="dist",
                ocr_name=_name,
                confirmed_code=_dc,
                context={
                    "form_id": form_id,
                    "issuer_fingerprint": issuer_fingerprint,
                    "retailer_code": _rc,
                    "dist_name": _dn,
                },
                mappings_dir=mappings_dir,
            )
        elif len(_candidates) > 1:
            # 1:N → Claude가 판단 (후보 목록 + 그룹 식별 필드 값 포함)
            # 그룹 식별 필드: form_types.json cross_validation의 detail_group_field에서 동적 조회
            # issuer보다 직접적인 근거 — form별 필드명이 다르므로 값만 수집
            _jisho_values: list[str] = []
            if _dist_group_field:
                _jisho_values = list({
                    item.get(_dist_group_field, "")
                    for item in items
                    if item.get("customer") == _name and item.get(_dist_group_field)
                })
            cached_retailers_needing_dist.append({
                "ocr_name":     _name,
                "retailer_code": _rc,
                "candidates":   _candidates,
                **({"jisho_values": _jisho_values} if _jisho_values else {}),
            })
        else:
            # 0건 → retail_user.csv에 해당 소매처코드 행 없음 (판매처코드 정의 미비)
            log.warning(
                "[%s] dist NOT_FOUND — 소매처 '%s' (코드 %s)에 대한 판매처 후보가 retail_user.csv에 없음",
                "phase3", _name, _rc,
            )
            pending.append({
                "mapping_type": "dist",
                "ocrName": _name,
                "candidates": [],
                "page_number": customer_page.get(_name),
            })
    miss_products = [
        {"product": n, "item_type": product_type_map.get(n, "条件")}
        for n in unique_products
        if n not in confirmed_products
    ]

    # ── ② Claude 병렬 호출 (소매처/판매처 + 제품을 독립 call로 분리) ───────────
    run_retailer = bool(miss_retailers or cached_retailers_needing_dist)
    run_product  = bool(miss_products)

    if run_retailer or run_product:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        retailer_coro = (
            asyncio.to_thread(
                _call_retailer_claude,
                client, form_md, issuer,
                miss_retailers, cached_retailers_needing_dist,
                mappings_dir,
            )
            if run_retailer else _noop_result()
        )
        product_coro = (
            asyncio.to_thread(
                _call_product_claude,
                client, form_md, miss_products, mappings_dir,
            )
            if run_product else _noop_result()
        )

        (r_result, r_in, r_out, r_cr, r_cc), (p_result, p_in, p_out, p_cr, p_cc) = \
            await asyncio.gather(retailer_coro, product_coro)

        await _accumulate_tokens(
            doc_id,
            r_in + p_in, r_out + p_out, r_cr + p_cr, r_cc + p_cc,
            run_id=run_id,
        )

        # retailers 처리
        for m in r_result.get("retailers", []):
            ocr = m.get("ocr_name", "")
            if not ocr:
                continue
            if m.get("confidence") == "high" and m.get("retailer_code"):
                retailer_code = m["retailer_code"]
                dist_code     = m.get("dist_code", "")
                confirmed_retailers[ocr] = {
                    "retailer_code": retailer_code,
                    "dist_code":     dist_code,
                    "basis":         m.get("basis", "claude"),
                }
                await confirm_mapping(
                    mapping_type="retailer",
                    ocr_name=ocr,
                    confirmed_code=retailer_code,
                    context={"retailer_name": retailer_name_by_code.get(retailer_code, "")},
                    mappings_dir=mappings_dir,
                )
                if dist_code:
                    await confirm_mapping(
                        mapping_type="dist",
                        ocr_name=ocr,
                        confirmed_code=dist_code,
                        context={
                            "form_id": form_id,
                            "issuer_fingerprint": issuer_fingerprint,
                            "retailer_code": retailer_code,
                            "dist_name": dist_name_by_code.get(dist_code, ""),
                        },
                        mappings_dir=mappings_dir,
                    )
            else:
                pending.append({
                    "mapping_type": "retailer",
                    "ocrName":      ocr,
                    "candidates":   m.get("candidates", []),
                    "page_number":  customer_page.get(ocr),
                })

        # dist_only 처리 (소매처는 캐시됐고 dist만 결정)
        for m in r_result.get("dist_only", []):
            ocr = m.get("ocr_name", "")
            if not ocr or ocr not in confirmed_retailers:
                continue
            if m.get("confidence") == "high" and m.get("dist_code"):
                dist_code     = m["dist_code"]
                retailer_code = m["retailer_code"]
                confirmed_retailers[ocr]["dist_code"] = dist_code
                await confirm_mapping(
                    mapping_type="dist",
                    ocr_name=ocr,
                    confirmed_code=dist_code,
                    context={
                        "form_id": form_id,
                        "issuer_fingerprint": issuer_fingerprint,
                        "retailer_code": retailer_code,
                        "dist_name": dist_name_by_code.get(dist_code, ""),
                    },
                    mappings_dir=mappings_dir,
                )
            else:
                pending.append({
                    "mapping_type": "dist",
                    "ocrName":      ocr,
                    "candidates":   m.get("candidates", []),
                    "page_number":  customer_page.get(ocr),
                })

        # products 처리
        for m in p_result.get("products", []):
            ocr = m.get("ocr_name", "")
            if not ocr:
                continue
            if m.get("confidence") == "high" and m.get("product_code"):
                confirmed_products[ocr] = {
                    "code":        m["product_code"],
                    "master_name": m.get("master_name", ""),
                    "basis":       m.get("basis", "claude"),
                }
                await confirm_mapping(
                    mapping_type="product",
                    ocr_name=ocr,
                    confirmed_code=m["product_code"],
                    context={"product_name": m.get("master_name", "")},
                    mappings_dir=mappings_dir,
                )
            else:
                pending.append({
                    "mapping_type": "product",
                    "ocrName":      ocr,
                    "candidates":   m.get("candidates", []),
                    "page_number":  product_page.get(ocr),
                })

    # ── ②-후처리: Claude 결과 반영 후에도 dist_code 없는 거래처 재조회 ──────────
    # miss_retailers (domae_retail_1 / Claude 경유 확정)는 위 pre-Claude 루프에서 제외되므로
    # Claude 호출 완료 후 confirmed_retailers 전체를 대상으로 retail_user.csv 재조회
    pending_ocr_names = {p["ocrName"] for p in pending if p.get("mapping_type") == "dist"}
    for _name, _info in list(confirmed_retailers.items()):
        if _info.get("dist_code") or _name in pending_ocr_names:
            continue
        _rc = _info.get("retailer_code", "")
        if not _rc:
            continue
        _candidates = [
            {"dist_code": r["판매처코드"], "dist_name": r["판매처명"]}
            for r in retail_user_rows
            if r.get("소매처코드") == _rc
        ]
        if len(_candidates) == 1:
            _dc = _candidates[0]["dist_code"]
            _dn = _candidates[0]["dist_name"]
            confirmed_retailers[_name]["dist_code"] = _dc
            await confirm_mapping(
                mapping_type="dist",
                ocr_name=_name,
                confirmed_code=_dc,
                context={
                    "form_id": form_id,
                    "issuer_fingerprint": issuer_fingerprint,
                    "retailer_code": _rc,
                    "dist_name": _dn,
                },
                mappings_dir=mappings_dir,
            )
        else:
            if not _candidates:
                log.warning(
                    "[%s] dist NOT_FOUND (Claude 후처리) — 소매처 '%s' (코드 %s)에 대한 판매처 후보 없음",
                    "phase3", _name, _rc,
                )
            pending.append({
                "mapping_type": "dist",
                "ocrName":      _name,
                "candidates":   _candidates,
                "page_number":  customer_page.get(_name),
            })

    # ── ③ 아이템에 코드 적용 ─────────────────────────────────────────────────
    items_out = _apply_mappings(items, confirmed_retailers, confirmed_products)

    result = {
        "doc_id":              doc_id,
        "form_id":             form_id,
        "hatsu_month":         hatsu_month,
        "issuer":              issuer,
        "confirmed_retailers": confirmed_retailers,
        "confirmed_products":  confirmed_products,
        "items":               items_out,
        "cover_totals":        _extract_cover_totals(phase2_result),
    }
    out_path = output_dir / "phase3_output.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    return result, pending


async def _accumulate_tokens(
    doc_id: str, in_tok: int, out_tok: int, cr: int, cc: int, run_id: str = ""
) -> None:
    if in_tok or out_tok:
        from ..db.queries import accumulate_token_usage
        await accumulate_token_usage(
            doc_id, "phase3", in_tok, out_tok, "claude-haiku-4-5-20251001",
            cache_read_tokens=cr, cache_creation_tokens=cc,
            run_id=run_id,
        )
