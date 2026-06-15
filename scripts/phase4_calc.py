"""
Phase 4 — NET 계산·출력
LLM 없음. 완전 결정적. 재현성이 생명.

교차검증은 backend/pipeline/phase4.py의 Claude 호출이 담당한다.
이 파일은 NET 계산과 출력만 수행한다.

입력: extracted/{doc_id}/phase3_output.json
      extracted/{doc_id}/phase2_output.json  (cover/summary totals용)
출력: stdout 테이블 + (--save 옵션 시) phase4_output.json

모든 양식별 분기는 config/form_types.json에서 읽는다.
이 파일에 form_id를 직접 비교하는 분기(if form_id == "form_XX")를 추가하지 말 것.
"""
import ast
import argparse, csv, json, math, operator, os, re, sys, time
from collections import defaultdict
from pathlib import Path
from typing import Optional

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))


def _get_sheets_store():
    """GOOGLE_SHEETS_MAPPINGS_ID 환경변수가 설정된 경우 SheetsStore 반환."""
    sid = os.environ.get("GOOGLE_SHEETS_MAPPINGS_ID", "")
    if not sid:
        # backend/.env에서 직접 읽기 (subprocess 환경에서 env가 전달 안 될 때 대비)
        env_path = BASE / "backend" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("GOOGLE_SHEETS_MAPPINGS_ID="):
                    sid = line.split("=", 1)[1].strip()
                    break
    if not sid:
        return None
    try:
        from backend.core.sheets_store import SheetsStore
        return SheetsStore(sid)
    except Exception:
        return None

with open(BASE / "config" / "form_types.json", encoding="utf-8") as _f:
    FORM_TYPES: dict = json.load(_f)

# 消費税率 — config/tax_rules.json이 단일 출처 (코드에 세율 하드코딩 금지)
# 파일 없으면 즉시 FileNotFoundError로 중단 (숨은 기본값 없음)
with open(BASE / "config" / "tax_rules.json", encoding="utf-8") as _f:
    TAX_RULES: dict = json.load(_f)
_RATE_8:  float = TAX_RULES["bracket_rates"]["8"]
_RATE_10: float = TAX_RULES["bracket_rates"]["10"]

# ── 헬퍼 ─────────────────────────────────────────────────────────────────────
def to_f(v, default=None):
    try:
        return float(v) if v not in (None, "", "—") else default
    except (ValueError, TypeError):
        return default

def extract_n_token(conds):
    for c in (conds or []):
        m = re.match(r"(N\d)", str(c))
        if m:
            return m.group(1)
    return ""

def _clean_cell(val) -> str:
    """MD 테이블 파이프 잔여물 제거. 'R営業中四国 | | |' → 'R営業中四国'"""
    if not val:
        return ""
    return re.sub(r'\s*(?:\|\s*)+$', '', str(val)).strip()

# ── DSL 수식 평가기 ───────────────────────────────────────────────────────────
_SAFE_OPS = {
    ast.Add:  operator.add,
    ast.Sub:  operator.sub,
    ast.Mult: operator.mul,
    ast.Div:  operator.truediv,
    ast.USub: operator.neg,
}

def _safe_eval(expr: str, ctx: dict, *, _form_id: str = "", _label: str = "") -> float:
    """산술 표현식만 허용하는 안전한 평가기. eval() 미사용.
    허용: 숫자 리터럴, ctx 변수명, +  -  *  /  ()
    금지: 함수 호출, 속성 접근, 비교 연산, 그 외 모든 것.

    _form_id, _label: 오류 메시지에 포함할 컨텍스트 정보.
    """
    _ctx_str = f"form={_form_id!r} label={_label!r}" if _form_id or _label else ""

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(
            f"DSL 수식 구문 오류 [{_ctx_str}]: expr={expr!r} → {e}"
        ) from e

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)):
                raise ValueError(
                    f"비수치 상수 [{_ctx_str}]: expr={expr!r}, value={node.value!r}"
                )
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in ctx:
                available = sorted(ctx.keys())
                raise ValueError(
                    f"알 수 없는 변수 [{_ctx_str}]: "
                    f"expr={expr!r}, 변수={node.id!r}, "
                    f"사용 가능한 변수={available}"
                )
            v = ctx[node.id]
            return float(v) if v is not None else 0.0
        if isinstance(node, ast.BinOp):
            op = _SAFE_OPS.get(type(node.op))
            if op is None:
                raise ValueError(
                    f"허용되지 않은 연산자 [{_ctx_str}]: "
                    f"expr={expr!r}, 연산자={type(node.op).__name__}"
                )
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Div) and right == 0.0:
                raise ZeroDivisionError(
                    f"0 나누기 [{_ctx_str}]: expr={expr!r}, "
                    f"우변=0 (변수: {ast.unparse(node.right) if hasattr(ast, 'unparse') else '?'})"
                )
            return op(left, right)
        if isinstance(node, ast.UnaryOp):
            op = _SAFE_OPS.get(type(node.op))
            if op is None:
                raise ValueError(
                    f"허용되지 않은 단항 연산자 [{_ctx_str}]: "
                    f"expr={expr!r}, 연산자={type(node.op).__name__}"
                )
            return op(_eval(node.operand))
        raise ValueError(
            f"허용되지 않은 AST 노드 [{_ctx_str}]: "
            f"expr={expr!r}, 노드={type(node).__name__} "
            f"(함수 호출·속성 접근·비교 연산 등은 DSL에서 지원하지 않음)"
        )

    return _eval(tree)


def _eval_expr(
    net_cfg: dict,
    cols: dict,
    shikiri: float,
    teiban_joken: float,
    *,
    _form_id: str = "",
) -> float:
    """formula_type == 'expr' 경로 처리.
    1. vars 해석 → ctx 변수 주입
    2. computed_vars 해석 (divide_by 포함)
    3. 본 expr 실행

    _form_id: 오류 메시지에 포함할 form 식별자.
    """
    ctx: dict = {
        "shikiri": shikiri,
        "teiban":  teiban_joken,
    }

    # 1. vars
    for alias, field in (net_cfg.get("vars") or {}).items():
        ctx[alias] = to_f(cols.get(field) if field else None, 0) or 0.0

    # 2. computed_vars
    for var_name, var_cfg in (net_cfg.get("computed_vars") or {}).items():
        cv_expr = var_cfg.get("expr", "")
        if not cv_expr:
            raise ValueError(
                f"computed_vars[{var_name!r}].expr가 비어 있음 [form={_form_id!r}]"
            )
        try:
            base = _safe_eval(
                cv_expr, ctx,
                _form_id=_form_id, _label=f"computed_vars.{var_name}",
            )
        except (ValueError, ZeroDivisionError) as e:
            raise ValueError(
                f"computed_vars[{var_name!r}] 계산 실패 [form={_form_id!r}]: {e}"
            ) from e

        divide_by = var_cfg.get("divide_by")
        if divide_by:
            when = divide_by.get("when", {})
            condition_met = (
                cols.get(when.get("field", ""), "") == when.get("equals", "")
                if when else True
            )
            if condition_met:
                divisor_field = divide_by["field"]
                divisor = to_f(cols.get(divisor_field), 0) or 0.0
                zero_policy = divide_by.get("zero_policy", "skip_divide")
                if divisor > 0:
                    base = base / divisor
                elif zero_policy == "return_none":
                    return None  # type: ignore[return-value]
                # zero_policy == "skip_divide" → 나누지 않음 (base 유지)
                # divisor=0 + skip_divide → 로그 없이 통과 (정상 정책)
            else:
                default = divide_by.get("default", 1)
                if default != 1:
                    base = base / default
        ctx[var_name] = base

    # 3. 본 expr
    main_expr = net_cfg.get("expr", "")
    if not main_expr:
        raise ValueError(
            f"net.expr가 비어 있음 [form={_form_id!r}] — form_types.json을 확인하세요"
        )
    return _safe_eval(main_expr, ctx, _form_id=_form_id, _label="net.expr")


# ── Step 2: 전처리 (JSON 규칙 실행) ──────────────────────────────────────────
def preprocess(form_id, cols):
    cols = dict(cols)
    cfg = FORM_TYPES.get(form_id, {})
    for rule in cfg.get("preprocess", []):
        field = rule["field"]
        guards = rule.get("guard_fields", [])
        if guards and not any(cols.get(g) for g in guards):
            continue
        if rule["op"] == "divide_by_100":
            v = to_f(cols.get(field))
            if v is not None:
                cols[field] = v / 100
    return cols

# ── Step 3: NET 수식 (JSON 라우팅) ────────────────────────────────────────────
def calc_net(form_id, cols, shikiri, teiban_joken=0.0):
    """결정적 NET 계산. LLM 없음. 수식은 config/form_types.json에서 읽음."""
    cfg = FORM_TYPES.get(form_id, {})
    net_cfg = cfg.get("net", {})

    # ── Layer 1: DSL 경로 ────────────────────────────────────────────────────
    if net_cfg.get("formula_type") == "expr":
        return _eval_expr(net_cfg, cols, shikiri, teiban_joken, _form_id=form_id)


    # ── 하위 호환: named formula 경로 ────────────────────────────────────────
    formula = net_cfg.get("formula")

    if formula == "subtract_conditions":
        c1 = to_f(cols.get(net_cfg["c1"],  0), 0)
        c2 = to_f(cols.get(net_cfg.get("c2") or "", 0), 0)
        if net_cfg.get("cs_divide_by_case_qty") and cols.get("数量単位") == "CS":
            case_qty = to_f(cols.get("ケース入数", 0), 0)
            if case_qty > 0:
                return shikiri - (c1 + c2) / case_qty
        return shikiri - (c1 + c2)

    elif formula == "subtract_conditions_or_fallback":
        c1 = to_f(cols.get(net_cfg["c1"], 0), 0)
        c2 = to_f(cols.get(net_cfg.get("c2") or "", 0), 0)
        joken = c1 + c2
        if joken != 0:
            return shikiri - joken
        return shikiri - to_f(cols.get(net_cfg["fallback"], 0), 0)

    elif formula == "subtract_teiban_and_self":
        u_self = to_f(cols.get(net_cfg["self_field"], 0), 0)
        return shikiri - teiban_joken - u_self

    elif formula == "subtract_pack_conditions":
        c1 = to_f(cols.get(net_cfg["c1"], 0), 0)
        c2 = to_f(cols.get(net_cfg.get("c2") or "", 0), 0)
        case_in = to_f(cols.get(net_cfg["divisor"], 0), 0)
        if case_in <= 0:
            return None
        return shikiri - (c1 + c2) / case_in

    _supported_legacy = [
        "subtract_conditions",
        "subtract_conditions_or_fallback",
        "subtract_teiban_and_self",
        "subtract_pack_conditions",
    ]
    if formula is None:
        raise ValueError(
            f"net 수식 미정의 [form={form_id!r}]: "
            f"net.formula_type 또는 net.formula가 없습니다. "
            f"form_types.json을 확인하세요."
        )
    raise ValueError(
        f"지원하지 않는 legacy formula [form={form_id!r}]: formula={formula!r}. "
        f"지원되는 legacy formula: {_supported_legacy}. "
        f"신규 양식은 formula_type=expr 사용을 권장합니다."
    )

# ── CSV 마스터 로드 ───────────────────────────────────────────────────────────
_sheets_store = _get_sheets_store()

def load_csv_dict(filename, key_col, base_dir=None):
    if _sheets_store:
        rows = _sheets_store.read_csv(filename)
        if rows:
            return {r[key_col]: r for r in rows if r.get(key_col)}
    path = (base_dir or BASE) / "mappings" / filename
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return {r[key_col]: r for r in rows}

# ── 출력 포맷터 ───────────────────────────────────────────────────────────────
def fmt(c, v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.4f}" if ("売上" in c or c == "ケース計") else f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)

# ── Summary 출력 헬퍼 ─────────────────────────────────────────────────────────
def _find_summary_val(ckey: str, stotals: dict) -> Optional[float]:
    """Summary totals에서 거래처키 매칭. 괄호 코드·공백 차이를 허용."""
    v = to_f(stotals.get(ckey))
    if v is not None:
        return v
    ckey_clean = re.sub(r'\s*\(\d+\)\s*$', '', ckey).strip()
    ckey_nsp   = re.sub(r'\s+', '', ckey_clean)
    for sk, sv in stotals.items():
        sk_clean = re.sub(r'\s*\(\d+\)\s*$', '', sk).strip()
        if ckey_clean == sk_clean or re.sub(r'\s+', '', sk_clean) == ckey_nsp:
            return to_f(sv)
    return None

# ── Summary 출력 함수 (cover_keys로 파라미터화) ──────────────────────────────
def print_summary_rate_then_customer(rows_out, summary_totals, cover_totals, cover_keys):
    """소비세율별 → 得意先별 2단계 계층 합계."""
    hasso_key  = cover_keys.get("hasso",  "")
    yakumu_key = cover_keys.get("yakumu", "")
    tax_8_key  = cover_keys.get("tax_8",  "")
    tax_10_key = cover_keys.get("tax_10", "")

    rate_sums: dict[str, float] = {}
    for r in rows_out:
        rate = r["_zei_rate"] or "—"
        rate_sums[rate] = rate_sums.get(rate, 0.0) + r["_kin_gaku"]

    hasso_keisan = sum(rate_sums.values())
    cover_hasso  = to_f(cover_totals.get(hasso_key),  0) if hasso_key  else 0
    cover_yakumu = to_f(cover_totals.get(yakumu_key), 0) if yakumu_key else 0
    cover_total  = (cover_hasso or 0) + (cover_yakumu or 0)

    print("\n[계층 합계]")
    print(f"  ── 레벨 1: 소비세율별 소계 (Cover 페이지) ───────────────────────────────────")
    print(f"  {'구분':<36} {'Detail계산(税抜)':>16} {'Cover기준':>14} 일치")
    print(f"  {'─'*72}")
    for rate, s in sorted(rate_sums.items()):
        print(f"  {rate:<36} {s:>16,.0f} {'—':>14}")
    print(f"  {'販促金請求 小計(税抜)':<36} {hasso_keisan:>16,.0f} {'—':>14}")
    cv_ok = "✅" if abs(hasso_keisan - cover_total) < 1 else "⚠️ 불일치"
    print(f"  {'Cover合計':<36} {hasso_keisan:>16,.0f} {cover_total:>14,.0f} {cv_ok}")
    print()

    merged_order: list[str] = []
    merged_sums:  dict[str, float] = {}
    merged_tax8:  dict[str, float] = {}
    merged_tax10: dict[str, float] = {}
    merged_svkey: dict[str, Optional[float]] = {}

    for r in rows_out:
        ckey       = r["_customer_ocr"]
        ckey_clean = re.sub(r'\s*\(\d+\)\s*$', '', ckey).strip()
        norm       = re.sub(r'\s+', '', ckey_clean)
        if norm not in merged_sums:
            merged_order.append(norm)
            merged_sums[norm]  = 0.0
            merged_tax8[norm]  = 0.0
            merged_tax10[norm] = 0.0
            merged_svkey[norm] = _find_summary_val(ckey, summary_totals)
        merged_sums[norm]  += r["_kin_gaku"]
        _rm = re.search(r"(\d+)", r.get("_zei_rate") or "")
        if _rm and int(_rm.group(1)) >= 10:
            merged_tax10[norm] += r["_kin_gaku"]
        else:
            merged_tax8[norm]  += r["_kin_gaku"]

    print(f"  ── 得意先별 소계 (vs Summary) ──────────────────────────────────────────────")
    print(f"  {'得意先名':<44} {'Detail+税(税込)':>16} {'Summary(税込)':>14} 일치")
    print(f"  {'─'*86}")
    for norm in merged_order:
        t8  = math.floor(merged_tax8[norm]  * _RATE_8)
        t10 = math.floor(merged_tax10[norm] * _RATE_10)
        ck_incl = merged_sums[norm] + t8 + t10
        sv = merged_svkey[norm]
        if sv is not None:
            ok_mark = "✅" if abs(ck_incl - sv) < 2 else "⚠️ 불일치"
            sv_str  = f"{sv:>14,.0f}"
        else:
            ok_mark = "—"
            sv_str  = f"{'—':>14}"
        print(f"  {norm[:43]:<44} {ck_incl:>16,.0f} {sv_str} {ok_mark}")


def print_summary_invoice_totals(rows_out, cover_pages, cover_keys, detail_ex):
    """청구서(cover 페이지)별 합계표 + Detail 교차검증."""
    honbai_key = cover_keys.get("honbai", "本体合計金額")
    tax_key    = cover_keys.get("tax",    "消費税金額")
    total_key  = cover_keys.get("total",  "合計ご請求金額")

    print("\n[계층 합계]")
    print(f"  {'Page':<6} {'本体合計':>12} {'消費税':>10} {'税込合計':>12}")
    print(f"  {'─'*46}")

    total_honbai = total_tax = total_total = 0
    for ct in cover_pages:
        page_num = ct.get("_page", "?")
        honbai   = int(ct.get(honbai_key) or 0)
        tax      = int(ct.get(tax_key)    or 0)
        total    = int(ct.get(total_key)  or 0)
        print(f"  {page_num:<6} {honbai:>12,} {tax:>10,} {total:>12,}")
        total_honbai += honbai
        total_tax    += tax
        total_total  += total

    print(f"  {'─'*46}")
    print(f"  {'合計':<6} {total_honbai:>12,} {total_tax:>10,} {total_total:>12,}")

    diff    = detail_ex - total_honbai
    ok_mark = "✅ 일치" if abs(diff) < 1 else f"⚠️ 불일치 ({diff:+,.0f}円)"
    print(f"\n  Detail金額合計: {detail_ex:>12,}  Cover{honbai_key}: {total_honbai:>12,}  →  {ok_mark}")


# ── 교차검증 (config-driven, form_id 분기 없음) ───────────────────────────────
def calc_cross_validation(
    form_cfg: dict,
    rows_out: list[dict],
    cover_pages: list[dict],
    summary_totals: dict,
    detail_ex: float,
    jisho_filter: set | None = None,
) -> list[tuple]:
    """form_types.json의 cross_validation 배열을 순서대로 실행.
    jisho_filter: 번들별 모드에서 특정 지점만 보고 싶을 때 jisho 이름 집합을 전달.
    """
    # 첫 번째 cover 페이지 totals (단일 cover 양식용)
    cover_totals = (
        {k: v for k, v in cover_pages[0].items() if not k.startswith("_")}
        if cover_pages else {}
    )

    # breakdown 집계 (breakdown_key가 있는 양식만)
    breakdown_key = form_cfg.get("cover_totals", {}).get("breakdown_key")
    breakdown_totals: dict[str, int] = {}
    if breakdown_key:
        for ct in cover_pages:
            for jname, jamt in (ct.get(breakdown_key) or {}).items():
                breakdown_totals[jname] = breakdown_totals.get(jname, 0) + int(jamt or 0)

    xv: list[tuple] = []

    for rule in form_cfg.get("cross_validation", []):
        rtype = rule["type"]

        if rtype == "cover_honbai_vs_detail":
            cv_total = sum(int(ct.get(rule["cover_key"]) or 0) for ct in cover_pages)
            if cv_total > 0:
                xv.append((rule["label"], cv_total, detail_ex, abs(detail_ex - cv_total) < 1))

        elif rtype == "cover_breakdown_vs_detail":
            group_field = rule["detail_group_field"]
            detail_by_group: dict[str, int] = {}
            for r in rows_out:
                if r.get(group_field):
                    g = r[group_field]
                    detail_by_group[g] = detail_by_group.get(g, 0) + int(r["_kin_gaku"] or 0)
            for jname, cv_amt in sorted(breakdown_totals.items()):
                if jisho_filter and jname not in jisho_filter:
                    continue
                det_amt = detail_by_group.get(jname, 0)
                label   = rule["label"].replace("{key}", jname)
                xv.append((label, cv_amt, det_amt, abs(det_amt - cv_amt) < 1))

        elif rtype == "cover_taxex_vs_detail":
            cv_8  = to_f(cover_totals.get(rule["cover_key_8"],  0), 0)
            cv_10 = to_f(cover_totals.get(rule["cover_key_10"], 0), 0)
            cv_ex = cv_8 + cv_10
            if cv_ex > 0:
                xv.append((rule["label"], cv_ex, detail_ex, abs(detail_ex - cv_ex) < 1))

        elif rtype == "cover_total_vs_summary":
            cv_total = to_f(cover_totals.get(rule["cover_key"], 0), 0)
            sv_total = sum(to_f(v, 0) for v in summary_totals.values()) if summary_totals else 0
            if cv_total > 0 and sv_total > 0:
                xv.append((rule["label"], cv_total, sv_total, abs(sv_total - cv_total) < 1))

        elif rtype == "summary_vs_detail":
            sv = sum(to_f(v, 0) for v in summary_totals.values())
            if sv > 0:
                xv.append((rule["label"], sv, detail_ex, abs(detail_ex - sv) < 1))

        elif rtype == "per_customer_vs_summary":
            merged_sums:  dict[str, float] = {}
            merged_tax8:  dict[str, float] = {}
            merged_tax10: dict[str, float] = {}
            merged_svkey: dict[str, Optional[float]] = {}

            for r in rows_out:
                ckey       = r["_customer_ocr"]
                ck_clean   = re.sub(r"\s*\(\d+\)\s*$", "", ckey).strip()
                norm       = re.sub(r"\s+", "", ck_clean)
                if norm not in merged_sums:
                    merged_sums[norm]  = 0.0
                    merged_tax8[norm]  = 0.0
                    merged_tax10[norm] = 0.0
                    merged_svkey[norm] = _find_summary_val(ckey, summary_totals)
                merged_sums[norm] += r["_kin_gaku"]
                _rm = re.search(r"(\d+)", r.get("_zei_rate") or "")
                if _rm and int(_rm.group(1)) >= 10:
                    merged_tax10[norm] += r["_kin_gaku"]
                else:
                    merged_tax8[norm]  += r["_kin_gaku"]

            for norm, total_ex_cust in merged_sums.items():
                sv = merged_svkey[norm]
                if sv is None:
                    continue
                label = rule["label"].replace("{key}", norm[:20])
                xv.append((label, sv, total_ex_cust, abs(total_ex_cust - sv) < 2))

    return xv


# ── 메인 처리 로직 ────────────────────────────────────────────────────────────
def run(doc_id, save=False, summary_only=False, base_dir=None):
    """
    phase3_output.json을 읽어 NET 계산·교차검증·출력을 수행한다.
    base_dir: 테스트 시 임시 디렉토리 경로를 주입할 수 있음 (기본값: BASE)
    반환값: (rows_out, xv) — 테스트용
    """
    _t0 = time.time()
    root = Path(base_dir) if base_dir else BASE
    doc_dir = root / "extracted" / doc_id

    p3_path = doc_dir / "phase3_output.json"
    if not p3_path.exists():
        sys.exit(f"[오류] {p3_path} 없음 — Phase 3 완료 후 실행하세요")

    with open(p3_path, encoding="utf-8") as f:
        p3 = json.load(f)

    form_id     = p3["form_id"]
    hatsu_month = p3["hatsu_month"]
    items_in    = p3["items"]

    if form_id not in FORM_TYPES:
        sys.exit(f"[오류] '{form_id}'가 config/form_types.json에 없음 — 양식 정의를 추가하세요")

    form_cfg = FORM_TYPES[form_id]
    net_cfg  = form_cfg.get("net", {})

    # cover_breakdown_vs_detail 교차검증에 사용할 그룹 필드명 (config에서 읽음)
    _breakdown_field: str | None = None
    for _xv in form_cfg.get("cross_validation", []):
        if _xv.get("type") == "cover_breakdown_vs_detail":
            _breakdown_field = _xv.get("detail_group_field")
            break

    # ── Cover/Summary 페이지 로드 (Phase 2 output) ───────────────────────────
    # cover_pages: [{"_page": N, <totals 키들>}, ...]  — 메타(_page)는 _ prefix
    cover_pages: list[dict] = []
    summary_totals: dict = {}
    bundles_info: list[dict] = []

    p2_path = doc_dir / "phase2_output.json"
    if p2_path.exists():
        with open(p2_path, encoding="utf-8") as f:
            p2 = json.load(f)
        bundles_info = p2.get("bundles", [])
        for page in p2.get("pages", []):
            role = page.get("role")
            if role == "cover":
                totals = page.get("totals") or {}
                cover_pages.append({"_page": page.get("page"), **totals})
            elif role == "summary":
                raw = page.get("totals") or page.get("customer_summaries") or {}
                for k, v in raw.items():
                    k2 = re.sub(r"^得意先\s*小計\s*", "", k).strip()
                    summary_totals[k2] = v

    # 단일 cover 양식용 편의 변수 (_page 메타 제거)
    cover_totals = (
        {k: v for k, v in cover_pages[0].items() if not k.startswith("_")}
        if cover_pages else {}
    )

    # ── Teiban precompute ────────────────────────────────────────────────────
    # subtract_teiban_and_self 수식에서만 필요.
    # teiban_type은 config에서 읽음 — 하드코딩 금지.
    teiban_map: dict[tuple, float] = {}
    if net_cfg.get("needs_teiban") or net_cfg.get("formula") == "subtract_teiban_and_self":
        teiban_type = net_cfg.get("teiban_type", "定番条件")
        for _item in items_in:
            if _item.get("condition_type") == teiban_type:
                _key = (_item.get("customer_ocr", ""), _item.get("product_ocr", ""))
                _cols_pre = preprocess(form_id, _item.get("columns", {}))
                # self_field: named formula 호환 / DSL은 vars.c1 에서 읽음
                _sf = net_cfg.get("self_field") or (net_cfg.get("vars") or {}).get("c1", "")
                _u = to_f(_cols_pre.get(_sf, 0), 0)
                teiban_map[_key] = _u

    cond_disp = form_cfg.get("condition_display", {})
    cond_mode = cond_disp.get("mode", "keesu")

    unit_price    = load_csv_dict("unit_price.csv",   "제품코드",  root)
    retail_master = load_csv_dict("retail_user.csv",  "소매처코드", root)
    # 마스터 빈 결과 = Sheets 토큰/네트워크 장애 (운영상 빈 적 없음).
    # NET을 전부 None으로 '조용히' 계산하지 않고 명시적으로 실패한다.
    if not unit_price:
        sys.exit("[오류] unit_price 마스터가 비어 있음 — Sheets 토큰/네트워크 확인 필요 (NET 계산 중단)")

    retail_tantou:    dict[str, list[str]] = {}
    retail_tantou_id: dict[str, list[str]] = {}
    dist_name_by_code: dict[str, str] = {}   # 판매처코드 → 판매처명 2차 조회용
    _ru_rows = (_sheets_store.read_csv("retail_user.csv") if _sheets_store else [])
    if not _ru_rows:
        with open(root / "mappings" / "retail_user.csv", encoding="utf-8-sig") as f:
            _ru_rows = list(csv.DictReader(f))
    for r in _ru_rows:
        retail_tantou.setdefault(r["소매처코드"],    []).append(r["담당자명"])
        retail_tantou_id.setdefault(r["소매처코드"], []).append(r["ID"])
        dc = r.get("판매처코드", "").strip()
        dn = r.get("판매처명",   "").strip()
        if dc and dn:
            dist_name_by_code[dc] = dn

    rows_out: list[dict] = []

    for item in items_in:
        cols          = preprocess(form_id, item.get("columns", {}))
        retailer_code = item.get("retailer_code", "")
        dist_code     = item.get("dist_code", "")
        prod_code     = item.get("product_code")
        unconfirmed    = item.get("unconfirmed", False)
        customer_ocr   = item.get("customer_ocr", "")
        product_ocr    = item.get("product_ocr", "")
        condition_type = item.get("condition_type", "")
        applied_conds = item.get("applied_conditions", [])

        # 条件区分ベースのkubun（by_kubunモード用）
        kubun_field = cond_disp.get("kubun_field")
        kubun_val   = (cols.get(kubun_field, "") or "") if kubun_field else ""
        no_net_kubun = net_cfg.get("no_net_kubun", [])

        sr            = retail_master.get(retailer_code, {})
        retailer_name = sr.get("소매처명", "").lstrip("■").strip()
        # 1차: 소매처코드 → 판매처명, 2차: 판매처코드 → 판매처명, fallback: dist_code
        dist_name = sr.get("판매처명", "").strip() or dist_name_by_code.get(dist_code, dist_code)

        tantousha_list = retail_tantou.get(retailer_code, [])
        tantousha = (tantousha_list[0] if len(tantousha_list) == 1
                     else "·".join(sorted(set(tantousha_list))) if tantousha_list
                     else "")
        tantousha_id_list = retail_tantou_id.get(retailer_code, [])
        tantousha_id = (tantousha_id_list[0] if len(tantousha_id_list) == 1
                        else "·".join(sorted(set(tantousha_id_list))) if tantousha_id_list
                        else "")

        up           = unit_price.get(prod_code, {}) if prod_code else {}
        shikiri      = to_f(up.get("시키리"))
        honbucho     = to_f(up.get("본부장"))
        product_name = up.get("제품명", product_ocr)
        keesu_iru    = to_f(up.get("단일상자환산값"))                         # N: ケース入数
        _k2          = to_f(up.get("2합환산값"))
        booru_iru    = (int(_k2 / keesu_iru) if (_k2 and keesu_iru and keesu_iru > 0) else None)  # O: ボール入数

        qty_fields = form_cfg.get("qty_field", ["数量"])
        if isinstance(qty_fields, str):
            qty_fields = [qty_fields]
        qty = next((to_f(cols.get(f, 0), 0) for f in qty_fields if cols.get(f)), 0.0)
        unit_val = cols.get("数量単位", "")
        case_qty = to_f(cols.get("ケース入数", 0), 0)
        kin_gaku = to_f(cols.get("金額",      0), 0)
        zei_rate = cols.get("消費税率", "")

        bara_source = form_cfg.get("bara_source", "by_unit")
        if bara_source == "null":
            keesu = booru = 0
            bara = None
            kosuu_kei = 0
        elif bara_source.startswith("column:"):
            col_name = bara_source[7:]
            keesu = booru = 0
            bara = int(to_f(cols.get(col_name, 0), 0))
            kosuu_kei = bara
        else:  # "by_unit" — 数量単位=CS/個 분기 (form_01)
            if unit_val == "CS":
                keesu, booru = int(qty), 0
                bara = 0
                _iru = keesu_iru or case_qty
                kosuu_kei = int(keesu * _iru) if _iru else 0
            else:
                keesu, booru = 0, 0
                bara = int(qty)
                kosuu_kei = bara
        _iru_div  = keesu_iru or case_qty
        keesu_kei = kosuu_kei / _iru_div if _iru_div else 0.0

        net = None
        if shikiri is not None and kubun_val not in no_net_kubun:
            if net_cfg.get("needs_teiban") or net_cfg.get("formula") == "subtract_teiban_and_self":
                teiban_type = net_cfg.get("teiban_type", "定番条件")
                ctype = item.get("condition_type", teiban_type)
                tj = 0.0 if ctype == teiban_type else teiban_map.get((customer_ocr, product_ocr), 0.0)
                net = calc_net(form_id, cols, shikiri, teiban_joken=tj)
            else:
                net = calc_net(form_id, cols, shikiri)

        shikiri_uriage = (shikiri * kosuu_kei / 1000) if shikiri and kosuu_kei else None
        type_val = item.get("item_type", "条件")

        if cond_mode == "pack":
            c1_pack  = to_f(cols.get(cond_disp.get("c1", "")))
            c2_pack  = to_f(cols.get(cond_disp.get("c2", ""), 0), 0)
            c1_keesu = c2_keesu = None
        elif cond_mode == "by_kubun":
            pack_kubun  = cond_disp.get("pack_kubun",  "個")
            keesu_kubun = cond_disp.get("keesu_kubun", "CS")
            if kubun_val == pack_kubun:
                c1_pack  = to_f(cols.get(cond_disp.get("c1", "条件")))
                c2_pack  = to_f(cols.get(cond_disp.get("c2", "条件2"), 0), 0)
                c1_keesu = c2_keesu = None
            elif kubun_val == keesu_kubun:
                c1_pack  = c2_pack = None
                c1_keesu = to_f(cols.get(cond_disp.get("c1", "条件")))
                c2_keesu = to_f(cols.get(cond_disp.get("c2", "条件2"), 0), 0)
            else:  # 円, % 등 — 条件 컬럼 공백
                c1_pack = c2_pack = c1_keesu = c2_keesu = None
        else:  # keesu (기본값)
            c1_pack = c2_pack = None
            c1_keesu = to_f(cols.get(cond_disp.get("c1", "条件")))
            c2_keesu = to_f(cols.get(cond_disp.get("c2", "条件2"), 0), 0)

        # AF/AG: 조건 단위 통합 (output-format.md 수식, 条件（ボール）은 미구현)
        if kubun_val not in no_net_kubun:
            af_val = (c1_pack or 0.0) + (c1_keesu / keesu_iru if (c1_keesu and keesu_iru) else 0.0)
            ag_val = (c2_pack or 0.0) + (c2_keesu / keesu_iru if (c2_keesu and keesu_iru) else 0.0)
        else:
            af_val = ag_val = None

        rows_out.append({
            "受注先":          dist_name,
            "受注先コード":    dist_code,
            "condition_type":  condition_type,
            **({_breakdown_field: _clean_cell(item.get(_breakdown_field) or "")} if _breakdown_field else {}),
            "担当者":          tantousha,
            "担当者ID":        tantousha_id,
            "ロットアウト判別用": extract_n_token(applied_conds),
            "代表スーパー":    retailer_code,
            "スーパー":        retailer_name,
            "商品名":          product_name,
            "商品コード":      prod_code or "",
            "ケース入数":      keesu_iru,
            "ボール入数":      booru_iru,
            "ケース":          keesu,
            "ボール":          booru,
            "バラ":            bara,
            "個数計":          kosuu_kei,
            "ケース計":        round(keesu_kei, 4),
            "発生月":          hatsu_month,
            "仕切":            shikiri,
            "仕切売上":        round(shikiri_uriage, 4) if shikiri_uriage is not None else None,
            "条件1（パック）": c1_pack,
            "条件2（パック）": c2_pack,
            "条件1（ボール）": None,
            "条件2（ボール）": None,
            "条件1（ケース）": c1_keesu,
            "条件2（ケース）": c2_keesu,
            "Q":               (int(keesu * keesu_iru) if (keesu and keesu_iru) else None),
            "S":               None,
            "AF":              round(af_val, 4) if af_val is not None else None,
            "AG":              round(ag_val, 4) if ag_val is not None else None,
            "タイプ":          type_val,
            "本部長価格":      honbucho,
            "NET":             round(net, 4) if net is not None else None,
            "net_lt_honbu":    False,
            "unconfirmed":     unconfirmed,
            "_invoice_no":     item.get("invoice_no", ""),
            "_page_number":    (item.get("source_pages") or [None])[0],
            "_customer_ocr":   customer_ocr,
            "_product_ocr":    product_ocr,
            "未収金額合計":    int(kin_gaku) if kin_gaku else 0,
            "_kin_gaku":       kin_gaku,
            "_zei_rate":       zei_rate,
            "_flag":           "⚠️" if unconfirmed else "",
        })

    # ── 교차검증 (config-driven) ─────────────────────────────────────────────
    detail_ex = sum(r["_kin_gaku"] for r in rows_out)
    xv = calc_cross_validation(form_cfg, rows_out, cover_pages, summary_totals, detail_ex)

    # ── 번들별 교차검증 ─────────────────────────────────────────────────────────
    bundle_xv_list: list[dict] = []
    if bundles_info:
        for b in bundles_info:
            p_start, p_end = b["page_range"]
            cover_pg = b["cover_page"]
            b_rows = [r for r in rows_out
                      if (r.get("_page_number") or 0) >= p_start
                      and (r.get("_page_number") or 0) <= p_end]
            b_cover_pages = [cp for cp in cover_pages if cp.get("_page") == cover_pg]
            b_detail_ex = sum(r["_kin_gaku"] for r in b_rows)
            b_jishos = {r.get(_breakdown_field) for r in b_rows if _breakdown_field and r.get(_breakdown_field)}
            b_xv = calc_cross_validation(
                form_cfg, b_rows, b_cover_pages, {}, b_detail_ex,
                jisho_filter=b_jishos or None,
            )
            jisho_label = (b_rows[0].get(_breakdown_field) if (_breakdown_field and b_rows) else None) or f"묶음 {b['bundle_idx']+1}"
            bundle_xv_list.append({
                "bundle_idx": b["bundle_idx"],
                "jisho": jisho_label,
                "cover_page": cover_pg,
                "xv": [{"label": l, "expected": e, "actual": a, "ok": ok, "xv_type": "simple"}
                       for l, e, a, ok in b_xv],
            })

    # NET < 本部長価格 경보
    for r in rows_out:
        net_v = r["NET"]
        hon_v = r["本部長価格"]
        if net_v is not None and hon_v is not None and net_v < hon_v:
            r["net_lt_honbu"] = True
            r["_flag"] = (r["_flag"] + " ⚠️NET<本部長").strip()

    # ── 출력 ────────────────────────────────────────────────────────────────
    COLS = [
        "受注先", "受注先コード", "担当者", "ロットアウト判別用",
        "代表スーパー", "スーパー", "商品名", "商品コード",
        "ケース入数", "ボール入数",
        "ケース", "ボール", "バラ", "個数計", "ケース計",
        "発生月", "仕切", "仕切売上",
        "条件1（パック）", "条件2（パック）",
        "条件1（ボール）", "条件2（ボール）",
        "条件1（ケース）", "条件2（ケース）",
        "AF", "AG",
        "タイプ", "本部長価格", "NET",
    ]

    SEP = "=" * 100
    print(SEP)
    print(f"  Phase 4 — {doc_id}  ({form_id})")
    print(SEP)

    print("\n[교차검증]")
    for label, expected, actual, ok in xv:
        mark = "✅ 일치" if ok else "⚠️ 불일치"
        print(f"  {label}: 기준={expected:,.0f}  실계={actual:,.0f}  →  {mark}")

    # Summary 출력 (config-driven)
    summary_style = form_cfg.get("summary", "standard")
    cover_keys    = form_cfg.get("summary_cover_keys", {})
    if summary_style == "rate_then_customer":
        print_summary_rate_then_customer(rows_out, summary_totals, cover_totals, cover_keys)
    elif summary_style == "invoice_totals":
        print_summary_invoice_totals(rows_out, cover_pages, cover_keys, detail_ex)
    # "standard": 별도 summary 출력 없음

    if not summary_only:
        print(f"\n{'─'*100}")
        print("  " + " | ".join(COLS))
        print("  " + "-" * 200)
        grouped: dict[str, list] = defaultdict(list)
        for r in rows_out:
            grouped[r["_customer_ocr"]].append(r)
        for cust, rows in grouped.items():
            print(f"\n  ■ {cust}")
            for r in rows:
                prefix = "⚠️" if r["_flag"] else "  "
                print(f"  {prefix} " + " | ".join(fmt(c, r[c]) for c in COLS))

    print(f"\n{SEP}")
    net_alerts = [r for r in rows_out if "NET<本部長" in r["_flag"]]
    if net_alerts:
        print("  NET < 本部長 경보:")
        for r in net_alerts:
            print(f"    {r['_customer_ocr']} / {r['_product_ocr']} : NET={r['NET']} 本部長={r['本部長価格']}")
    else:
        print("  NET < 本部長 경보: 없음")

    # ── 세율별 집계 (UI summary 섹션용) ──────────────────────────────────────
    _rate_sums: dict[str, int] = {}
    for _r in rows_out:
        _kg = int(_r["_kin_gaku"])
        _raw = _r.get("_zei_rate") or ""
        _m = re.search(r"(\d+)", _raw)
        _key = f"{_m.group(1)}%" if _m else "—"
        _rate_sums[_key] = _rate_sums.get(_key, 0) + _kg
    summary = {
        "by_rate":  _rate_sums,
        "total_ex": int(detail_ex),
    }

    if save:
        out_path = doc_dir / "phase4_output.json"
        _internal = {"_kin_gaku", "_zei_rate", "_flag"}
        def _export(r):
            out = {}
            for k, v in r.items():
                if k in _internal:
                    continue
                out[k[1:] if k.startswith("_") else k] = v
            return out
        payload = {
            "doc_id":  doc_id,
            "form_id": form_id,
            "xv":      [{"label": l, "expected": e, "actual": a, "ok": ok, "xv_type": "simple"}
                        for l, e, a, ok in xv],
            "rows":    [_export(r) for r in rows_out],
            "summary": summary,
        }
        if form_cfg.get("show_sections"):
            payload["show_sections"] = form_cfg["show_sections"]
        if form_cfg.get("aggregate_label"):
            payload["aggregate_label"] = form_cfg["aggregate_label"]
        if bundles_info:
            payload["bundles"] = [
                {"bundle_idx": b["bundle_idx"], "page_range": b["page_range"], "cover_page": b["cover_page"]}
                for b in bundles_info
            ]
        if bundle_xv_list:
            payload["bundle_xv"] = bundle_xv_list
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n  → {out_path} 저장 완료")

    # timing.json 기록
    _t1 = time.time()
    _duration = round(_t1 - _t0, 1)
    _timing_path = doc_dir / "timing.json"
    _td = json.loads(_timing_path.read_text(encoding="utf-8")) if _timing_path.exists() else {"doc_id": doc_id, "phases": {}}
    _ph = _td["phases"].setdefault("phase4", {})
    _ph["start"]        = _t0
    _ph["start_str"]    = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_t0))
    _ph["end"]          = _t1
    _ph["end_str"]      = time.strftime("%Y-%m-%d %H:%M:%S")
    _ph["duration_sec"] = _duration
    _timing_path.write_text(json.dumps(_td, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  [Phase 4 실행 시간: {_duration}초]")

    return rows_out, xv


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Phase 4: NET계산·교차검증")
    ap.add_argument("--doc",          required=True,       help="doc_id")
    ap.add_argument("--save",         action="store_true", help="phase4_output.json 저장")
    ap.add_argument("--summary-only", action="store_true", help="합계 섹션만 출력")
    args = ap.parse_args()
    run(args.doc, save=args.save, summary_only=args.summary_only)
