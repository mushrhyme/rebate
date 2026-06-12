"""test_formula_dsl_mock_forms.py — DSL 자동화 경로 Mock Form 테스트

현업 입력(form_02·03·05) 없이도 DSL 평가 경로를 검증한다.
임시 form_types config를 monkeypatch로 주입해 운영 config를 오염시키지 않는다.

검증 케이스:
  1. 단순 산식: shikiri - (c1 + c2)
  2. 조건부 divisor: CS면 case_qty로 나눔
  3. teiban 필요 산식: shikiri - teiban - self
  4. divisor=0 → zero_policy에 따른 안전 처리
  5. unknown variable → 명확한 오류 (변수명 + 사용 가능 목록)
  6. plugin formula_type인데 미등록 → 명확한 오류 (plugin 이름 + 등록 방법)

실행: pytest tests/unit/test_formula_dsl_mock_forms.py -v
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from scripts.phase4_calc import (
    _eval_expr,
    _safe_eval,
    calc_net,
)


# ── 공통 헬퍼 ─────────────────────────────────────────────────────────────────

def _make_net_cfg(**kwargs) -> dict:
    """최소 net config 딕셔너리 생성."""
    return {"formula_type": "expr", **kwargs}


def _run_calc_net(mock_form_id: str, mock_net_cfg: dict, cols: dict,
                  shikiri: float, teiban: float = 0.0) -> float | None:
    """calc_net을 monkeypatch 없이 직접 호출하는 헬퍼.

    운영 FORM_TYPES를 건드리지 않고 _eval_expr을 직접 호출한다.
    """
    ft = mock_net_cfg.get("formula_type")
    if ft == "expr":
        return _eval_expr(mock_net_cfg, cols, shikiri, teiban, _form_id=mock_form_id)
    raise ValueError(f"테스트 헬퍼: formula_type={ft!r} 미지원")


# ── 케이스 1: 단순 산식 shikiri - (c1 + c2) ──────────────────────────────────

class TestSimpleSubtract:
    _NET = _make_net_cfg(
        expr="shikiri - (c1 + c2)",
        vars={"c1": "条件1", "c2": "条件2"},
    )

    def test_basic_subtract(self):
        """shikiri=1000, c1=100, c2=50 → 850."""
        cols = {"条件1": 100, "条件2": 50}
        result = _run_calc_net("mock_form_A", self._NET, cols, shikiri=1000)
        assert result == pytest.approx(850.0)

    def test_c2_null_treated_as_zero(self):
        """c2 필드가 null(None)이면 0으로 처리."""
        net = _make_net_cfg(
            expr="shikiri - (c1 + c2)",
            vars={"c1": "条件1", "c2": None},
        )
        cols = {"条件1": 200}
        result = _run_calc_net("mock_form_A", net, cols, shikiri=500)
        assert result == pytest.approx(300.0)

    def test_missing_column_defaults_to_zero(self):
        """cols에 없는 필드는 0으로 처리 (KeyError 아님)."""
        cols = {}  # 빈 dict
        result = _run_calc_net("mock_form_A", self._NET, cols, shikiri=1000)
        assert result == pytest.approx(1000.0)


# ── 케이스 2: 조건부 divisor (CS 행) ─────────────────────────────────────────

class TestConditionalDivisor:
    _NET = _make_net_cfg(
        expr="shikiri - discount",
        vars={"c1": "条件", "c2": None},
        computed_vars={
            "discount": {
                "expr": "c1 + c2",
                "divide_by": {
                    "field": "ケース入数",
                    "when": {"field": "数量単位", "equals": "CS"},
                    "default": 1,
                    "zero_policy": "skip_divide",
                },
            }
        },
    )

    def test_cs_divides_by_case_qty(self):
        """数量単位=CS, ケース入数=10 → discount = (100+0)/10 = 10, net=990."""
        cols = {"条件": 100, "数量単位": "CS", "ケース入数": 10}
        result = _run_calc_net("mock_form_B", self._NET, cols, shikiri=1000)
        assert result == pytest.approx(990.0)

    def test_non_cs_uses_default(self):
        """数量単位≠CS → divide_by 조건 불충족, default=1 유지 → discount=200."""
        cols = {"条件": 200, "数量単位": "個", "ケース入数": 10}
        result = _run_calc_net("mock_form_B", self._NET, cols, shikiri=1000)
        # condition_met=False → else branch: default=1 → base/1=200
        assert result == pytest.approx(800.0)


# ── 케이스 3: teiban 필요 (shikiri - teiban - self) ──────────────────────────

class TestTeibanRequired:
    _NET = _make_net_cfg(
        expr="shikiri - teiban - self_val",
        vars={"self_val": "未収条件"},
        needs_teiban=True,
    )

    def test_teiban_and_self_subtracted(self):
        """shikiri=1000, teiban=200, self=50 → 750."""
        cols = {"未収条件": 50}
        result = _run_calc_net(
            "mock_form_C", self._NET, cols,
            shikiri=1000, teiban=200,
        )
        assert result == pytest.approx(750.0)

    def test_teiban_zero_uses_shikiri_minus_self(self):
        """teiban=0이면 shikiri - 0 - self."""
        cols = {"未収条件": 100}
        result = _run_calc_net(
            "mock_form_C", self._NET, cols,
            shikiri=500, teiban=0,
        )
        assert result == pytest.approx(400.0)


# ── 케이스 4: divisor=0 안전 처리 ────────────────────────────────────────────

class TestDivisorZero:
    def test_zero_policy_skip_divide_keeps_base(self):
        """divisor=0 + zero_policy=skip_divide → 나누지 않고 base 유지."""
        net = _make_net_cfg(
            expr="shikiri - discount",
            vars={"c1": "条件"},
            computed_vars={
                "discount": {
                    "expr": "c1",
                    "divide_by": {
                        "field": "ケース入数",
                        "when": {"field": "数量単位", "equals": "CS"},
                        "zero_policy": "skip_divide",
                    },
                }
            },
        )
        # ケース入数=0, CS → 나누기 시도하지만 divisor=0 → skip_divide → discount=100
        cols = {"条件": 100, "数量単位": "CS", "ケース入数": 0}
        result = _run_calc_net("mock_form_D", net, cols, shikiri=1000)
        assert result == pytest.approx(900.0)  # discount=100 그대로

    def test_zero_policy_return_none(self):
        """divisor=0 + zero_policy=return_none → None 반환."""
        net = _make_net_cfg(
            expr="shikiri - discount",
            vars={"c1": "条件"},
            computed_vars={
                "discount": {
                    "expr": "c1",
                    "divide_by": {
                        "field": "ケース入数",
                        "when": {"field": "数量単位", "equals": "CS"},
                        "zero_policy": "return_none",
                    },
                }
            },
        )
        cols = {"条件": 100, "数量単位": "CS", "ケース入数": 0}
        result = _run_calc_net("mock_form_D", net, cols, shikiri=1000)
        assert result is None

    def test_direct_division_by_zero_in_expr_raises(self):
        """expr 내 직접 0 나누기 → ZeroDivisionError (명확한 오류 메시지)."""
        with pytest.raises(ZeroDivisionError, match="0 나누기"):
            _safe_eval("100 / 0", {}, _form_id="mock_test")


# ── 케이스 5: Unknown variable → 명확한 오류 ─────────────────────────────────

class TestUnknownVariable:
    def test_unknown_var_in_expr_raises_with_name(self):
        """정의되지 않은 변수 사용 → 변수명과 사용 가능한 목록이 오류에 포함됨."""
        net = _make_net_cfg(
            expr="shikiri - nonexistent_var",
        )
        with pytest.raises(ValueError) as exc_info:
            _run_calc_net("mock_form_E", net, {}, shikiri=1000)

        msg = str(exc_info.value)
        assert "nonexistent_var" in msg, f"변수명이 오류에 없음: {msg}"
        assert "사용 가능한 변수" in msg or "알 수 없는 변수" in msg, \
            f"오류 메시지가 도움이 안 됨: {msg}"
        assert "mock_form_E" in msg or "form=" in msg, \
            f"form_id가 오류에 없음: {msg}"

    def test_unknown_var_in_computed_var_expr(self):
        """computed_var.expr에 미정의 변수 → 오류에 computed_var 이름 포함."""
        net = _make_net_cfg(
            expr="shikiri - discount",
            computed_vars={
                "discount": {"expr": "undefined_var + c1"}
            },
        )
        with pytest.raises(ValueError) as exc_info:
            _run_calc_net("mock_form_E", net, {}, shikiri=1000)

        msg = str(exc_info.value)
        assert "undefined_var" in msg or "discount" in msg, \
            f"오류 메시지가 충분하지 않음: {msg}"

    def test_forbidden_ast_node_raises(self):
        """함수 호출 시도 → 허용되지 않은 AST 노드 오류."""
        with pytest.raises(ValueError, match="허용되지 않은|Unsupported"):
            _safe_eval("abs(-1)", {}, _form_id="mock_test")

    def test_comparison_operator_raises(self):
        """비교 연산자 → 허용되지 않은 AST 노드 오류."""
        with pytest.raises(ValueError):
            _safe_eval("shikiri > 0", {"shikiri": 100}, _form_id="mock_test")


# ── 케이스 6: 수식 미정의 → 명확한 오류 ─────────────────────────────────────

class TestPluginNotRegistered:
    def test_undefined_formula_raises_with_guidance(self):
        """net 수식이 전혀 없는 form → 명확한 안내 오류."""
        mock_form_types = {
            "mock_no_formula": {
                "net": {}  # formula_type도 formula도 없음
            }
        }

        with patch.dict("scripts.phase4_calc.FORM_TYPES", mock_form_types):
            with pytest.raises(ValueError) as exc_info:
                calc_net("mock_no_formula", cols={}, shikiri=1000)

        msg = str(exc_info.value)
        assert "mock_no_formula" in msg or "form=" in msg
        assert "formula" in msg.lower() or "수식" in msg or "미정의" in msg
