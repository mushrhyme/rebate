"""test_cross_validation_vocab.py — 교차검증 어휘 밖 type 런타임 차단.

phase4_calc.calc_cross_validation의 if/elif 체인에 else가 없어 모르는 type을
조용히 건너뛰던 무음 실패를 회귀로 박는다. 스키마 enum이 sync에서 1차로 막지만,
json 손편집·구버전 우회 시의 런타임 방어선이 이 테스트의 대상이다.

실행: pytest tests/unit/test_cross_validation_vocab.py -v
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from scripts.phase4_calc import calc_cross_validation  # noqa: E402


def _call(rules):
    return calc_cross_validation(
        form_cfg={"cross_validation": rules},
        rows_out=[],
        cover_pages=[],
        summary_totals={},
        detail_ex=0.0,
    )


def test_unknown_type_raises_loudly():
    """어휘 밖 type → ValueError (조용히 건너뛰지 않음)."""
    with pytest.raises(ValueError, match="알 수 없는 cross_validation type"):
        _call([{"type": "made_up_check", "label": "검증"}])


def test_known_type_does_not_raise():
    """구현된 type은 예외 없이 통과 (데이터가 비어 결과는 빈 리스트)."""
    assert _call([{"type": "summary_vs_detail", "label": "요약 vs 명세"}]) == []


def test_empty_rules_ok():
    assert _call([]) == []
