"""DSL 검증 게이트 프로토타입 (P1) — product_aggregate 대상.

자연어→DSL 파이프라인([docs/nl-to-dsl-pipeline.md])의 핵심인 "검증 게이트"의 첫 구현.
LLM이 form_types.json 설정을 작성/수정했을 때, 사람이 승인하기 전 자동으로 통과해야 하는
게이트들을 한 번에 돌린다. 하나라도 실패하면 승인 화면에 도달하지 못한다(exit!=0).

게이트:
  1. 스키마+정규화 — form_types.schema.json 통과 + 제어문자(널바이트 등) 거부
                     (Results.tsx 널바이트 버그의 직접 교훈)
  2. dry-run       — 샘플 문서에 build_product_aggregate 실행, 예외 없이 완주
  3. 불변식        — 분해 결과를 items에서 독립 재계산해 대조
                     (그룹 total_qty == 定番총수량, total_amount == 定番총액 + 추가조건액)
  4. 골든 diff     — 저장된 골든과 비교, 바뀐 그룹을 표면화 (최초엔 골든 생성)

사용:
  python scripts/validate_dsl_gate.py --form form_04 --doc "ＣＶＳ①"
  python scripts/validate_dsl_gate.py --form form_04 --doc "ＣＶＳ①" --update-golden
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.phase4_calc import build_product_aggregate  # noqa: E402

CONFIG_PATH  = ROOT / "config" / "form_types.json"
SCHEMA_PATH  = ROOT / "config" / "form_types.schema.json"
EXTRACTED    = ROOT / "extracted"
GOLDEN_DIR   = ROOT / "tests" / "fixtures" / "dsl_gate"

# 거부할 제어문자 (탭·개행·캐리지리턴 제외). 널바이트 포함.
_CTRL = {c for c in range(0x00, 0x20)} - {0x09, 0x0a, 0x0d}


def _find_doc(needle: str) -> Path | None:
    if not EXTRACTED.exists():
        return None
    nn = unicodedata.normalize("NFC", needle)
    for d in sorted(EXTRACTED.iterdir()):
        if d.is_dir() and nn in unicodedata.normalize("NFC", d.name):
            return d
    return None


def _ctrl_positions(text: str) -> list[tuple[int, str]]:
    return [(i, hex(ord(ch))) for i, ch in enumerate(text) if ord(ch) in _CTRL]


# ── Gate 1: 스키마 + 정규화 ──────────────────────────────────────────────────
def gate_schema(form_id: str) -> tuple[bool, str]:
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    ctrl = _ctrl_positions(raw)
    if ctrl:
        return False, f"form_types.json에 제어문자 {len(ctrl)}개 (예: {ctrl[:3]}) — 거부"
    cfg = json.loads(raw)
    if form_id not in cfg:
        return False, f"form_types.json에 {form_id} 없음"
    try:
        from jsonschema import Draft7Validator
    except ImportError:
        return True, "jsonschema 미설치 — 스키마 검증 건너뜀(제어문자 검사만 통과)"
    if not SCHEMA_PATH.exists():
        return True, "schema 파일 없음 — 제어문자 검사만 통과"
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = list(Draft7Validator(schema).iter_errors(cfg))
    if errors:
        e = errors[0]
        path = " → ".join(str(p) for p in e.absolute_path)
        return False, f"스키마 위반 {len(errors)}개 (첫: [{path}] {e.message})"
    return True, f"스키마 통과 + 제어문자 0"


# ── Gate 2: dry-run ──────────────────────────────────────────────────────────
def gate_dryrun(form_cfg: dict, items: list) -> tuple[bool, str, dict | None]:
    if "product_aggregate" not in form_cfg:
        return False, "이 양식에 product_aggregate 설정 없음 (게이트 대상 아님)", None
    # 필드 실재성 — 설정한 qty/amount 필드가 실제 items 컬럼에 존재하는지.
    # (불변식이 같은 필드로 재계산하면 오타 필드를 못 잡으므로 여기서 먼저 막는다)
    pa = form_cfg["product_aggregate"]
    cols_seen: set[str] = set()
    for it in items:
        cols_seen |= set(it.get("columns", {}).keys())
    for label, fld in (("qty_field", pa.get("qty_field", "数量")),
                       ("amount_field", pa.get("amount_field", "金額"))):
        if cols_seen and fld not in cols_seen:
            return False, f"{label}='{fld}' 가 items 컬럼에 없음 (오타 의심). 실제 컬럼 예: {sorted(cols_seen)[:6]}", None
    try:
        out = build_product_aggregate(items, form_cfg)
    except Exception as e:  # noqa: BLE001
        return False, f"build_product_aggregate 예외: {type(e).__name__}: {e}", None
    if not out or not out.get("groups"):
        return False, "분해 그룹 0개 — 추출/설정 점검 필요", None
    warn = out.get("warnings") or []
    msg = f"그룹 {len(out['groups'])}개 생성" + (f", 경고 {len(warn)}건" if warn else "")
    return True, msg, out


# ── Gate 3: 불변식 (items에서 독립 재계산) ───────────────────────────────────
def gate_invariants(form_cfg: dict, items: list, out: dict) -> tuple[bool, str]:
    cfg = form_cfg["product_aggregate"]
    base_type  = cfg.get("base_condition", "定番条件")
    qty_field  = cfg.get("qty_field", "数量")
    amt_field  = cfg.get("amount_field", "金額")

    # (jisho, customer, product_code) → {base_qty, base_amt, extra_amt}
    truth: dict[tuple, dict] = {}
    for it in items:
        ct = it.get("condition_type") or ""
        if not ct:
            continue
        pcode = it.get("product_code") or it.get("product_ocr") or ""
        key = (it.get("jisho", ""), it.get("customer_ocr") or it.get("customer", ""), pcode)
        t = truth.setdefault(key, {"base_qty": 0.0, "base_amt": 0.0, "extra_amt": 0.0})
        cols = it.get("columns", {})
        q = float(str(cols.get(qty_field, 0)).replace(",", "") or 0)
        a = float(str(cols.get(amt_field, 0)).replace(",", "") or 0)
        if ct == base_type:
            t["base_qty"] += q
            t["base_amt"] += a
        else:
            t["extra_amt"] += a

    EPS = 0.02
    viol = []
    for g in out["groups"]:
        key = (g.get("jisho", ""), g.get("customer", ""), g.get("product_code", ""))
        t = truth.get(key)
        if not t:
            viol.append(f"{key}: items에서 근거 못 찾음")
            continue
        exp_qty = t["base_qty"]
        exp_amt = t["base_amt"] + t["extra_amt"]
        if abs(g.get("total_qty", 0) - exp_qty) > EPS:
            viol.append(f"{g.get('product_name')}: 수량 {g.get('total_qty')} ≠ 定番총 {exp_qty}")
        if abs(g.get("total_amount", 0) - exp_amt) > EPS:
            viol.append(f"{g.get('product_name')}: 금액 {g.get('total_amount')} ≠ 定番+추가 {round(exp_amt,2)}")
    if viol:
        return False, f"불변식 위반 {len(viol)}건: " + " | ".join(viol[:4])
    return True, f"전 그룹 수량·금액 보존 확인 ({len(out['groups'])}그룹)"


# ── Gate 4: 골든 diff ────────────────────────────────────────────────────────
def _golden_view(out: dict) -> dict:
    """비교용 정규화 — 그룹키 → {total_qty, total_amount, rows}."""
    view = {}
    for g in out["groups"]:
        k = f"{g.get('jisho')}|{g.get('customer')}|{g.get('product_code')}"
        view[k] = {
            "total_qty": g.get("total_qty"),
            "total_amount": g.get("total_amount"),
            "rows": [{"qty": r.get("qty"), "amount": r.get("amount")} for r in g.get("rows", [])],
        }
    return view


def gate_golden(form_id: str, doc_name: str, out: dict, update: bool) -> tuple[bool, str]:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    safe = unicodedata.normalize("NFC", doc_name).replace("/", "_")
    gp = GOLDEN_DIR / f"{form_id}__{safe}.json"
    view = _golden_view(out)
    if update or not gp.exists():
        gp.write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8")
        return True, ("골든 갱신됨" if update else "골든 신규 생성 — 다음 실행부터 diff")
    prev = json.loads(gp.read_text(encoding="utf-8"))
    diffs = []
    for k in sorted(set(prev) | set(view)):
        if k not in prev:
            diffs.append(f"+신규 {k}")
        elif k not in view:
            diffs.append(f"-삭제 {k}")
        elif prev[k] != view[k]:
            diffs.append(f"~변경 {k}: 금액 {prev[k]['total_amount']}→{view[k]['total_amount']}")
    if diffs:
        return False, f"골든과 {len(diffs)}건 차이: " + " | ".join(diffs[:4])
    return True, "골든과 동일"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--form", required=True)
    ap.add_argument("--doc", required=True, help="extracted/ 하위 디렉토리 부분일치")
    ap.add_argument("--update-golden", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    form_cfg = cfg.get(args.form, {})
    docdir = _find_doc(args.doc)
    if not docdir:
        print(f"[중단] 문서 못 찾음: {args.doc}")
        return 2
    p3 = docdir / "phase3_output.json"
    if not p3.exists():
        print(f"[중단] phase3_output.json 없음: {docdir.name}")
        return 2
    items = json.loads(p3.read_text(encoding="utf-8")).get("items", [])

    print(f"검증 대상: {args.form} / {unicodedata.normalize('NFC', docdir.name)}  (items {len(items)})\n")
    results = []

    ok1, m1 = gate_schema(args.form)
    results.append(("1 스키마+정규화", ok1, m1))

    out = None
    if ok1:
        ok2, m2, out = gate_dryrun(form_cfg, items)
        results.append(("2 dry-run", ok2, m2))
        if ok2:
            ok3, m3 = gate_invariants(form_cfg, items, out)
            results.append(("3 불변식", ok3, m3))
            ok4, m4 = gate_golden(args.form, docdir.name, out, args.update_golden)
            results.append(("4 골든 diff", ok4, m4))

    print("─" * 70)
    allok = True
    for name, ok, msg in results:
        allok = allok and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {msg}")
    print("─" * 70)
    print("결과:", "✅ 전 게이트 통과 — 승인 가능" if allok else "❌ 실패 — 승인 차단")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
