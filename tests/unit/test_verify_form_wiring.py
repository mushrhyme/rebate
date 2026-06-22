"""test_verify_form_wiring.py — verify_form_wiring 게이트·라우팅 검증

설계: 동기화 후 gap 검증 + 3갈래 라우팅(safe/owner/dev). 엔진 어휘는 소스에서 추출.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import scripts.verify_form_wiring as vfw  # noqa: E402


# ── 엔진 어휘 추출 (단일 출처) ───────────────────────────────────────────────
def test_engine_cross_validation_types_extracted():
    types = vfw.engine_cross_validation_types()
    # phase4_calc.py의 rtype 분기에서 추출 — 알려진 6종 포함
    for t in ["cover_honbai_vs_detail", "cover_breakdown_vs_detail",
              "cover_taxex_vs_detail", "per_customer_vs_summary"]:
        assert t in types


def test_aggregate_registry_has_subset_subtract():
    assert "subset_subtract" in vfw.AGGREGATE_STRATEGIES


# ── NET expr 어휘 파싱 ───────────────────────────────────────────────────────
def test_net_names_and_ops_basic():
    names, ops = vfw._net_names_and_ops({"expr": "shikiri - teiban - c1"})
    assert names == {"shikiri", "teiban", "c1"}
    assert all(vfw.engine_op_allowed(o) for o in ops)


def test_net_unsupported_op_detected():
    # '%'(Mod)는 _SAFE_OPS에 없음 → 미지원 연산
    _, ops = vfw._net_names_and_ops({"expr": "shikiri % 2"})
    assert any(not vfw.engine_op_allowed(o) for o in ops)


def test_net_computed_vars_names_collected():
    names, _ = vfw._net_names_and_ops({
        "expr": "shikiri - discount",
        "computed_vars": {"discount": {"expr": "c1 + c2"}},
    })
    assert {"shikiri", "discount", "c1", "c2"} <= names


# ── 통합: 실제 form_04 (등록·연결 완료 양식) ─────────────────────────────────
def _load():
    data = json.loads((ROOT / "config" / "form_types.json").read_text(encoding="utf-8"))
    return data, vfw.engine_cross_validation_types()


def test_form04_fully_wired_no_dev_gap():
    data, cv = _load()
    if "form_04" not in data:
        pytest.skip("form_04 미등록 환경")
    findings = vfw.verify("form_04", data, cv)
    grades = {f.gate: f.grade for f in findings}
    assert grades.get("block") == "ok"
    assert grades.get("schema") == "ok"
    assert grades.get("cross_validation") == "ok"
    assert grades.get("aggregate") == "ok"
    # 엔진 연결 양식이므로 dev(T3) gap 없어야 함
    assert not any(f.grade == "dev" for f in findings)


def test_unknown_cross_validation_type_routes_to_dev():
    """엔진에 없는 교차검증 type → dev(T3)로 라우팅."""
    data, cv = _load()
    if "form_04" not in data:
        pytest.skip("form_04 미등록 환경")
    # form_04 설정을 복제해 가짜 type 주입 — block과 어긋나도 cross_validation 게이트만 본다
    import copy
    entry = copy.deepcopy(data["form_04"])
    entry["cross_validation"] = [{"type": "made_up_type_xyz", "label": "x"}]
    data2 = {**data, "form_04": entry}
    findings = vfw.verify("form_04", data2, cv)
    cv_findings = [f for f in findings if f.gate == "cross_validation"]
    assert any(f.grade == "dev" for f in cv_findings), "미등록 type이 dev로 라우팅되지 않음"


def test_global_build_check_grade():
    """build ↔ json 정합 게이트는 ok(동치) 또는 safe(재빌드 가능)만 낸다."""
    f = vfw.check_global()
    assert f.grade in ("ok", "safe")
    if f.grade == "safe":
        assert f.fixer is not None  # safe는 자동수정자 보유
