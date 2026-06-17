"""제품별 집계 — 분해 전략 레지스트리.

phase4_calc.build_product_aggregate가 그룹핑·표시 스펙 생성을 맡고, **그룹별 행 분해**는
여기 등록된 전략 함수에 위임한다. config `product_aggregate.strategy`로 전략을 선택한다.

새 분해 알고리즘 = `@register("이름")` 함수 1회 추가 + 골든 테스트. 그 뒤 같은 분해를 쓰는
새 양식은 config 한 줄(`"strategy": "이름"`)로 끝난다(코드 0).

원칙(architecture.md §3): 임의 코드 실행 금지 — 전략은 등록된 화이트리스트로만 확장한다.
설계: docs/registry-driven-primitives.md
"""

# name -> strategy fn.  contract: (conds, base_type) -> (rows | None, warning | None)
#   conds:      {condition_type: {"qty": float, "amount": float}}  (그룹 1개분, 이미 수치 코어션됨)
#   base_type:  기준 조건명(예: "定番条件")
#   rows:       [{"qty", "units": {조건:단가}, "amount"}]  — None이면 이 그룹은 분해 대상 아님(skip)
#   warning:    데이터 이상 경고 문자열 또는 None (제품명은 호출자가 prefix로 붙임)
#   불변식: Σrows.qty = 기준 총수량, Σrows.amount = 그룹 원본 총금액 (±0.01)
AGGREGATE_STRATEGIES: dict = {}


def register(name):
    def deco(fn):
        AGGREGATE_STRATEGIES[name] = fn
        return fn
    return deco


def get_strategy(name):
    fn = AGGREGATE_STRATEGIES.get(name)
    if fn is None:
        raise ValueError(
            f"알 수 없는 집계 전략: {name!r}. "
            f"등록된 전략: {sorted(AGGREGATE_STRATEGIES)}. "
            f"새 전략은 scripts/aggregate_strategies.py에 @register로 추가하세요."
        )
    return fn


def _num_out(v: float):
    """정수면 int, 아니면 소수 2자리."""
    return int(v) if abs(v - round(v)) < 1e-9 else round(v, 2)


@register("subset_subtract")
def subset_subtract(conds: dict, base_type: str):
    """추가조건(原価引き·導入 등)을 기준조건(定番)의 부분집합으로 보고 수량을 분해한다.

    - 기준 총수량에서 각 추가조건 수량을 빼 '기준만' 그룹을 만든다.
    - 추가조건 그룹 금액 = 기준 총금액을 수량 비율로 배분 + 추가조건 원본 금액.
    - '기준만' 그룹 금액 = 기준 총금액의 잔여 비율.
    → 분해 총금액 = 기준 원본 총금액 (보존).

    기준조건 없거나 기준 수량≤0이면 (None, None) — 이 그룹은 분해 대상 아님.
    추가조건 합이 기준 초과(데이터 오류)면 '기준만' 그룹을 만들지 않고 warning을 돌려준다.
    """
    base = conds.get(base_type)
    if not base or base["qty"] <= 0:
        return None, None

    base_qty = base["qty"]
    base_amount = base["amount"]                       # 기준 원본 총금액 (정확)
    base_unit_disp = round(base_amount / base_qty, 2) if base_qty else 0.0
    extras = [(ct, v) for ct, v in conds.items() if ct != base_type]
    extra_qty_sum = sum(v["qty"] for _, v in extras)
    base_only_qty = base_qty - extra_qty_sum

    rows = []
    warning = None
    for ct, v in extras:
        q = v["qty"]
        # 금액 = 그 물량의 기준분(기준 총금액을 수량 비율로 배분) + 추가조건 원본 금액
        base_share = base_amount * (q / base_qty) if base_qty else 0.0
        rows.append({
            "qty": _num_out(q),
            "units": {base_type: base_unit_disp, ct: round((v["amount"] / q) if q else 0.0, 2)},
            "amount": round(base_share + v["amount"], 2),
        })
    if base_only_qty > 0.0001:
        rows.append({
            "qty": _num_out(base_only_qty),
            "units": {base_type: base_unit_disp},
            "amount": round(base_amount * (base_only_qty / base_qty), 2),
        })
    elif base_only_qty < -0.0001:
        warning = f"추가조건 합({extra_qty_sum})이 {base_type}({base_qty}) 초과 — 분해 음수"

    return rows, warning
