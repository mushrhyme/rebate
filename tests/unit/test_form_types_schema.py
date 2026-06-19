"""test_form_types_schema.py — config/form_types.json JSON Schema 검증

실행: pytest tests/unit/test_form_types_schema.py -v

검증 항목:
  1. 현재 form_types.json이 schema를 통과하는지
  2. formula_type="expr" 필수 필드 누락 시 실패
  3. formula_type="plugin" 필수 필드 누락 시 실패
  4. 유효하지 않은 formula 이름 시 실패
  5. computed_vars divide_by 구조 검증
  6. 어떤 form_id / 필드가 문제인지 식별 가능한 오류 메시지
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

try:
    from jsonschema import validate, ValidationError, Draft7Validator
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

_SCHEMA_PATH   = ROOT / "config" / "form_types.schema.json"
_CONFIG_PATH   = ROOT / "config" / "form_types.json"

pytestmark = pytest.mark.skipif(
    not HAS_JSONSCHEMA,
    reason="jsonschema 미설치 — pip install jsonschema",
)


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def form_types() -> dict:
    return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))


# ── 1. 현재 config가 schema 통과 ─────────────────────────────────────────────

class TestCurrentConfigPassesSchema:
    def test_full_config_validates(self, schema, form_types):
        """config/form_types.json 전체가 schema를 통과해야 한다."""
        errors = list(Draft7Validator(schema).iter_errors(form_types))
        if errors:
            details = []
            for e in errors:
                path = " → ".join(str(p) for p in e.absolute_path)
                details.append(f"  [{path}] {e.message}")
            pytest.fail("form_types.json schema 검증 실패:\n" + "\n".join(details))

    def test_form_01_net_is_expr(self, form_types):
        """form_01.net.formula_type == 'expr'"""
        assert form_types["form_01"]["net"]["formula_type"] == "expr"

    def test_form_01_has_expr_field(self, form_types):
        """form_01.net.expr이 비어 있지 않아야 한다."""
        expr = form_types["form_01"]["net"].get("expr", "")
        assert expr, "form_01.net.expr 비어 있음"

    def test_form_01_computed_vars_structure(self, form_types, schema):
        """form_01.net.computed_vars 각 항목이 schema를 통과해야 한다."""
        cv = form_types["form_01"]["net"].get("computed_vars", {})
        # computed_vars 전체를 form config 안에 넣어 full schema context로 검증
        for var_name, var_cfg in cv.items():
            wrapped = {
                "form_01": {
                    "net": {
                        "formula_type": "expr",
                        "expr": "shikiri - discount",
                        "computed_vars": {var_name: var_cfg}
                    }
                }
            }
            errors = list(Draft7Validator(schema).iter_errors(wrapped))
            if errors:
                details = [e.message for e in errors]
                pytest.fail(
                    f"form_01.net.computed_vars.{var_name}: {details}"
                )

    def test_form_04_net_is_expr_with_needs_teiban(self, form_types):
        """form_04.net.formula_type == 'expr' + needs_teiban == True"""
        net = form_types["form_04"]["net"]
        assert net["formula_type"] == "expr"
        assert net.get("needs_teiban") is True

    @pytest.mark.parametrize("form_id", ["form_01", "form_04"])
    def test_each_form_individually(self, schema, form_types, form_id):
        """각 form을 개별적으로 검증해 어느 form이 실패하는지 명확히 표시."""
        wrapped = {form_id: form_types[form_id]}
        errors = list(Draft7Validator(schema).iter_errors(wrapped))
        if errors:
            details = [f"  {e.absolute_path}: {e.message}" for e in errors]
            pytest.fail(f"{form_id} schema 실패:\n" + "\n".join(details))


# ── 2. schema가 잘못된 구조를 잡아내는지 ─────────────────────────────────────

class TestSchemaRejectsInvalidStructures:
    def test_expr_missing_expr_field(self, schema):
        """formula_type=expr인데 expr 없으면 schema 실패."""
        invalid = {
            "form_x": {
                "net": {
                    "formula_type": "expr"
                    # expr 누락
                }
            }
        }
        errors = list(Draft7Validator(schema).iter_errors(invalid))
        assert any("expr" in str(e.message) or e.validator == "required"
                   for e in errors), \
            "expr 누락인데 schema가 통과함"

    def test_invalid_legacy_formula_name(self, schema):
        """지원되지 않는 legacy formula 이름은 schema 실패."""
        invalid = {
            "form_x": {
                "net": {
                    "formula": "unknown_formula"
                }
            }
        }
        errors = list(Draft7Validator(schema).iter_errors(invalid))
        assert errors, "알 수 없는 formula 이름인데 schema가 통과함"

    def test_preprocess_unknown_op(self, schema):
        """preprocess에 알 수 없는 op는 schema 실패."""
        invalid = {
            "form_x": {
                "preprocess": [
                    {"field": "条件", "op": "unknown_op"}
                ]
            }
        }
        errors = list(Draft7Validator(schema).iter_errors(invalid))
        assert errors, "unknown preprocess op인데 schema가 통과함"

    def test_divide_by_invalid_zero_policy(self, schema):
        """divide_by.zero_policy에 허용되지 않은 값은 실패."""
        invalid = {
            "form_x": {
                "net": {
                    "formula_type": "expr",
                    "expr": "shikiri - d",
                    "computed_vars": {
                        "d": {
                            "expr": "c1",
                            "divide_by": {
                                "field": "ケース入数",
                                "zero_policy": "invalid_policy"
                            }
                        }
                    }
                }
            }
        }
        errors = list(Draft7Validator(schema).iter_errors(invalid))
        assert errors, "invalid zero_policy인데 통과함"

    def test_divide_by_missing_required_field(self, schema):
        """divide_by에 field 키 없으면 실패."""
        invalid = {
            "form_x": {
                "net": {
                    "formula_type": "expr",
                    "expr": "shikiri - d",
                    "computed_vars": {
                        "d": {
                            "expr": "c1",
                            "divide_by": {
                                "zero_policy": "skip_divide"
                                # field 누락
                            }
                        }
                    }
                }
            }
        }
        errors = list(Draft7Validator(schema).iter_errors(invalid))
        assert errors, "divide_by field 누락인데 통과함"

    def test_computed_var_no_extra_keys(self, schema):
        """computed_var에 정의되지 않은 키가 있으면 실패 (additionalProperties: false)."""
        invalid = {
            "form_x": {
                "net": {
                    "formula_type": "expr",
                    "expr": "shikiri - d",
                    "computed_vars": {
                        "d": {
                            "expr": "c1 + c2",
                            "unknown_key": "should_fail"
                        }
                    }
                }
            }
        }
        errors = list(Draft7Validator(schema).iter_errors(invalid))
        assert errors, "computed_var 불필요 키인데 통과함"


# ── 3. 유효한 구조는 통과 ────────────────────────────────────────────────────

class TestSchemaAcceptsValidStructures:
    def test_valid_expr_minimal(self, schema):
        """최소 expr 구조는 통과."""
        valid = {
            "form_x": {
                "net": {
                    "formula_type": "expr",
                    "expr": "shikiri - c1"
                }
            }
        }
        errors = list(Draft7Validator(schema).iter_errors(valid))
        assert not errors, f"유효한 expr인데 실패: {[e.message for e in errors]}"

    def test_valid_expr_with_computed_vars(self, schema):
        """computed_vars 포함 expr 구조는 통과."""
        valid = {
            "form_x": {
                "net": {
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
                                "zero_policy": "skip_divide"
                            }
                        }
                    },
                    "no_net_kubun": ["円"],
                    "needs_teiban": False
                }
            }
        }
        errors = list(Draft7Validator(schema).iter_errors(valid))
        assert not errors, f"유효한 computed_vars 구조인데 실패: {[e.message for e in errors]}"

    def test_valid_legacy_subtract_conditions(self, schema):
        """하위 호환 legacy formula는 통과."""
        valid = {
            "form_x": {
                "net": {
                    "formula": "subtract_conditions",
                    "c1": "条件",
                    "c2": None,
                    "cs_divide_by_case_qty": True
                }
            }
        }
        errors = list(Draft7Validator(schema).iter_errors(valid))
        assert not errors, f"유효한 legacy formula인데 실패: {[e.message for e in errors]}"

    def test_unknown_toplevel_keys_allowed(self, schema):
        """form config 내 알 수 없는 최상위 키는 허용 (additionalProperties: true)."""
        valid = {
            "form_x": {
                "net": {
                    "formula_type": "expr",
                    "expr": "shikiri - c1"
                },
                "unknown_future_key": "allowed"
            }
        }
        errors = list(Draft7Validator(schema).iter_errors(valid))
        assert not errors, f"unknown 키 허용이어야 하는데 실패: {[e.message for e in errors]}"


# ── 3. cross_validation type 어휘 enum (무음 무시 차단) ───────────────────────

class TestCrossValidationTypeEnum:
    """교차검증 type을 구현된 6종으로 제한 — 어휘 밖 type이 조용히 무시되는 무음 실패 차단.

    이전엔 schema가 cross_validation을 `{"type":"array"}`로만 두어 임의 type이 통과했고,
    phase4_calc는 if/elif 체인에 else가 없어 모르는 type을 조용히 건너뛰었다.
    """

    _VALID_TYPES = [
        "cover_honbai_vs_detail", "cover_breakdown_vs_detail", "cover_taxex_vs_detail",
        "cover_total_vs_summary", "summary_vs_detail", "per_customer_vs_summary",
    ]

    @pytest.mark.parametrize("rtype", _VALID_TYPES)
    def test_known_types_pass(self, schema, rtype):
        valid = {"form_x": {"cross_validation": [{"type": rtype, "label": "검증"}]}}
        errors = list(Draft7Validator(schema).iter_errors(valid))
        assert not errors, f"구현된 type인데 실패: {rtype} — {[e.message for e in errors]}"

    def test_unknown_type_rejected(self, schema):
        """어휘 밖 type → schema 실패 (sync/build에서 시끄럽게 차단)."""
        invalid = {"form_x": {"cross_validation": [{"type": "made_up_check", "label": "검증"}]}}
        errors = list(Draft7Validator(schema).iter_errors(invalid))
        assert errors, "어휘 밖 cross_validation type이 schema를 통과함(무음 무시 위험)"

    def test_missing_type_rejected(self, schema):
        invalid = {"form_x": {"cross_validation": [{"label": "검증"}]}}
        errors = list(Draft7Validator(schema).iter_errors(invalid))
        assert errors, "type 없는 cross_validation 규칙이 통과함"

    def test_current_config_cross_validation_valid(self, form_types, schema):
        """현 form_types.json의 모든 cross_validation type이 enum 안에 있어야 한다."""
        for form_id, cfg in form_types.items():
            for rule in cfg.get("cross_validation", []):
                assert rule.get("type") in self._VALID_TYPES, \
                    f"{form_id}: 미구현 cross_validation type {rule.get('type')!r}"
