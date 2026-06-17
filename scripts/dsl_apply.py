"""자연어→DSL 승인 게이트 + 동결 (P3).

P2(컴파일) + P1(검증 게이트)를 묶어, 현업이 승인하기 전에 보는 '승인 요약'을
만들고, 승인 시 컴파일된 설정을 form_types.json에 **동결**한다(백업·변경이력·
재검증). 동결 대상은 *자연어가 아니라 컴파일된 설정* — 자연어를 재실행하면
드리프트하므로 컴파일 산출물이 source of truth가 된다.

흐름:
  자연어 → 컴파일(P2) → 게이트(P1) → [승인 요약 표시]
    → (--apply 시) 백업 + form_types.json 동결 + 변경이력 기록 + 사후 재검증

미리보기:  python scripts/dsl_apply.py --form form_04 --doc "ＣＶＳ①" --rule "..."
동결:      python scripts/dsl_apply.py --form form_04 --doc "ＣＶＳ①" --rule "..." --apply
적용 방식은 merge — 컴파일이 누락한 기존 키(예: unit_field)는 보존한다.
"""
from __future__ import annotations
import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.validate_dsl_gate as G                       # noqa: E402
from scripts.compile_dsl_poc import compile_product_aggregate, _grounding  # noqa: E402

BACKUP_DIR = ROOT / "config" / ".form_types_backups"
CHANGELOG  = ROOT / "config" / "form_types_changelog.jsonl"


def _run_gates(form_cfg: dict, items: list, form_id: str, doc_name: str):
    """P1 게이트 실행 → (allok, [(name,ok,msg)], dry-run out)."""
    res = []
    ok1, m1 = G.gate_schema(form_id);              res.append(("스키마+정규화", ok1, m1))
    ok2, m2, out = G.gate_dryrun(form_cfg, items); res.append(("dry-run+필드", ok2, m2))
    allok = ok1 and ok2
    if ok2:
        ok3, m3 = G.gate_invariants(form_cfg, items, out); res.append(("불변식", ok3, m3))
        ok4, m4 = G.gate_golden(form_id, doc_name, out, update=False); res.append(("골든 diff", ok4, m4))
        allok = allok and ok3 and ok4
    return allok, res, out


# 숫자 결과에 영향을 주어 게이트(불변식·필드실재성)가 검증하는 필드.
# 그 외(unit_field 등 표시 전용)는 게이트가 못 잡으므로 사람 확인이 필요하다.
_GATE_VALIDATED = {"base_condition", "qty_field", "amount_field"}


def _config_diff(cur: dict, new: dict) -> tuple[list[str], bool]:
    keys = sorted(set(cur) | set(new))
    out = []
    has_review = False
    for k in keys:
        a, b = cur.get(k, "∅"), new.get(k, "∅")
        if a != b:
            if k in _GATE_VALIDATED:
                out.append(f"    {k}: {a!r} → {b!r}  (게이트 검증됨)")
            else:
                has_review = True
                out.append(f"  ⚠ {k}: {a!r} → {b!r}  (게이트 비검증 — 사람 확인 필요)")
    return out, has_review


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--form", required=True)
    ap.add_argument("--doc", required=True)
    ap.add_argument("--rule", required=True)
    ap.add_argument("--apply", action="store_true", help="승인하여 form_types.json에 동결")
    ap.add_argument("--confirm-display", action="store_true",
                    help="게이트 비검증(표시 전용) 필드 변경을 사람이 확인했음을 명시")
    ap.add_argument("--actor", default="현업(prototype)")
    ap.add_argument("--now", help="동결 타임스탬프 (ISO, 생략 시 현재시각)")
    args = ap.parse_args()

    docdir = G._find_doc(args.doc)
    if not docdir:
        print("문서 못 찾음:", args.doc); return 2
    items = json.loads((docdir / "phase3_output.json").read_text(encoding="utf-8")).get("items", [])
    cols_l, conds_l = _grounding(items)

    # 1) 컴파일
    print("【1】 자연어 → DSL 컴파일")
    print("  규칙:", args.rule)
    config, reasoning = compile_product_aggregate(args.rule, cols_l, conds_l)
    print("  컴파일 결과:", json.dumps(config, ensure_ascii=False))
    if reasoning:
        print("  근거:", reasoning)

    # 2) 적용안 = 기존에 merge (컴파일 누락 키 보존)
    cfg_all = json.loads(G.CONFIG_PATH.read_text(encoding="utf-8"))
    cur_pa = cfg_all.get(args.form, {}).get("product_aggregate", {})
    proposed_pa = {**cur_pa, **config}
    form_cfg_proposed = {**cfg_all.get(args.form, {}), "product_aggregate": proposed_pa}

    # 3) 게이트
    print("\n【2】 검증 게이트")
    allok, res, out = _run_gates(form_cfg_proposed, items, args.form, docdir.name)
    for name, ok, msg in res:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {msg}")

    # 4) 승인 요약
    print("\n【3】 승인 요약 (현업 확인용)")
    diff, has_review = _config_diff(cur_pa, proposed_pa)
    print("  설정 변경:")
    print("\n".join(diff) if diff else "    (값 변화 없음 — 동일 설정)")
    if has_review:
        print("  ※ ⚠ 표시 필드는 게이트가 검증하지 못함 — 동결 전 반드시 사람이 확인.")
    print("  표본 분해 결과:")
    for g in (out.get("groups", [])[:2] if out else []):
        rows = " / ".join(f"{r['qty']}@{r['amount']}" for r in g["rows"])
        print(f"    {g.get('jisho')} · {g.get('product_name')}: {rows} (합 {g.get('total_amount')})")

    # 5) 동결
    if not args.apply:
        print("\n→ 미리보기. 동결하려면 --apply (게이트 통과 시에만 허용)")
        return 0 if allok else 1
    if not allok:
        print("\n❌ 게이트 미통과 — 동결 거부")
        return 1
    if has_review and not args.confirm_display:
        print("\n❌ 게이트 비검증(⚠) 필드 변경 있음 — --confirm-display 로 사람 확인 명시해야 동결 가능")
        return 1

    ts = args.now or _dt.datetime.now().isoformat(timespec="seconds")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    safe_ts = ts.replace(":", "")
    backup = BACKUP_DIR / f"form_types.{safe_ts}.json"
    backup.write_text(G.CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    cfg_all[args.form]["product_aggregate"] = proposed_pa
    G.CONFIG_PATH.write_text(json.dumps(cfg_all, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 사후 재검증 (쓰기 과정에서 제어문자/스키마 오염 없는지)
    ok_post, m_post = G.gate_schema(args.form)
    if not ok_post:
        backup_text = backup.read_text(encoding="utf-8")
        G.CONFIG_PATH.write_text(backup_text, encoding="utf-8")
        print(f"\n❌ 사후 검증 실패({m_post}) — 백업으로 롤백함")
        return 1

    entry = {
        "ts": ts, "actor": args.actor, "form": args.form,
        "field": "product_aggregate", "rule": args.rule,
        "compiled": config, "frozen": proposed_pa,
        "gate": "passed", "display_confirmed": bool(has_review and args.confirm_display),
        "backup": str(backup.relative_to(ROOT)),
    }
    with CHANGELOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"\n✅ 동결 완료")
    print(f"   백업:   {backup.relative_to(ROOT)}")
    print(f"   변경이력: {CHANGELOG.relative_to(ROOT)} (+1)")
    print(f"   사후검증: {m_post}")
    print("   → 이제 해당 문서를 재분석하면 동결된 설정으로 계산됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
