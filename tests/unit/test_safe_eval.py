"""
_safe_eval / _eval_expr 단위 테스트.
Phase 2에서 함수가 구현되면 이 스텁이 실제 테스트가 된다.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))


# ── Phase 2 구현 전까지 import가 없으면 skip ──────────────────────────────────
def _import_safe_eval():
    from scripts.phase4_calc import _safe_eval
    return _safe_eval


def _import_eval_expr():
    from scripts.phase4_calc import _eval_expr
    return _eval_expr


# ── _safe_eval 기본 산술 ───────────────────────────────────────────────────────
class TestSafeEval:
    def test_simple_subtraction(self):
        _safe_eval = _import_safe_eval()
        assert _safe_eval("shikiri - c1", {"shikiri": 100.0, "c1": 30.0}) == 70.0

    def test_addition(self):
        _safe_eval = _import_safe_eval()
        assert _safe_eval("c1 + c2", {"c1": 10.0, "c2": 5.0}) == 15.0

    def test_parentheses(self):
        _safe_eval = _import_safe_eval()
        assert _safe_eval("shikiri - (c1 + c2)", {"shikiri": 200.0, "c1": 30.0, "c2": 20.0}) == 150.0

    def test_division(self):
        _safe_eval = _import_safe_eval()
        assert _safe_eval("c1 / c2", {"c1": 100.0, "c2": 4.0}) == 25.0

    def test_nested_parentheses(self):
        _safe_eval = _import_safe_eval()
        result = _safe_eval("shikiri - (c1 + c2) / case_qty",
                            {"shikiri": 500.0, "c1": 60.0, "c2": 40.0, "case_qty": 10.0})
        assert result == 490.0  # 500 - 100/10

    def test_zero_variable(self):
        _safe_eval = _import_safe_eval()
        assert _safe_eval("shikiri - c1", {"shikiri": 100.0, "c1": 0.0}) == 100.0

    def test_negative_result(self):
        _safe_eval = _import_safe_eval()
        assert _safe_eval("shikiri - c1", {"shikiri": 50.0, "c1": 80.0}) == -30.0

    def test_unknown_variable_raises(self):
        _safe_eval = _import_safe_eval()
        with pytest.raises((ValueError, KeyError)):
            _safe_eval("shikiri - unknown_var", {"shikiri": 100.0})

    def test_function_call_blocked(self):
        _safe_eval = _import_safe_eval()
        with pytest.raises(Exception):
            _safe_eval("__import__('os').system('ls')", {})

    def test_comparison_blocked(self):
        _safe_eval = _import_safe_eval()
        with pytest.raises(Exception):
            _safe_eval("c1 > 0", {"c1": 10.0})


# ── _eval_expr: computed_vars (CS divide) ────────────────────────────────────
class TestEvalExpr:
    """subtract_conditions (CS 있음) 케이스."""

    def _cs_cfg(self):
        return {
            "formula_type": "expr",
            "expr": "shikiri - discount",
            "vars": {"c1": "条件", "c2": None},
            "computed_vars": {
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
        }

    def test_non_cs_unit(self):
        """個 단위 — discount를 나누지 않음."""
        _eval_expr = _import_eval_expr()
        cfg = self._cs_cfg()
        cols = {"条件": 30.0, "数量単位": "個", "ケース入数": 10.0}
        result = _eval_expr(cfg, cols, shikiri=200.0, teiban_joken=0.0)
        assert result == 170.0  # 200 - 30

    def test_cs_unit_divides_discount(self):
        """CS 단위 — discount를 ケース入数로 나눔."""
        _eval_expr = _import_eval_expr()
        cfg = self._cs_cfg()
        cols = {"条件": 60.0, "数量単位": "CS", "ケース入数": 10.0}
        result = _eval_expr(cfg, cols, shikiri=200.0, teiban_joken=0.0)
        assert result == 194.0  # 200 - 60/10

    def test_cs_zero_case_qty_skips_divide(self):
        """ケース入数 = 0 — zero_policy: skip_divide → 나누지 않음."""
        _eval_expr = _import_eval_expr()
        cfg = self._cs_cfg()
        cols = {"条件": 60.0, "数量単位": "CS", "ケース入数": 0.0}
        result = _eval_expr(cfg, cols, shikiri=200.0, teiban_joken=0.0)
        assert result == 140.0  # 200 - 60 (나누지 않음)

    def test_null_c2_treated_as_zero(self):
        """c2: null → 0으로 처리."""
        _eval_expr = _import_eval_expr()
        cfg = self._cs_cfg()
        cols = {"条件": 30.0, "数量単位": "個", "ケース入数": 10.0}
        result = _eval_expr(cfg, cols, shikiri=200.0, teiban_joken=0.0)
        assert result == 170.0  # c2 없어도 정상 계산

    def test_teiban_variable_in_ctx(self):
        """teiban 변수가 ctx에 올바르게 주입된다."""
        _eval_expr = _import_eval_expr()
        cfg = {
            "formula_type": "expr",
            "expr": "shikiri - teiban - c1",
            "vars": {"c1": "未収条件"},
        }
        cols = {"未収条件": 20.0}
        result = _eval_expr(cfg, cols, shikiri=200.0, teiban_joken=50.0)
        assert result == 130.0  # 200 - 50 - 20
