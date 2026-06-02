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
import csv
import json
import logging
import re
import unicodedata
from pathlib import Path

import anthropic

from ..core.config import get_settings

log = logging.getLogger(__name__)

_SYSTEM_PROMPT_CACHE: str | None = None

# NFKC 정규화 후의 법인격 표기 목록
# （株）→(株)、㈱→(株) 등은 NFKC로 먼저 변환되므로 반각 형태만 열거
_LEGAL_MARKERS = [
    "株式会社", "有限会社", "合同会社",
    "(株)", "(有)", "(合)",
]


def normalize_ocr_name(name: str) -> str:
    """OCR 명칭 정규화: 전각→반각(NFKC) + 법인격 제거 + 공백 압축.

    캐시 조회 키로만 사용. 원본 OCR명은 캐시 파일에 그대로 저장한다.
    같은 거래처를 다르게 표기한 OCR 결과가 동일 키로 매핑되도록 한다.
    例)
      "（株）ファミリーマート"  → "ファミリーマート"
      "ファミリーマート（株）"  → "ファミリーマート"
      "(株)ファミリーマート"    → "ファミリーマート"  (전각·반각 모두 처리)
    """
    # 전각→반각, 합자 정규화 (㈱→(株) 등)
    name = unicodedata.normalize("NFKC", name)
    # 법인격 제거
    for marker in _LEGAL_MARKERS:
        name = name.replace(marker, "")
    # 연속 공백 → 단일 공백, 앞뒤 제거
    return " ".join(name.split())


# ── CSV 로더 ─────────────────────────────────────────────────────────────────

def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ── 캐시 로더 ─────────────────────────────────────────────────────────────────

def _load_retailer_cache(path: Path) -> dict[str, str]:
    """normalize(ocr_name) → retailer_code.

    키를 정규화해서 인덱싱하므로 전각·반각 차이나 법인격 표기 변동에
    무관하게 캐시가 히트된다. 원본 OCR명은 CSV에 보존된다.
    """
    if not path.exists():
        return {}
    return {normalize_ocr_name(r["ocr_name"]): r["retailer_code"] for r in _read_csv(path)}


def _load_product_cache(path: Path) -> dict[str, str]:
    """normalize(ocr_name) → product_code."""
    if not path.exists():
        return {}
    return {normalize_ocr_name(r["ocr_name"]): r["product_code"] for r in _read_csv(path)}


def _load_dist_cache(path: Path) -> dict[tuple, str]:
    """(form_id, issuer_fingerprint, retailer_code) → dist_code"""
    if not path.exists():
        return {}
    result = {}
    for r in _read_csv(path):
        key = (r.get("form_id", ""), r.get("issuer_fingerprint", ""), r.get("retailer_code", ""))
        result[key] = r["dist_code"]
    return result


# ── 캐시 저장 ─────────────────────────────────────────────────────────────────


def _upsert_cache_row(
    path: Path, key_col: str, headers: list[str], key: str, new_row: list[str],
) -> None:
    """캐시 파일에 key 기준 upsert. 헤더 컬럼 확장 시 기존 행에 빈 값을 채운다."""
    rows: list[list[str]] = []
    if path.exists() and path.stat().st_size > 0:
        try:
            with path.open(encoding="utf-8-sig") as f:
                rows = [[r.get(h, "") for h in headers] for r in csv.DictReader(f)]
        except Exception:
            pass
    key_idx = headers.index(key_col)
    updated = False
    for i, row in enumerate(rows):
        if len(row) > key_idx and row[key_idx] == key:
            rows[i] = new_row
            updated = True
            break
    if not updated:
        rows.append(new_row)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


def _upsert_dist_cache_row(
    path: Path, form_id: str, issuer_fingerprint: str,
    retailer_code: str, dist_code: str, dist_name: str = "",
) -> None:
    """ocr_dist.csv 복합키 upsert."""
    headers = ["form_id", "issuer_fingerprint", "retailer_code", "dist_code", "dist_name"]
    new_row = [form_id, issuer_fingerprint, retailer_code, dist_code, dist_name]
    rows: list[list[str]] = []
    if path.exists() and path.stat().st_size > 0:
        try:
            with path.open(encoding="utf-8-sig") as f:
                rows = [[r.get(h, "") for h in headers] for r in csv.DictReader(f)]
        except Exception:
            pass
    updated = False
    for i, row in enumerate(rows):
        if len(row) >= 3 and row[0] == form_id and row[1] == issuer_fingerprint and row[2] == retailer_code:
            rows[i] = new_row
            updated = True
            break
    if not updated:
        rows.append(new_row)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


def _append_retailer_cache(path: Path, ocr_name: str, retailer_code: str, retailer_name: str = "") -> None:
    _upsert_cache_row(path, "ocr_name", ["ocr_name", "retailer_code", "retailer_name"], ocr_name, [ocr_name, retailer_code, retailer_name])


def _append_product_cache(path: Path, ocr_name: str, product_code: str, product_name: str = "") -> None:
    _upsert_cache_row(path, "ocr_name", ["ocr_name", "product_code", "product_name"], ocr_name, [ocr_name, product_code, product_name])


def _append_dist_cache(
    path: Path, form_id: str, issuer_fingerprint: str,
    retailer_code: str, dist_code: str, dist_name: str = "",
) -> None:
    _upsert_dist_cache_row(path, form_id, issuer_fingerprint, retailer_code, dist_code, dist_name)


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

def _parse_bracket_code_csv(form_md: str) -> str:
    """form_XX.md의 データソース 섹션에서 bracket_code_csv 지시어 추출.
    없으면 빈 문자열 반환.
    """
    for line in form_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("bracket_code_csv:"):
            return stripped[len("bracket_code_csv:"):].strip()
    return ""


def _parse_retailer_csvs(form_md: str) -> list[str]:
    """form_XX.md의 ## データソース 섹션에서 소매처 매핑용 CSV 목록 추출.
    섹션이 없으면 name 기반 검색 기본값 반환."""
    lines = form_md.splitlines()
    in_section = False
    csvs: list[str] = []
    for line in lines:
        if line.strip().startswith("## データソース"):
            in_section = True
            continue
        if in_section:
            if line.startswith("##"):
                break
            stripped = line.strip()
            if stripped.startswith("- ") and stripped.endswith(".csv"):
                csvs.append(stripped[2:].strip())
    return csvs or ["retail_user.csv"]


def _build_retailer_csv_context(form_md: str, mappings_dir: Path) -> str:
    """form_XX.md의 データソース 섹션에 명시된 CSV만 로드 (소매처·판매처 매핑용)."""
    parts = []
    for fname in _parse_retailer_csvs(form_md):
        p = mappings_dir / fname
        if p.exists():
            parts.append(f"### {fname}\n{p.read_text(encoding='utf-8-sig')}")
    return "\n\n".join(parts)


def _build_product_csv_context(mappings_dir: Path) -> str:
    """제품 매핑용 CSV (unit_price.csv만)."""
    p = mappings_dir / "unit_price.csv"
    return f"### unit_price.csv\n{p.read_text(encoding='utf-8-sig')}" if p.exists() else ""


# ── Claude 공통 호출 ──────────────────────────────────────────────────────────

def _call_claude(
    client: anthropic.Anthropic,
    system: list[dict],
    user_payload: dict,
) -> tuple[dict, int, int, int, int]:
    """Claude API 호출 및 JSON 파싱 공통 로직."""
    message = client.messages.create(
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

    # ── ① 캐시 조회 ───────────────────────────────────────────────────────────
    cache_r = _load_retailer_cache(mappings_dir / "ocr_retailer.csv")
    cache_p = _load_product_cache(mappings_dir / "ocr_product.csv")
    cache_d = _load_dist_cache(mappings_dir / "ocr_dist.csv")

    fingerprint_fields = _parse_fingerprint_fields(form_md)
    issuer_fingerprint = _build_issuer_fingerprint(issuer, fingerprint_fields)

    # 소매처 캐시 히트 (정규화 키로 조회)
    confirmed_retailers: dict[str, dict] = {}
    for name in unique_retailers:
        norm = normalize_ocr_name(name)
        if norm in cache_r:
            retailer_code = cache_r[norm]
            dist_key = (form_id, issuer_fingerprint, retailer_code)
            dist_code = cache_d.get(dist_key, "")
            confirmed_retailers[name] = {
                "retailer_code": retailer_code,
                "dist_code": dist_code,
                "basis": "cache",
            }

    # 제품 캐시 히트 (정규화 키로 조회)
    confirmed_products: dict[str, dict] = {}
    for name in unique_products:
        norm = normalize_ocr_name(name)
        if norm in cache_p:
            confirmed_products[name] = {"code": cache_p[norm], "basis": "cache"}

    # retail_user.csv 행 목록 — bracket 코드 처리 + dist 1:1 자동확정에서 공통 사용
    retail_user_rows = _read_csv(mappings_dir / "retail_user.csv") if (mappings_dir / "retail_user.csv").exists() else []
    retailer_name_by_code: dict[str, str] = {r["소매처코드"]: r["소매처명"] for r in retail_user_rows}
    dist_name_by_code: dict[str, str] = {r["판매처코드"]: r["판매처명"] for r in retail_user_rows}

    # ── ①-2 괄호 코드 직접 조회 (form_01 등 bracket_code_csv 지정 양식) ─────────
    # 결정적 처리이므로 Claude에 위임하지 않고 Python이 직접 수행
    bracket_csv_name = _parse_bracket_code_csv(form_md)
    if bracket_csv_name:
        bracket_path = mappings_dir / bracket_csv_name
        if bracket_path.exists():
            domae_map: dict[str, str] = {}
            for r in _read_csv(bracket_path):
                keys = list(r.keys())
                if len(keys) >= 2:
                    domae_map[r[keys[0]]] = r[keys[1]]
            for name in unique_retailers:
                if name in confirmed_retailers:
                    continue
                m = re.search(r'\((\d+)\)', name)
                if not m:
                    continue
                bracket_code = m.group(1)
                retailer_code = domae_map.get(bracket_code, "")
                if retailer_code:
                    dist_key = (form_id, issuer_fingerprint, retailer_code)
                    dist_code = cache_d.get(dist_key, "")
                    confirmed_retailers[name] = {
                        "retailer_code": retailer_code,
                        "dist_code":     dist_code,
                        "basis":         f"括弧コード {bracket_code} → {bracket_csv_name} 自動確定",
                    }
                    _append_retailer_cache(mappings_dir / "ocr_retailer.csv", name, retailer_code, retailer_name_by_code.get(retailer_code, ""))
                    if dist_code:
                        _append_dist_cache(
                            mappings_dir / "ocr_dist.csv",
                            form_id, issuer_fingerprint, retailer_code, dist_code, dist_name_by_code.get(dist_code, ""),
                        )

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
            _append_dist_cache(
                mappings_dir / "ocr_dist.csv",
                form_id, issuer_fingerprint, _rc, _dc, _dn,
            )
        elif len(_candidates) > 1:
            # 1:N → Claude가 issuer 정보로 추론 (후보 목록 포함)
            cached_retailers_needing_dist.append({
                "ocr_name": _name,
                "retailer_code": _rc,
                "candidates": _candidates,
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
        if n not in confirmed_products  # cache_p 직접 참조 금지 — 정규화 히트는 confirmed_products에 반영됨
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
                _append_retailer_cache(mappings_dir / "ocr_retailer.csv", ocr, retailer_code, retailer_name_by_code.get(retailer_code, ""))
                if dist_code:
                    _append_dist_cache(
                        mappings_dir / "ocr_dist.csv",
                        form_id, issuer_fingerprint, retailer_code, dist_code, dist_name_by_code.get(dist_code, ""),
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
                _append_dist_cache(
                    mappings_dir / "ocr_dist.csv",
                    form_id, issuer_fingerprint, retailer_code, dist_code, dist_name_by_code.get(dist_code, ""),
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
                _append_product_cache(mappings_dir / "ocr_product.csv", ocr, m["product_code"], m.get("master_name", ""))
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
            _append_dist_cache(mappings_dir / "ocr_dist.csv", form_id, issuer_fingerprint, _rc, _dc, _dn)
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
