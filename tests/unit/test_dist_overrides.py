"""판매처 조건부 override (dist_overrides) 골든·안전 테스트.

form_04의 실재 규칙(jisho=CVS営業部 → 広域リテール 판매처)을 골든으로 박는다.
핵심 안전속성: 규칙 미선언/매칭실패/모호 시 None → 기존 경로(LLM)로 폴백(기본 OFF).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from backend.pipeline.dist_overrides import resolve_dist_override  # noqa: E402

# form_04 1:N 후보 (실제 판매처명 형태)
_CANDS = [
    {"dist_code": "D100", "dist_name": "株式会社日本アクセス広域リテール営業本部加工食品飲料部"},
    {"dist_code": "D200", "dist_name": "株式会社日本アクセス東北支店"},
]
# form_04 config의 실제 규칙
_RULES = [{"when": {"jisho": "CVS営業部"}, "pick_candidate_name_contains": "広域リテール"}]


def test_form04_cvs_eigyo_resolves_deterministically():
    """jisho=CVS営業部 → 広域リテール 포함 후보를 결정적으로 선택 (LLM 불필요)."""
    r = resolve_dist_override(_CANDS, {"jisho": "CVS営業部", "retailer_code": "R1"}, _RULES)
    assert r is not None
    assert r["dist_code"] == "D100"
    assert r["rule"]["when"] == {"jisho": "CVS営業部"}


def test_no_rules_returns_none():
    """규칙 미선언 → 항상 None (기본 OFF, 기존 동작 보존)."""
    assert resolve_dist_override(_CANDS, {"jisho": "CVS営業部"}, None) is None
    assert resolve_dist_override(_CANDS, {"jisho": "CVS営業部"}, []) is None


def test_when_mismatch_returns_none():
    """술어 불일치(다른 jisho) → None → 폴백."""
    r = resolve_dist_override(_CANDS, {"jisho": "R営業東北", "retailer_code": "R1"}, _RULES)
    assert r is None


def test_ambiguous_match_falls_back():
    """name_contains가 2개+ 일치(모호) → None (추측 금지, 폴백)."""
    cands = [
        {"dist_code": "D1", "dist_name": "広域リテールA"},
        {"dist_code": "D2", "dist_name": "広域リテールB"},
    ]
    assert resolve_dist_override(cands, {"jisho": "CVS営業部"}, _RULES) is None


def test_zero_match_falls_back():
    """name_contains가 0개 일치 → None (폴백)."""
    cands = [{"dist_code": "D9", "dist_name": "전혀다른판매처"}]
    assert resolve_dist_override(cands, {"jisho": "CVS営業部"}, _RULES) is None


def test_dist_code_action():
    """dist_code 액션: 후보 중 해당 코드 유일 선택."""
    rules = [{"when": {"retailer_code": "R001"}, "dist_code": "D200"}]
    r = resolve_dist_override(_CANDS, {"retailer_code": "R001"}, rules)
    assert r is not None and r["dist_code"] == "D200"


def test_multi_field_when_is_and():
    """when 다중 필드는 AND — 하나라도 어긋나면 미적용."""
    rules = [{"when": {"jisho": "CVS営業部", "retailer_code": "R001"},
              "pick_candidate_name_contains": "広域リテール"}]
    assert resolve_dist_override(_CANDS, {"jisho": "CVS営業部", "retailer_code": "R999"}, rules) is None
    assert resolve_dist_override(_CANDS, {"jisho": "CVS営業部", "retailer_code": "R001"}, rules) is not None
