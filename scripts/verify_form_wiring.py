"""
verify_form_wiring.py — 양식 "뒷단 연결" 검증 + 안전등급 자동수정

동기화 버튼 직후 "MD만 생기고 엔진엔 안 붙은" gap이 없는지 결정적으로 점검한다.
각 gap을 3갈래로 라우팅한다 (docs/literate-config-migration.md 후속 설계):

  🔧 safe  — 파생물 재생성. 정본 불가침·결정적·검증가능 → --fix로 자동 적용.
  👤 owner — 현업이 MD에 적고 동기화하면 반영될 규칙(TBD·식별패턴 등). 지어내지 않고 안내.
  🛠 dev   — 엔진에 없는 어휘(미등록 교차검증 type·미지원 연산·미등록 전략). T3 개발 필요.

엔진 어휘는 하드코딩하지 않고 **엔진 소스에서 직접 추출**한다(드리프트 방지·단일 출처).

사용:
  python scripts/verify_form_wiring.py                # 등록된(블록 보유) 전 양식 점검
  python scripts/verify_form_wiring.py form_03         # 특정 양식
  python scripts/verify_form_wiring.py form_03 --fix    # safe 등급 자동수정까지
"""
import argparse
import ast
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from scripts.build_form_types import build_forms, serialize, extract_config_block  # noqa: E402
from scripts.aggregate_strategies import AGGREGATE_STRATEGIES  # noqa: E402
from scripts.phase4_calc import _SAFE_OPS, _SAFE_CMP, _SAFE_BOOLOPS, _RELATIONSHIP_STRATEGY  # noqa: E402  (엔진 허용 연산·집계 매핑 — 단일 출처)

# 엔진이 지원하는 op 노드 타입 전체 (산술 + 비교 + 논리 and/or/not). _net_names_and_ops·게이트가 참조.
_ENGINE_OP_TYPES = set(_SAFE_OPS) | set(_SAFE_CMP) | set(_SAFE_BOOLOPS) | {ast.Not}

FORM_DEFS = BASE / "form_definitions"
CONFIG = BASE / "config" / "form_types.json"
SCHEMA = BASE / "config" / "form_types.schema.json"
INDEX = FORM_DEFS / "_index.md"
PHASE4_SRC = BASE / "scripts" / "phase4_calc.py"

GRADE_ICON = {"ok": "✅", "safe": "🔧", "owner": "👤", "dev": "🛠", "info": "ℹ️"}


# ── 엔진 어휘 (소스에서 추출) ─────────────────────────────────────────────────
def engine_cross_validation_types() -> set[str]:
    """phase4_calc.py의 `rtype == "..."` 분기에서 등록된 교차검증 종류를 추출."""
    src = PHASE4_SRC.read_text(encoding="utf-8")
    return set(re.findall(r'rtype == "([^"]+)"', src))


def engine_dsl_names() -> set[str]:
    """DSL expr에서 vars·computed_vars 외에 항상 허용되는 내장 이름."""
    return {"shikiri", "teiban"}


def engine_op_allowed(op_node) -> bool:
    return type(op_node) in _ENGINE_OP_TYPES


# ── Finding ──────────────────────────────────────────────────────────────────
class Finding:
    def __init__(self, gate, grade, message, fixer=None):
        self.gate = gate
        self.grade = grade          # ok | safe | owner | dev | info
        self.message = message
        self.fixer = fixer          # safe 등급일 때만: () -> str

    def __repr__(self):
        return f"{GRADE_ICON.get(self.grade,'?')} [{self.gate}] {self.message}"


# ── 안전 등급 수정자 ──────────────────────────────────────────────────────────
def _fix_rebuild() -> str:
    CONFIG.write_text(serialize(build_forms()), encoding="utf-8")
    return "config/form_types.json을 블록에서 재빌드(정렬·드리프트 정정)"


def _fix_index(form_id: str):
    def _do() -> str:
        text = INDEX.read_text(encoding="utf-8")
        link = f"[{form_id}.md]({form_id}.md)"
        new = re.sub(rf"(\|\s*{form_id}\s*\|)\s*\(미등록\)\s*\|", rf"\1 {link} |", text)
        INDEX.write_text(new, encoding="utf-8")
        return f"_index.md에 {form_id} 등록행 채움"
    return _do


# ── 게이트 ────────────────────────────────────────────────────────────────────
def _net_names_and_ops(net: dict):
    """net.expr + computed_vars의 모든 expr에서 (이름 집합, op노드 리스트) 수집."""
    exprs = []
    if net.get("expr"):
        exprs.append(net["expr"])
    for cv in (net.get("computed_vars") or {}).values():
        if isinstance(cv, dict) and cv.get("expr"):
            exprs.append(cv["expr"])
    names, ops = set(), []
    for e in exprs:
        try:
            tree = ast.parse(e, mode="eval")
        except SyntaxError:
            names.add(f"<구문오류:{e}>")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, (ast.BinOp, ast.UnaryOp, ast.BoolOp)):
                ops.append(node.op)
            elif isinstance(node, ast.Compare):
                ops.extend(node.ops)
    return names, ops


def verify(form_id: str, json_data: dict, cv_types: set[str]) -> list[Finding]:
    out: list[Finding] = []
    md_path = FORM_DEFS / f"{form_id}.md"
    md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    entry = json_data.get(form_id)
    block = extract_config_block(md, f"{form_id}.md") if md else None

    # 1) 블록 존재 / json 정합
    if block is None:
        if entry is not None:
            out.append(Finding("block", "owner",
                "json엔 등록됐으나 [config] 블록 없음 — UI 동기화로 블록 생성 필요(현업)"))
        else:
            out.append(Finding("block", "info", "미등록 초안(블록·json 둘 다 없음) — 점검 생략"))
        return out
    if entry is None:
        out.append(Finding("block", "safe",
            "블록은 있으나 json 미반영 — 재빌드 필요", _fix_rebuild))
    elif block != entry:
        out.append(Finding("block", "safe",
            "블록 ↔ json 불일치(블록이 정본) — json 재빌드로 정정", _fix_rebuild))
    else:
        out.append(Finding("block", "ok", "블록 ↔ json 일치"))

    cfg = entry or block

    # 2) JSON Schema
    try:
        from jsonschema import Draft7Validator
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        errs = [e for e in Draft7Validator(schema).iter_errors({form_id: cfg})]
        if errs:
            msg = "; ".join(f"{'/'.join(map(str,e.path))}: {e.message}" for e in errs[:3])
            out.append(Finding("schema", "owner", f"스키마 위반(설정값 점검) — {msg}"))
        else:
            out.append(Finding("schema", "ok", "스키마 유효"))
    except ImportError:
        out.append(Finding("schema", "info", "jsonschema 미설치 — 스키마 검사 생략"))

    # 3) NET expr 어휘 (엔진 DSL과 대조)
    net = cfg.get("net") or {}
    if net.get("formula_type") == "expr":
        allowed = engine_dsl_names() | set(net.get("vars") or {}) | set(net.get("computed_vars") or {})
        names, ops = _net_names_and_ops(net)
        bad_ops = [o for o in ops if not engine_op_allowed(o)]
        unknown = {n for n in names if n not in allowed and not n.startswith("<")}
        syn = {n for n in names if n.startswith("<")}
        if syn:
            out.append(Finding("net_expr", "owner", f"수식 구문오류 {syn} — MD 수식 점검(현업)"))
        if bad_ops:
            kinds = sorted({type(o).__name__ for o in bad_ops})
            out.append(Finding("net_expr", "dev",
                f"엔진 미지원 연산 {kinds} — DSL 어휘 추가 필요(개발자 T3)"))
        if unknown:
            out.append(Finding("net_expr", "owner",
                f"정의 안 된 변수 {sorted(unknown)} — vars에 컬럼 매핑 추가 필요(현업) 또는 오타"))
        if not (syn or bad_ops or unknown):
            out.append(Finding("net_expr", "ok", f"NET 수식 어휘 유효 ({net.get('expr')})"))

    # 4) 교차검증 type 엔진 등록
    for rule in cfg.get("cross_validation") or []:
        t = rule.get("type")
        if t not in cv_types:
            out.append(Finding("cross_validation", "dev",
                f"교차검증 type '{t}' 엔진 미등록 — 엔진에 검증 종류 추가 필요(개발자 T3)"))
    if cfg.get("cross_validation") and all(r.get("type") in cv_types for r in cfg["cross_validation"]):
        out.append(Finding("cross_validation", "ok",
            f"교차검증 type 전부 등록됨 ({[r['type'] for r in cfg['cross_validation']]})"))

    # 5) 집계 전략 레지스트리 — strategy(명시) 또는 relationship(선언적)로 결정된 실효 전략을 검사
    pa = cfg.get("product_aggregate") or {}
    if pa.get("strategy") or pa.get("relationship"):
        eff = pa.get("strategy") or _RELATIONSHIP_STRATEGY.get(pa.get("relationship", "subset"))
        if eff is None:
            out.append(Finding("aggregate", "owner",
                f"relationship '{pa.get('relationship')}' 미지원 — {sorted(_RELATIONSHIP_STRATEGY)} 중 선택(현업)"))
        elif eff in AGGREGATE_STRATEGIES:
            out.append(Finding("aggregate", "ok", f"집계 전략 '{eff}' 등록됨"))
        else:
            out.append(Finding("aggregate", "dev",
                f"집계 전략 '{eff}' 미등록 — aggregate_strategies에 추가 필요(개발자 T3)"))

    # 6) 식별 패턴 (양식 인식 연결)
    m = re.search(r"##\s*식별\s*패턴\s*\n+(.+)", md)
    pats = re.findall(r"`([^`]+)`", m.group(1)) if m else []
    if not pats:
        out.append(Finding("identify", "owner",
            "## 식별 패턴 키워드 없음 — 문서 식별 불가, 현업이 식별 키워드 기재 필요"))
    else:
        out.append(Finding("identify", "ok", f"식별 패턴 {len(pats)}개 ({pats})"))

    # 7) _index.md 등록
    idx = INDEX.read_text(encoding="utf-8") if INDEX.exists() else ""
    if re.search(rf"\|\s*{form_id}\s*\|\s*\(미등록\)\s*\|", idx):
        out.append(Finding("index", "safe",
            "_index.md 미등록 표기 — 등록행 채움(기계적)", _fix_index(form_id)))
    elif re.search(rf"\|\s*{form_id}\s*\|", idx):
        out.append(Finding("index", "ok", "_index.md 등록됨"))
    else:
        out.append(Finding("index", "owner", "_index.md에 행 자체 없음 — 추가 필요"))

    # 8) 남은 TBD (현업 입력 대기)
    n_tbd = len(re.findall(r"\bTBD\b", md))
    if n_tbd:
        out.append(Finding("tbd", "owner",
            f"미확정 규칙 {n_tbd}개(TBD) — 현업이 MD에 채우고 동기화하면 반영(엔진 어휘 내 한)"))

    return out


def check_global() -> Finding:
    """전 양식 build ↔ json 정합(순서·내용). 실패 시 재빌드(safe)."""
    built = serialize(build_forms())
    cur = CONFIG.read_text(encoding="utf-8") if CONFIG.exists() else ""
    if built == cur:
        return Finding("build_check", "ok", "build ↔ form_types.json 동치(전 양식)")
    return Finding("build_check", "safe",
        "build ↔ form_types.json 불일치(순서/드리프트) — 재빌드로 정정", _fix_rebuild)


def registered_forms() -> list[str]:
    """블록을 가진(=등록된) 양식 id 목록."""
    ids = []
    for p in sorted(FORM_DEFS.glob("form_[0-9]*.md")):
        if extract_config_block(p.read_text(encoding="utf-8"), p.name) is not None:
            ids.append(p.stem)
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("forms", nargs="*", help="점검할 form_id (생략 시 등록된 전 양식)")
    ap.add_argument("--fix", action="store_true", help="safe 등급 자동수정 적용")
    args = ap.parse_args()

    json_data = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    cv_types = engine_cross_validation_types()
    targets = args.forms or registered_forms()

    findings: list[Finding] = [check_global()]
    for fid in targets:
        findings += verify(fid, json_data, cv_types)

    # 출력
    print(f"\n=== verify_form_wiring — {', '.join(targets) or '(없음)'} ===\n")
    for f in findings:
        print(f" {f}")

    safe = [f for f in findings if f.grade == "safe"]
    owner = [f for f in findings if f.grade == "owner"]
    dev = [f for f in findings if f.grade == "dev"]

    if args.fix and safe:
        print("\n--- 🔧 safe 등급 자동수정 적용 ---")
        applied = set()
        for f in safe:
            if f.fixer and f.fixer not in applied:
                msg = f.fixer()
                applied.add(f.fixer)
                print(f"   ✓ {msg}")
        # 재검증으로 닫혔는지 확인
        print("   (재빌드 후 build 동치:", check_global().grade == "ok", ")")

    print(f"\n요약: 🔧safe {len(safe)}  👤owner {len(owner)}  🛠dev {len(dev)}")
    if owner:
        print("  👤 현업 할 일:", "; ".join(f.message for f in owner))
    if dev:
        print("  🛠 개발자(T3):", "; ".join(f.message for f in dev))
    if safe and not args.fix:
        print("  🔧 --fix 로 자동수정 가능")

    # 종료코드: dev/owner 있으면 1(주의), safe만이면 0(또는 --fix로 닫힘)
    return 1 if (owner or dev) else 0


if __name__ == "__main__":
    sys.exit(main())
