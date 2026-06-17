"""
render_form_prose.py — [config] 블록 → 사람이 읽는 "실행 규칙" 문장 (결정적 렌더)

literate config 정본 통일의 핵심: 블록이 유일 정본이고, 사람이 읽는 NET·교차검증·
출력 설정 문장은 손으로 적지 않고 블록에서 **자동 출력**한다. 렌더는 결정적이라
블록과 100% 일치 — 산문↔블록 드리프트가 구조적으로 불가능해진다.

LLM 없음. 순수 변환(구조→문장)이라 안전하다(반대 방향, 즉 산문→구조 추론이 위험했던 것).

사용:
  python scripts/render_form_prose.py form_04        # 콘솔 출력
  render_rules(entry) -> markdown str                # 모듈로 호출
"""
import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).parent.parent
CONFIG = BASE / "config" / "form_types.json"

# 자동 생성 섹션 경계 마커 (이 사이는 렌더 결과로 통째 교체된다)
BEGIN = "<!-- BEGIN auto-rules (블록에서 생성 — 직접 수정 금지) -->"
END = "<!-- END auto-rules -->"

# show_sections 코드 → 한국어
_SECTION_LABEL = {
    "xv": "교차검증",
    "rate_summary": "세율 요약",
    "summary": "합계 요약",
}


def _expr_to_jp(net: dict) -> str:
    """expr의 변수 토큰을 실제 일본어 명칭으로 치환해 읽기 좋게."""
    expr = net.get("expr", "")
    subs = {"shikiri": "仕切"}
    if net.get("teiban_type"):
        subs["teiban"] = net["teiban_type"]
    for alias, col in (net.get("vars") or {}).items():
        if col:
            subs[alias] = str(col)
    for alias, cv in (net.get("computed_vars") or {}).items():
        subs.setdefault(alias, alias)  # computed는 이름 유지(아래 별도 설명)
    # 긴 이름부터 치환(부분일치 방지) + 단어경계
    for name in sorted(subs, key=len, reverse=True):
        expr = re.sub(rf"\b{re.escape(name)}\b", subs[name], expr)
    return expr.replace("-", "−")  # 보기 좋은 마이너스


def render_rules(entry: dict) -> str:
    """[config] entry → 사람이 읽는 실행 규칙 마크다운(자동 생성)."""
    lines: list[str] = [BEGIN, "", "## 실행 규칙 (블록에서 자동 생성 · 직접 편집 금지)", ""]

    # NET
    net = entry.get("net") or {}
    if net.get("formula_type") == "expr" and net.get("expr"):
        lines.append("**NET 계산**")
        lines.append("")
        lines.append(f"- NET = {_expr_to_jp(net)}")
        mapping = []
        for alias, col in (net.get("vars") or {}).items():
            mapping.append(f"`{alias}` = {col if col else '(없음)'}")
        if net.get("teiban_type"):
            mapping.append(f"`teiban` = {net['teiban_type']}(동일 得意先·商品 정번 행 조회)")
        for alias, cv in (net.get("computed_vars") or {}).items():
            if isinstance(cv, dict):
                desc = cv.get("expr", "")
                db = cv.get("divide_by")
                if isinstance(db, dict) and db.get("field"):
                    cond = ""
                    w = db.get("when")
                    if isinstance(w, dict) and w.get("field"):
                        cond = f" ({w['field']}={w.get('equals','')}일 때)"
                    desc = f"({desc}) ÷ {db['field']}{cond}"
                mapping.append(f"`{alias}` = {desc}")
        if mapping:
            lines.append(f"  - 변수: {', '.join(mapping)}")
        if net.get("no_net_kubun"):
            lines.append(f"  - NET 계산 없음: {', '.join(net['no_net_kubun'])}")
        lines.append("")
    elif net.get("formula_type") == "plugin":
        lines.append(f"**NET 계산**: plugin `{net.get('plugin')}`")
        lines.append("")

    # 교차검증
    cvs = entry.get("cross_validation") or []
    if cvs:
        lines.append("**교차검증**")
        lines.append("")
        for i, r in enumerate(cvs, 1):
            label = r.get("label", r.get("type", "?"))
            keys = []
            for k in ("cover_key", "cover_key_8", "cover_key_10", "cover_breakdown_key"):
                if r.get(k):
                    keys.append(r[k])
            if r.get("detail_group_field"):
                keys.append(f"그룹={r['detail_group_field']}")
            tail = f" [{', '.join(keys)}]" if keys else ""
            lines.append(f"- {label}{tail}")
        lines.append("")

    # 제품 집계
    pa = entry.get("product_aggregate") or {}
    if pa.get("strategy"):
        lines.append("**제품 집계**")
        lines.append("")
        lines.append(f"- 전략 `{pa['strategy']}` — 기준 {pa.get('base_condition','?')} "
                     f"(수량 {pa.get('qty_field','?')}, 금액 {pa.get('amount_field','?')})")
        lines.append("")

    # 출력
    secs = entry.get("show_sections") or []
    if secs:
        labels = [_SECTION_LABEL.get(s, s) for s in secs]
        lines.append(f"**출력 섹션**: {', '.join(labels)}")
        lines.append("")
    if entry.get("aggregate_label"):
        lines.append(f"**집계 라벨**: {entry['aggregate_label']}")
        lines.append("")

    lines.append(END)
    return "\n".join(lines).rstrip() + "\n"


# 마커 사이(auto-rules)를 통째로 잡는 정규식 — 멱등 갱신·제거용
_AUTO_RE = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\s*", re.DOTALL)
# 백엔드 등 다른 모듈이 동일 규칙으로 strip 할 수 있게 마커 프리픽스 패턴도 노출
AUTO_MARKER_RE = re.compile(r"<!-- BEGIN auto-rules.*?<!-- END auto-rules -->\s*", re.DOTALL)


def strip_auto_rules(md: str) -> str:
    """form_XX.md에서 자동 생성 규칙 섹션을 제거(산문만 남김)."""
    return AUTO_MARKER_RE.sub("", md)


def inject_before_config(md: str, entry: dict) -> str:
    """auto-rules 섹션을 [config] 블록 바로 앞에 삽입/갱신(멱등).

    기존 auto-rules는 먼저 제거하고 새로 렌더한 것을 넣는다 → 블록과 항상 일치.
    """
    rendered = render_rules(entry)
    md = strip_auto_rules(md)
    idx = md.find("## [config]")
    if idx == -1:
        return md.rstrip() + "\n\n" + rendered
    head = md[:idx].rstrip()
    tail = md[idx:]
    return head + "\n\n" + rendered + "\n\n" + tail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("form_id")
    args = ap.parse_args()
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    if args.form_id not in data:
        print(f"등록 안 됨: {args.form_id}", file=sys.stderr)
        return 1
    print(render_rules(data[args.form_id]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
