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

_SYSTEM_PROMPT_CACHE: tuple[float, str] | None = None  # (mtime, prompt)


# ── 캐시 로더 ─────────────────────────────────────────────────────────────────

def _load_dist_cache(path: Path) -> dict[tuple, str]:
    """(form_id, issuer_fingerprint, retailer_code, jisho) → dist_code

    같은 소매처라도 jisho(入出荷支店 등 그룹 식별 필드)가 다르면 판매처가 갈리므로
    캐시 키에 jisho를 포함한다. jisho 컬럼이 없는 구(舊) 캐시 행은 jisho=""로 로드된다.

    path.exists() 선차단 금지 — _read_csv가 Sheets 우선 조회하므로
    로컬 파일이 없어도(Sheets 운영 모드) 캐시를 읽어야 한다."""
    result = {}
    for r in _read_csv(path):
        key = (
            r.get("form_id", ""), r.get("issuer_fingerprint", ""),
            r.get("retailer_code", ""), r.get("jisho", ""),
        )
        result[key] = r.get("dist_code", "")
    return result


def _code_in_master(code: str, master_codes: set[str], *, kind: str, ocr_name: str) -> bool:
    """Claude가 확정 제안한 코드가 마스터에 실재하는지 검증.

    마스터가 비어 있으면(Sheets·CSV 로드 실패) 검증을 강제하지 않는다 —
    이 경우 전건 pending화가 더 위험하므로 경고만 남기고 통과시킨다."""
    if not master_codes:
        log.warning("[phase3] %s 마스터가 비어 있어 '%s' 코드 검증 생략", kind, ocr_name)
        return True
    if code in master_codes:
        return True
    log.warning(
        "[phase3] %s '%s': Claude 제안 코드 '%s'가 마스터에 없음 → pending",
        kind, ocr_name, code,
    )
    return False


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


def get_dist_group_field(form_id: str) -> str | None:
    """판매처(dist) 확정 단위가 되는 그룹 식별 필드명을 form_types.json에서 조회한다.

    config/form_types.json의 cross_validation 중 type=="cover_breakdown_vs_detail"의
    detail_group_field를 반환한다. 없으면 None (jisho 미사용 양식 → 소매처당 판매처 1개).

    legacy run_phase3 / Tool Use 경로 / orchestrator가 동일 기준을 쓰도록 공유한다."""
    settings = get_settings()
    path = settings.workspace_root / "config" / "form_types.json"
    if not path.exists():
        return None
    try:
        cfg = json.loads(path.read_text(encoding="utf-8")).get(form_id, {})
    except (json.JSONDecodeError, OSError):
        return None
    for xv in cfg.get("cross_validation", []):
        if xv.get("type") == "cover_breakdown_vs_detail":
            return xv.get("detail_group_field")
    return None


def get_dist_overrides(form_id: str) -> list[dict]:
    """판매처(dist) 조건부 override 규칙을 form_types.json에서 조회한다.

    config/form_types.json[form_id].dist_overrides 배열을 반환한다(없으면 []).
    1:N 모호 케이스에서 LLM 대신 결정적으로 후보를 고르는 규칙 — dist_overrides.py 참조.
    미선언 양식은 [] → override 미적용(기존 동작)."""
    settings = get_settings()
    path = settings.workspace_root / "config" / "form_types.json"
    if not path.exists():
        return []
    try:
        cfg = json.loads(path.read_text(encoding="utf-8")).get(form_id, {})
    except (json.JSONDecodeError, OSError):
        return []
    rules = cfg.get("dist_overrides")
    return rules if isinstance(rules, list) else []


def build_jisho_by_customer(
    items: list[dict], unique_customers: list[str], dist_group_field: str | None,
) -> dict[str, list[str]]:
    """소매처별 jisho 값 목록을 만든다 (판매처 확정 단위).

    dist_group_field가 없으면 모든 소매처가 [""] → 소매처당 판매처 1개(기존 동작)."""
    out: dict[str, list[str]] = {}
    for i in items:
        c = i.get("customer", "")
        if not c:
            continue
        jv = i.get(dist_group_field, "") if dist_group_field else ""
        out.setdefault(c, [])
        if jv not in out[c]:
            out[c].append(jv)
    for c in unique_customers:
        out.setdefault(c, [""])
    return out


# ── 시스템 프롬프트 ────────────────────────────────────────────────────────────

def _get_system_prompt() -> str:
    """docs/phase3-prompt.md 전체를 시스템 프롬프트로 사용 (파일 헤더에 명시된 계약).

    mtime 기반 캐시 — md 수정 시 백엔드 재시작 없이 다음 호출부터 반영된다.
    """
    global _SYSTEM_PROMPT_CACHE
    path = get_settings().workspace_root / "docs" / "phase3-prompt.md"
    mtime = path.stat().st_mtime
    if _SYSTEM_PROMPT_CACHE is not None and _SYSTEM_PROMPT_CACHE[0] == mtime:
        return _SYSTEM_PROMPT_CACHE[1]
    prompt = path.read_text(encoding="utf-8")
    log.info(
        "phase3 시스템 프롬프트 로드 — 파일 전체 %d자 사용 (phase3-prompt.md mtime=%.0f)",
        len(prompt), mtime,
    )
    _SYSTEM_PROMPT_CACHE = (mtime, prompt)
    return prompt


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
        temperature=0,
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
    confirmed_dist: dict[tuple[str, str], dict] | None = None,
    dist_group_field: str | None = None,
) -> list[dict]:
    """phase2 items에 retailer_code / dist_code / product_code 쓰기.
    item_type은 Phase 2가 확정해서 넘겨주므로 읽기만 한다.

    판매처(dist) 결정 방식:
      - confirmed_dist 제공 시: (소매처, jisho) 단위로 각 item의 jisho 값으로 조회.
        jisho 미사용 양식은 dist_group_field=None → 키가 (소매처, "")로 통일.
      - confirmed_dist 미제공 시(레거시 호출): confirmed_retailers의 dist_code 사용
        (소매처 단위 — Tool Use adapter 등 아직 jisho 비대응 경로 호환)."""
    out = []
    for item in items:
        i = dict(item)
        ocr_customer = i.get("customer", "")
        ocr_product  = i.get("product", "")

        i.setdefault("customer_ocr", ocr_customer)
        i.setdefault("product_ocr",  ocr_product)

        rc = confirmed_retailers.get(ocr_customer, {})
        i["retailer_code"] = rc.get("retailer_code", "")
        if confirmed_dist is not None:
            _jisho = i.get(dist_group_field, "") if dist_group_field else ""
            i["dist_code"] = confirmed_dist.get((ocr_customer, _jisho), {}).get("dist_code", "")
        else:
            i["dist_code"] = rc.get("dist_code", "")
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
    cache_only: bool = False,
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
    _dist_group_field: str | None = get_dist_group_field(form_id)

    items = phase2_result.get("items", [])

    # cover 페이지에서 issuer 추출
    issuer: dict = {}
    for page in phase2_result.get("pages", []):
        if page.get("role") == "cover" and page.get("issuer"):
            issuer = page["issuer"]
            break

    unique_retailers = list({i["customer"] for i in items if i.get("customer")})

    # 소매처별 jisho(그룹 식별 필드) 값 목록 — 판매처(dist) 확정 단위.
    # 같은 소매처라도 jisho가 다르면 판매처가 갈리므로 (소매처 × jisho)로 매핑한다.
    # _dist_group_field가 없는(=jisho 미사용) 양식은 모든 소매처가 [""] → 기존과 동일하게
    # 소매처당 판매처 1개로 동작한다.
    jisho_by_customer = build_jisho_by_customer(items, unique_retailers, _dist_group_field)
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
    # _read_csv: Sheets 설정 시 Sheets 우선, 없으면 로컬 파일 (EC2에선 파일이 없으므로 .exists() 조건 제거)
    try:
        retail_user_rows = _read_csv(mappings_dir / "retail_user.csv")
    except FileNotFoundError:
        retail_user_rows = []
    retailer_name_by_code: dict[str, str] = {r["소매처코드"]: r["소매처명"] for r in retail_user_rows}
    dist_name_by_code: dict[str, str] = {r["판매처코드"]: r["판매처명"] for r in retail_user_rows}
    # 소매처코드 → 판매처 후보 목록 인덱스 (dist 1:1/1:N 판정용 — 거래처마다 전체 행 순회 방지)
    dist_candidates_by_retailer: dict[str, list[dict]] = {}
    for r in retail_user_rows:
        dist_candidates_by_retailer.setdefault(r.get("소매처코드", ""), []).append(
            {"dist_code": r["판매처코드"], "dist_name": r["판매처명"]}
        )

    # Claude 확정 답변 검증용 마스터 코드 집합 (마스터 밖 코드는 자동확정·캐시기록 금지)
    valid_retailer_codes = set(retailer_name_by_code)
    valid_dist_codes = set(dist_name_by_code)
    valid_product_codes = {
        r.get("제품코드", "") for r in _read_csv(mappings_dir / "unit_price.csv") if r.get("제품코드")
    }

    confirmed_retailers: dict[str, dict] = {}
    for name in unique_retailers:
        result = await lookup_retailer(
            ocr_name=name,
            form_id=form_id,
            mappings_dir=mappings_dir,
            form_definitions_dir=settings.form_definitions_dir,
        )

        if result.basis in ("cache", "bracket_code", "exact_match"):
            retailer_code = result.retailer_code
            # 판매처(dist)는 (소매처 × jisho) 단위라 여기서 확정하지 않고,
            # 아래 _resolve_dist_for() 패스에서 jisho별로 일괄 처리한다.
            confirmed_retailers[name] = {
                "retailer_code": retailer_code,
                "basis":         result.basis,
            }
            if result.basis in ("bracket_code", "exact_match"):
                await confirm_mapping(
                    mapping_type="retailer",
                    ocr_name=name,
                    confirmed_code=retailer_code,
                    context={"retailer_name": retailer_name_by_code.get(retailer_code, "")},
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

    # 판매처(dist) 확정 상태: (소매처 OCR명, jisho) → {dist_code, dist_name}
    confirmed_dist: dict[tuple[str, str], dict] = {}
    # 1:N 모호 케이스 중 Claude에 넘길 (소매처 × jisho) 항목
    cached_retailers_needing_dist: list[dict] = []
    # dist pending은 UI에서 (mapping_type, ocr_name) 단위로 표시되므로 소매처당 1회만 등록
    dist_pending_emitted: set[str] = set()

    async def _resolve_dist_for(_name: str, *, allow_claude: bool) -> None:
        """소매처 _name의 jisho별 판매처를 확정한다.

        우선순위: ① 캐시 히트 → ② 후보 1건 자동확정 → ③ 후보 복수(1:N):
        allow_claude면 Claude 위임 목록에 적재, 아니면 pending → ④ 후보 0건 pending.
        같은 소매처라도 jisho가 다르면 각각 독립적으로 판매처를 정한다."""
        info = confirmed_retailers.get(_name)
        if not info:
            return
        _rc = info.get("retailer_code", "")
        if not _rc:
            return
        _candidates = dist_candidates_by_retailer.get(_rc, [])
        for _jisho in jisho_by_customer.get(_name, [""]):
            if (_name, _jisho) in confirmed_dist:
                continue
            # ① 캐시 히트 (이미 확정된 값이므로 재기록 불필요)
            _cached = cache_d.get((form_id, issuer_fingerprint, _rc, _jisho), "")
            if _cached:
                confirmed_dist[(_name, _jisho)] = {
                    "dist_code": _cached,
                    "dist_name": dist_name_by_code.get(_cached, ""),
                }
                continue
            # ② 후보 1건 → Python 자동 확정 + 캐시 저장
            if len(_candidates) == 1:
                _dc = _candidates[0]["dist_code"]
                _dn = _candidates[0]["dist_name"]
                confirmed_dist[(_name, _jisho)] = {"dist_code": _dc, "dist_name": _dn}
                await confirm_mapping(
                    mapping_type="dist",
                    ocr_name=_name,
                    confirmed_code=_dc,
                    context={
                        "form_id": form_id,
                        "issuer_fingerprint": issuer_fingerprint,
                        "retailer_code": _rc,
                        "jisho": _jisho,
                        "dist_name": _dn,
                    },
                    mappings_dir=mappings_dir,
                )
            # ③ 후보 복수(1:N)
            elif len(_candidates) > 1:
                if allow_claude:
                    cached_retailers_needing_dist.append({
                        "ocr_name":     _name,
                        "retailer_code": _rc,
                        "candidates":   _candidates,
                        **({"jisho": _jisho} if _jisho else {}),
                    })
                elif _name not in dist_pending_emitted:
                    dist_pending_emitted.add(_name)
                    pending.append({
                        "mapping_type": "dist",
                        "ocrName": _name,
                        "candidates": _candidates,
                        "page_number": customer_page.get(_name),
                    })
            # ④ 후보 0건 → retail_user.csv에 해당 소매처코드 행 없음
            else:
                log.warning(
                    "[%s] dist NOT_FOUND — 소매처 '%s' (코드 %s)에 대한 판매처 후보가 retail_user.csv에 없음",
                    "phase3", _name, _rc,
                )
                if _name not in dist_pending_emitted:
                    dist_pending_emitted.add(_name)
                    pending.append({
                        "mapping_type": "dist",
                        "ocrName": _name,
                        "candidates": [],
                        "page_number": customer_page.get(_name),
                    })

    # 캐시 히트 소매처의 판매처를 jisho별로 선처리 (1:N은 Claude 위임 목록에 적재)
    for _name in unique_retailers:
        if _name in confirmed_retailers:
            await _resolve_dist_for(_name, allow_claude=True)

    miss_products = [
        {"product": n, "item_type": product_type_map.get(n, "条件")}
        for n in unique_products
        if n not in confirmed_products
    ]

    # ── ② Claude 병렬 호출 (소매처/판매처 + 제품을 독립 call로 분리) ───────────
    run_retailer = bool(miss_retailers or cached_retailers_needing_dist)
    run_product  = bool(miss_products)

    if (run_retailer or run_product) and not cache_only:
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
        responded_retailers: set[str] = set()
        for m in r_result.get("retailers", []):
            ocr = m.get("ocr_name", "")
            if not ocr:
                continue
            responded_retailers.add(ocr)
            if (m.get("confidence") == "high" and m.get("retailer_code")
                    and _code_in_master(m["retailer_code"], valid_retailer_codes,
                                        kind="retailer", ocr_name=ocr)):
                retailer_code = m["retailer_code"]
                # 판매처(dist)는 아래 post-Claude _resolve_dist_for() 패스에서 jisho별로
                # 확정한다. retailers[] 응답의 단일 dist_code는 다(多)-jisho를 표현할 수
                # 없으므로 여기서 쓰지 않는다.
                confirmed_retailers[ocr] = {
                    "retailer_code": retailer_code,
                    "basis":         m.get("basis", "claude"),
                }
                await confirm_mapping(
                    mapping_type="retailer",
                    ocr_name=ocr,
                    confirmed_code=retailer_code,
                    context={"retailer_name": retailer_name_by_code.get(retailer_code, "")},
                    mappings_dir=mappings_dir,
                )
            else:
                pending.append({
                    "mapping_type": "retailer",
                    "ocrName":      ocr,
                    "candidates":   m.get("candidates", []),
                    "page_number":  customer_page.get(ocr),
                })
        # Claude 출력에 누락된 miss_retailers → pending으로 안전하게 처리
        for n in miss_retailers:
            if n not in responded_retailers and n not in confirmed_retailers:
                pending.append({
                    "mapping_type": "retailer",
                    "ocrName":      n,
                    "candidates":   [],
                    "page_number":  customer_page.get(n),
                })

        # dist_only 처리 (소매처는 캐시됐고 dist만 결정) — (소매처 × jisho) 단위
        for m in r_result.get("dist_only", []):
            ocr = m.get("ocr_name", "")
            if not ocr or ocr not in confirmed_retailers:
                continue
            _jisho = m.get("jisho", "")
            if (m.get("confidence") == "high" and m.get("dist_code")
                    and _code_in_master(m["dist_code"], valid_dist_codes,
                                        kind="dist", ocr_name=ocr)):
                dist_code     = m["dist_code"]
                retailer_code = m.get("retailer_code") or confirmed_retailers[ocr].get("retailer_code", "")
                confirmed_dist[(ocr, _jisho)] = {
                    "dist_code": dist_code,
                    "dist_name": dist_name_by_code.get(dist_code, ""),
                }
                await confirm_mapping(
                    mapping_type="dist",
                    ocr_name=ocr,
                    confirmed_code=dist_code,
                    context={
                        "form_id": form_id,
                        "issuer_fingerprint": issuer_fingerprint,
                        "retailer_code": retailer_code,
                        "jisho": _jisho,
                        "dist_name": dist_name_by_code.get(dist_code, ""),
                    },
                    mappings_dir=mappings_dir,
                )
            elif ocr not in dist_pending_emitted:
                dist_pending_emitted.add(ocr)
                pending.append({
                    "mapping_type": "dist",
                    "ocrName":      ocr,
                    "candidates":   m.get("candidates", []),
                    "page_number":  customer_page.get(ocr),
                })

        # products 처리
        responded_products: set[str] = set()
        for m in p_result.get("products", []):
            ocr = m.get("ocr_name", "")
            if not ocr:
                continue
            responded_products.add(ocr)
            if (m.get("confidence") == "high" and m.get("product_code")
                    and _code_in_master(m["product_code"], valid_product_codes,
                                        kind="product", ocr_name=ocr)):
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
        # Claude 출력에 누락된 miss_products → pending으로 안전하게 처리
        for mp in miss_products:
            n = mp["product"]
            if n not in responded_products and n not in confirmed_products:
                pending.append({
                    "mapping_type": "product",
                    "ocrName":      n,
                    "candidates":   [],
                    "page_number":  product_page.get(n),
                })

    elif cache_only:
        # Claude 호출 없이 캐시 미스 항목을 바로 pending으로 등록
        for n in miss_retailers:
            pending.append({
                "mapping_type": "retailer",
                "ocrName":      n,
                "candidates":   [],
                "page_number":  customer_page.get(n),
            })
        for mp in miss_products:
            pending.append({
                "mapping_type": "product",
                "ocrName":      mp["product"],
                "candidates":   [],
                "page_number":  product_page.get(mp["product"]),
            })

    # ── ②-후처리: Claude로 확정된 소매처 포함, dist 미결 (소매처 × jisho) 일괄 처리 ──
    # Claude를 다시 부를 수 없으므로 1:1 자동확정 / 캐시 / pending만 수행한다.
    # _resolve_dist_for는 이미 confirmed_dist에 있는 (소매처, jisho)는 건너뛴다.
    for _name in list(confirmed_retailers.keys()):
        await _resolve_dist_for(_name, allow_claude=False)

    # ── ③ 아이템에 코드 적용 ─────────────────────────────────────────────────
    items_out = _apply_mappings(
        items, confirmed_retailers, confirmed_products,
        confirmed_dist, _dist_group_field,
    )

    result = {
        "doc_id":              doc_id,
        "form_id":             form_id,
        "hatsu_month":         hatsu_month,
        "issuer":              issuer,
        "confirmed_retailers": confirmed_retailers,
        # (소매처, jisho) → 판매처. JSON 직렬화를 위해 list로 펼침 (디버깅·추적용)
        "confirmed_dist": [
            {"customer": _c, "jisho": _j, **_v}
            for (_c, _j), _v in confirmed_dist.items()
        ],
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
