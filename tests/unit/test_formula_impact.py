"""#4 formula 영향 가시화 — sync 시 골든 번들 재계산으로 변동 행수·금액 차이 산출.

차단(자동 회귀 게이트)이 아니라 영향 표시: 현업이 "12행 / +34,000엔 바뀜"을 보고
의도된 변경인지 판단하게 한다. 골든 번들(tests/fixtures/regression/<form_id>/)이
없으면 available=False로 sync를 막지 않는다.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from backend.api.routes.forms import _compute_formula_impact  # noqa: E402

_FT = json.loads((ROOT / "config" / "form_types.json").read_text(encoding="utf-8"))


def _bundle_missing(form_id: str) -> bool:
    return not (ROOT / "tests" / "fixtures" / "regression" / form_id / "extracted").is_dir()


@pytest.mark.skipif(_bundle_missing("form_01"), reason="form_01 골든 번들 없음")
def test_identical_config_no_change():
    old = _FT["form_01"]
    imp = _compute_formula_impact("form_01", old, old)
    assert imp["available"] is True
    assert imp["rows_changed"] == 0
    assert imp["net_delta"] == 0.0
    assert imp["rows_total"] > 0


@pytest.mark.skipif(_bundle_missing("form_01"), reason="form_01 골든 번들 없음")
def test_formula_change_detected():
    old = _FT["form_01"]
    new = json.loads(json.dumps(old))
    new["net"]["expr"] = "shikiri"  # discount 제거 → 전 행 NET 변동
    imp = _compute_formula_impact("form_01", old, new)
    assert imp["available"] is True
    assert imp["rows_changed"] == imp["rows_total"]  # 모든 행 변동
    assert imp["net_delta"] != 0.0
    assert imp["samples"] and "net_before" in imp["samples"][0]


def test_missing_bundle_returns_unavailable_not_error():
    """번들 없는 form_id → available=False (예외 아님 — sync 차단 금지)."""
    imp = _compute_formula_impact("form_99_nonexistent", {}, {"net": {"formula_type": "expr", "expr": "shikiri"}})
    assert imp["available"] is False
    assert "reason" in imp
