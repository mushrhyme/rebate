"""자연어 → DSL 컴파일 PoC (P2).

현업의 자연어 규칙을 Claude가 form_types.json의 product_aggregate 설정으로
컴파일하고, 곧바로 P1 검증 게이트(scripts/validate_dsl_gate.py)에 통과시킨다.
"LLM은 산식을 작성하되 실행하지 않는다" — 컴파일 결과는 검증 게이트를 거쳐야
하며, 런타임 계산은 기존 결정적 코드(build_product_aggregate)가 한다.

흐름:
  자연어 규칙 + (샘플 문서의 실제 컬럼·조건타입 grounding)
    → Claude(tool_use 강제 구조화 출력)
    → product_aggregate 설정(JSON)
    → P1 게이트(dry-run + 필드 실재성 + 불변식)로 검증

사용:
  python scripts/compile_dsl_poc.py --doc "ＣＶＳ①"
  python scripts/compile_dsl_poc.py --doc "ＣＶＳ①" --rule "<자연어 규칙>"
"""
from __future__ import annotations
import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.validate_dsl_gate as G  # noqa: E402

MODEL = "claude-sonnet-4-6"  # 백엔드 phase2와 동일

DEFAULT_RULE = (
    "이 양식은 제품 단위로, 定番 물량에서 原価引き·導入 같은 추가조건 물량을 "
    "빼서 이중계산 없이 '실제 판매물량'으로 분해해 보여줘. 기준이 되는 조건은 "
    "定番条件이고, 수량과 금액은 표의 해당 컬럼을 쓰면 돼."
)

EMIT_TOOL = {
    "name": "emit_product_aggregate",
    "description": "제품별 이중조건 수량 분해(product_aggregate) 설정을 출력한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "base_condition": {"type": "string", "description": "차감 기준이 되는 조건타입 (분해의 베이스)"},
            "qty_field":      {"type": "string", "description": "수량 컬럼명 (items columns의 키)"},
            "unit_field":     {"type": "string", "description": "단가 컬럼명 (items columns의 키)"},
            "amount_field":   {"type": "string", "description": "금액 컬럼명 (items columns의 키)"},
            "reasoning":      {"type": "string", "description": "필드를 그렇게 고른 근거 (한 줄)"},
        },
        "required": ["base_condition", "qty_field", "amount_field"],
    },
}

SYSTEM = (
    "너는 현업의 자연어 규칙을 회계 분석 파이프라인의 결정적 DSL 설정으로 "
    "번역하는 컴파일러다. 절대 숫자를 계산하지 않는다 — 오직 설정(필드 매핑)만 만든다.\n"
    "product_aggregate 설정은 제품 단위로 base_condition의 총수량에서 다른 조건들의 "
    "수량을 빼 '실제 물량 그룹'으로 분해하는 데 쓰인다.\n"
    "규칙: base_condition/qty_field/unit_field/amount_field 는 반드시 제공된 "
    "'사용 가능 컬럼'과 '조건타입' 목록에 실제로 존재하는 값만 쓴다. 추측 금지."
)


def _load_key() -> str:
    for line in (ROOT / "backend" / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("ANTHROPIC_API_KEY 없음")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", required=True)
    ap.add_argument("--rule", default=DEFAULT_RULE)
    ap.add_argument("--form", default="form_04")
    args = ap.parse_args()

    docdir = G._find_doc(args.doc)
    if not docdir:
        print("문서 못 찾음:", args.doc); return 2
    items = json.loads((docdir / "phase3_output.json").read_text(encoding="utf-8")).get("items", [])

    # grounding: 실제 컬럼·조건타입
    cols: set[str] = set()
    conds: set[str] = set()
    for it in items:
        cols |= set(it.get("columns", {}).keys())
        if it.get("condition_type"):
            conds.add(it["condition_type"])
    cols_l = sorted(cols)
    conds_l = sorted(conds)

    print("=" * 72)
    print("입력 자연어 규칙:")
    print(" ", args.rule)
    print(f"\ngrounding — 조건타입: {conds_l}")
    print(f"grounding — 컬럼: {cols_l}")
    print("=" * 72)

    import anthropic
    client = anthropic.Anthropic(api_key=_load_key())
    user = (
        f"규칙:\n{args.rule}\n\n"
        f"사용 가능 컬럼(columns 키): {cols_l}\n"
        f"등장 조건타입: {conds_l}\n\n"
        "위 규칙을 product_aggregate 설정으로 컴파일해 emit_product_aggregate 도구로 출력하라."
    )
    resp = client.messages.create(
        model=MODEL, max_tokens=1024, system=SYSTEM,
        tools=[EMIT_TOOL], tool_choice={"type": "tool", "name": "emit_product_aggregate"},
        messages=[{"role": "user", "content": user}],
    )
    config = next((b.input for b in resp.content if b.type == "tool_use"), None)
    if not config:
        print("컴파일 실패 — tool_use 출력 없음"); return 1
    reasoning = config.pop("reasoning", "")

    print("\n[컴파일 결과] product_aggregate 설정:")
    print(json.dumps(config, ensure_ascii=False, indent=2))
    if reasoning:
        print("근거:", reasoning)

    # P1 게이트로 검증
    cfg = json.loads(G.CONFIG_PATH.read_text(encoding="utf-8"))
    form_cfg = copy.deepcopy(cfg.get(args.form, {}))
    form_cfg["product_aggregate"] = config

    print("\n" + "─" * 72)
    print("검증 게이트:")
    ok2, m2, out = G.gate_dryrun(form_cfg, items)
    print(f"  [{'PASS' if ok2 else 'FAIL'}] dry-run+필드: {m2}")
    allok = ok2
    if ok2:
        ok3, m3 = G.gate_invariants(form_cfg, items, out)
        print(f"  [{'PASS' if ok3 else 'FAIL'}] 불변식: {m3}")
        allok = allok and ok3
    print("─" * 72)
    if allok:
        print("✅ 컴파일된 설정이 게이트 통과 — 현업 승인 단계로 진행 가능")
        # 참고: 정답(현행 운영 설정)과 일치하는지
        gold = cfg.get(args.form, {}).get("product_aggregate")
        if gold:
            same = all(config.get(k) == gold.get(k) for k in ("base_condition", "qty_field", "amount_field"))
            print(f"   (참고) 현행 운영 설정과 핵심필드 {'일치' if same else '상이'}: 운영={gold}")
    else:
        print("❌ 게이트 차단 — 컴파일 재시도 필요 (자연어 명확화 또는 재컴파일)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
