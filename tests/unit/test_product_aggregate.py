"""제품별 집계 이중조건 수량 분해 (build_product_aggregate) 골든 테스트.

현업 캡처(form_04, 辛ラーメントゥーンバカップ113g) 스펙을 그대로 박제:
  원본(조건별): 定番 264 + 定番 2,352 + 原価引き 204(44.44) + 導入 2,352(47.44)
  → 제품 단위 집계 후 定番 총량에서 차감 분해:
     導入 2,352 (定番+導入)  = 140,978.88
     原価引き 204 (定番+原価引き) = 11,615.76
     定番만 60 (= 2,616 − 2,352 − 204) = 750
     合計 2,616 / 153,344.64

핵심 불변식: ① 정번만 = 정번총 − Σ추가, ② 총수량 = 정번총, ③ 금액 보존(원본 총금액).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from scripts.phase4_calc import build_product_aggregate  # noqa: E402

_CFG = {"product_aggregate": {"base_condition": "定番条件",
                              "qty_field": "数量", "unit_field": "未収条件", "amount_field": "金額"}}


def _item(ctype, qty, unit, amount):
    return {
        "jisho": "R営業東北", "customer_ocr": "(株) ファミリーマート",
        "product_code": "181886694", "product": "辛ラーメントゥーンバカップ113g",
        "condition_type": ctype,
        "columns": {"数量": qty, "未収条件": unit, "金額": amount},
    }


def _capture_items():
    return [
        _item("定番条件", 264, 12.5, 3300),
        _item("定番条件", 2352, 12.5, 29400),
        _item("原価引き条件", 204, 44.44, 9065.76),
        _item("導入条件", 2352, 47.44, 111578.88),
    ]


def test_decomposition_quantities():
    agg = build_product_aggregate(_capture_items(), _CFG)
    assert len(agg["groups"]) == 1
    g = agg["groups"][0]
    qtys = sorted(r["qty"] for r in g["rows"])
    assert qtys == [60, 204, 2352], f"분해 수량 불일치: {qtys}"
    assert g["total_qty"] == 2616  # = 정번 총량 (264+2352), 추가조건은 부분집합이라 안 더함


def test_base_only_group():
    """定番만 그룹 = 정번총 − Σ추가, 금액 = 수량 × 정번단가."""
    agg = build_product_aggregate(_capture_items(), _CFG)
    g = agg["groups"][0]
    base_only = [r for r in g["rows"] if list(r["units"].keys()) == ["定番条件"]]
    assert len(base_only) == 1
    assert base_only[0]["qty"] == 60       # 2616 − 2352 − 204
    assert base_only[0]["amount"] == 750.0  # 60 × 12.5


def test_dual_condition_amounts():
    """이중조건 그룹 금액 = 정번분 비율배분 + 추가조건 원본금액."""
    agg = build_product_aggregate(_capture_items(), _CFG)
    rows = {r["qty"]: r for r in agg["groups"][0]["rows"]}
    # 導入 2352: 정번분 32700*2352/2616=29400 + 도입 111578.88 = 140978.88
    assert abs(rows[2352]["amount"] - 140978.88) < 0.05
    assert rows[2352]["units"]["導入条件"] == 47.44
    # 原価引き 204: 32700*204/2616=2550 + 9065.76 = 11615.76
    assert abs(rows[204]["amount"] - 11615.76) < 0.05
    assert rows[204]["units"]["原価引き条件"] == 44.44


def test_amount_preservation():
    """분해 총금액 = 원본 제품 총금액 (보존)."""
    items = _capture_items()
    orig = sum(i["columns"]["金額"] for i in items)
    agg = build_product_aggregate(items, _CFG)
    assert abs(agg["groups"][0]["total_amount"] - orig) < 0.05


def test_dynamic_condition_columns():
    """등장 조건이 컬럼으로, 定番이 맨 앞."""
    agg = build_product_aggregate(_capture_items(), _CFG)
    assert agg["condition_columns"][0] == "定番条件"
    assert set(agg["condition_columns"]) == {"定番条件", "原価引き条件", "導入条件"}


def test_disabled_without_config():
    """product_aggregate 설정 없으면 None (이 양식은 기존 방식 유지)."""
    assert build_product_aggregate(_capture_items(), {}) is None


def test_overflow_warning():
    """추가조건 합이 定番 초과(데이터 오류) → 경고, 음수 그룹 안 만듦."""
    items = [
        _item("定番条件", 100, 12.5, 1250),
        _item("原価引き条件", 150, 44.44, 6666),  # 150 > 100
    ]
    agg = build_product_aggregate(items, _CFG)
    assert agg["warnings"], "초과 케이스에 경고가 없음"
    # 음수 定番만 그룹은 생성되지 않음
    base_only = [r for r in agg["groups"][0]["rows"] if list(r["units"].keys()) == ["定番条件"]]
    assert not base_only


# ── 파라미터화: relationship(독립) · group_by ────────────────────────────────
_CFG_INDEP = {"product_aggregate": {"relationship": "independent", "base_condition": "定番条件",
                                    "qty_field": "数量", "amount_field": "金額"}}


def test_relationship_independent_no_subtraction():
    """independent — 차감 없이 각 조건이 제 수량·금액 그대로 한 행씩."""
    agg = build_product_aggregate(_capture_items(), _CFG_INDEP)
    g = agg["groups"][0]
    qtys = sorted(r["qty"] for r in g["rows"])
    # 定番 264+2352=2616 합쳐짐, 原価引き 204, 導入 2352 — 차감 없음
    assert qtys == [204, 2352, 2616]
    assert g["total_qty"] == 2616 + 204 + 2352  # 독립이므로 전부 합산


def test_relationship_independent_preserves_amount():
    """independent도 금액 보존(공통 불변식)."""
    items = _capture_items()
    orig = sum(i["columns"]["金額"] for i in items)
    agg = build_product_aggregate(items, _CFG_INDEP)
    assert abs(agg["groups"][0]["total_amount"] - orig) < 0.05


def test_relationship_subset_is_default():
    """relationship 미지정 = subset (기존 동작과 동일)."""
    a = build_product_aggregate(_capture_items(), _CFG)
    b = build_product_aggregate(_capture_items(), {"product_aggregate": {
        "relationship": "subset", "base_condition": "定番条件",
        "qty_field": "数量", "unit_field": "未収条件", "amount_field": "金額"}})
    assert a["groups"][0]["total_qty"] == b["groups"][0]["total_qty"] == 2616


def test_unknown_relationship_raises():
    import pytest
    with pytest.raises(ValueError, match="relationship"):
        build_product_aggregate(_capture_items(), {"product_aggregate": {
            "relationship": "made_up", "base_condition": "定番条件"}})


def test_group_by_splits_groups():
    """group_by에 jisho 빼면 다른 지점이 같은 그룹으로 안 묶이는지 — customer/product만으로 묶기."""
    items = [
        _item("定番条件", 100, 12.5, 1250),
        {**_item("定番条件", 50, 12.5, 625), "jisho": "R営業西"},  # 다른 지점, 같은 제품/거래처
    ]
    # 기본(jisho 포함) → 2그룹
    a = build_product_aggregate(items, _CFG)
    assert len(a["groups"]) == 2
    # group_by에서 jisho 제외 → 1그룹으로 병합
    cfg = {"product_aggregate": {"group_by": ["customer", "product"],
                                 "base_condition": "定番条件", "qty_field": "数量",
                                 "unit_field": "未収条件", "amount_field": "金額"}}
    b = build_product_aggregate(items, cfg)
    assert len(b["groups"]) == 1
    assert b["groups"][0]["total_qty"] == 150
