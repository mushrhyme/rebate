"""판매처(dist) 조건부 override (S6 조건부 override 모양) — 완전 결정적.

1:N 판매처 후보가 모호할 때, 양식이 선언한 조건부 규칙으로 후보를 **결정적으로**
고른다. 기존엔 이 케이스를 LLM(1:N Claude 호출)이 판단했다 — 즉 비결정적이었다.
규칙이 매칭되면 LLM을 건너뛰고 코드가 후보를 확정한다(재현성↑).

규칙은 form_types.json의 `dist_overrides`에 선언한다(scripts 아님, 현업이 다루는 config):

    "dist_overrides": [
      { "when": { "jisho": "CVS営業部" },
        "pick_candidate_name_contains": "広域リテール" },
      { "when": { "retailer_code": "R001" },
        "dist_code": "D001" }
    ]

- `when`:  item 필드 → 값의 **정확 일치**(여러 개면 AND). 술어는 정확 일치만 — 임의
           코드/표현식 실행 없음(결정적·감사가능).
- 액션(둘 중 하나):
    · `pick_candidate_name_contains`: 후보 dist_name에 이 문자열을 포함하는 **유일한**
      후보를 선택. (form_04 "jisho=CVS営業部 → 広域リテール 판매처" 규칙이 이 형태)
    · `dist_code`: 후보 중 그 코드를 가진 **유일한** 후보를 선택.

**안전 규칙(폴백 우선):** 매칭 실패 또는 모호(0개·2개+ 일치)면 None을 돌려준다.
그러면 호출자는 기존 경로(LLM 1:N 또는 pending)로 폴백한다 — override는 *확실할 때만*
개입하고, 애매하면 절대 추측하지 않는다. 그래서 최악의 경우 = 기존 동작, 좋은 경우 =
결정적 확정.

`dist_overrides` 미선언 양식은 rules=[] → 항상 None → 동작 변화 없음(기본 OFF).

설계: docs/registry-driven-primitives.md (축 C — 조건부 override)
"""
from typing import Optional


def resolve_dist_override(
    candidates: list[dict],
    item_fields: dict,
    rules: Optional[list[dict]],
) -> Optional[dict]:
    """규칙을 순서대로 평가해 후보를 결정적으로 고른다.

    Args:
        candidates:  [{"dist_code": str, "dist_name": str}, ...] (2건 이상인 1:N 케이스)
        item_fields: 술어 평가용 item 필드 값 (예: {"jisho": ..., "retailer_code": ...})
        rules:       form_types.json dist_overrides (없으면 None/[])

    Returns:
        {"dist_code", "dist_name", "rule"} — 결정적으로 확정된 경우
        None — 매칭 없음 또는 모호 → 호출자가 기존 경로로 폴백
    """
    if not rules:
        return None

    for rule in rules:
        when = rule.get("when") or {}
        # 술어: 모든 필드가 정확히 일치해야 함(AND). 값은 문자열 비교로 정규화.
        if not all(str(item_fields.get(k, "")) == str(v) for k, v in when.items()):
            continue

        picked = _apply_action(candidates, rule)
        if picked is not None:
            return {**picked, "rule": rule}
        # 액션이 모호/실패 → 다음 규칙으로(폴백). 침묵 추측 금지.

    return None


def _apply_action(candidates: list[dict], rule: dict) -> Optional[dict]:
    """규칙의 액션을 적용해 유일 후보를 고른다. 0개·2개+면 None(안전 폴백)."""
    code = rule.get("dist_code")
    if code is not None:
        matches = [c for c in candidates if c.get("dist_code") == code]
        return _unique(matches)

    needle = rule.get("pick_candidate_name_contains")
    if needle:
        matches = [c for c in candidates if needle in (c.get("dist_name") or "")]
        return _unique(matches)

    return None


def _unique(matches: list[dict]) -> Optional[dict]:
    """유일 후보면 {dist_code, dist_name} 반환, 아니면 None."""
    if len(matches) != 1:
        return None
    c = matches[0]
    return {"dist_code": c.get("dist_code", ""), "dist_name": c.get("dist_name", "")}
