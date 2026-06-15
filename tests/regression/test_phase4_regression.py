"""
Phase 4 회귀 테스트.
run()을 self-contained 픽스처 번들로 호출하고 반환값을 정답 픽스처와 비교한다.

입력 박제 (Sheets/extracted 의존 제거):
  tests/fixtures/regression/<case>/extracted/<doc_id>/phase3_output.json  ← 입력
  tests/fixtures/regression/<case>/mappings/{unit_price,retail_user}.csv  ← 참조 마스터
  (생성: scripts/gen_regression_fixture.py)

번들이 있으면 그것을 base_dir로 쓰고 _sheets_store를 끈다 → 네트워크·Sheets 변경·
로컬 extracted 상태와 무관하게 "코드 변경"만이 유일한 변수가 된다.
번들이 없으면 (아직 박제 안 된 케이스) 로컬 extracted/로 폴백하되 없으면 skip.

정답 픽스처(form_XX_expected.json)는 현재 코드로 생성한 NET 출력이다.
DSL 전환·리팩터 등 어떤 변경 후에도 이 테스트가 통과해야 한다.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures"
BUNDLES = FIXTURES / "regression"


def _bundle_or_skip(case_id: str, doc_id: str) -> "pytest.MarkDecorator":
    """번들도 로컬 extracted도 없으면 skip."""
    has_bundle = (BUNDLES / case_id / "extracted" / doc_id / "phase3_output.json").exists()
    has_local = (ROOT / "extracted" / doc_id / "phase3_output.json").exists()
    return pytest.mark.skipif(
        not (has_bundle or has_local),
        reason=f"{case_id}: 박제 번들·로컬 extracted 모두 없음 — gen_regression_fixture.py로 박제",
    )


CASES = [
    pytest.param(
        "4월伊藤忠食品株式会社登録番号T2120001077362",
        "form_01",
        "form_01_expected.json",
        marks=_bundle_or_skip("form_01", "4월伊藤忠食品株式会社登録番号T2120001077362"),
        id="form_01",
    ),
    pytest.param(
        "2월日本アクセスＣＶＳ③",
        "form_04",
        "form_04_expected.json",
        marks=_bundle_or_skip("form_04", "2월日本アクセスＣＶＳ③"),
        id="form_04",
    ),
]


def _run_case(doc_id: str, fixture_name: str):
    """번들이 있으면 Sheets 끄고 번들로, 없으면 로컬 extracted로 run()."""
    import scripts.phase4_calc as pc

    case_id = fixture_name.replace("_expected.json", "")
    bundle = BUNDLES / case_id
    if (bundle / "extracted" / doc_id / "phase3_output.json").exists():
        saved = pc._sheets_store
        pc._sheets_store = None  # 번들의 로컬 마스터 CSV 강제 사용
        try:
            return pc.run(doc_id, save=False, base_dir=str(bundle))
        finally:
            pc._sheets_store = saved
    # 폴백: 로컬 extracted + (운영 Sheets) — 박제 안 된 케이스용
    return pc.run(doc_id, save=False)


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("doc_id,form_id,fixture_name", CASES)
def test_row_count_unchanged(doc_id, form_id, fixture_name):
    """행 수가 픽스처와 동일해야 한다."""
    rows_out, _xv = _run_case(doc_id, fixture_name)
    fixture = _load_fixture(fixture_name)
    assert len(rows_out) == len(fixture["rows"]), (
        f"행 수 불일치: expected={len(fixture['rows'])}, actual={len(rows_out)}"
    )


@pytest.mark.parametrize("doc_id,form_id,fixture_name", CASES)
def test_net_values_unchanged(doc_id, form_id, fixture_name):
    """NET 계산값 목록(정렬)이 픽스처와 동일해야 한다."""
    rows_out, _xv = _run_case(doc_id, fixture_name)
    fixture = _load_fixture(fixture_name)

    actual_nets   = sorted(r.get("NET")           for r in rows_out)
    expected_nets = sorted(r.get("NET")           for r in fixture["rows"])
    assert actual_nets == expected_nets, (
        f"NET 값 집합 불일치\n"
        f"  expected (sorted): {expected_nets[:10]}...\n"
        f"  actual   (sorted): {actual_nets[:10]}..."
    )


@pytest.mark.parametrize("doc_id,form_id,fixture_name", CASES)
def test_net_total_unchanged(doc_id, form_id, fixture_name):
    """NET 합계가 픽스처와 동일해야 한다 (None 제외, 소수점 2자리 반올림)."""
    rows_out, _xv = _run_case(doc_id, fixture_name)
    fixture = _load_fixture(fixture_name)

    def net_sum(rows):
        return round(sum(r["NET"] for r in rows if r.get("NET") is not None), 2)

    assert net_sum(rows_out) == net_sum(fixture["rows"]), (
        f"NET 합계 불일치: "
        f"expected={net_sum(fixture['rows'])}, actual={net_sum(rows_out)}"
    )


@pytest.mark.parametrize("doc_id,form_id,fixture_name", CASES)
def test_xv_unchanged(doc_id, form_id, fixture_name):
    """교차검증 결과(ok 여부)가 픽스처와 동일해야 한다."""
    _rows, xv = _run_case(doc_id, fixture_name)
    fixture = _load_fixture(fixture_name)

    fixture_xv = {item["label"]: item for item in fixture["xv"]}
    errors = []
    for label, expected, actual, ok in xv:
        if label not in fixture_xv:
            errors.append(f"[신규 xv] {label}")
            continue
        if fixture_xv[label]["ok"] != ok:
            errors.append(
                f"[xv ok 불일치] {label}: "
                f"expected={fixture_xv[label]['ok']}, actual={ok}"
            )

    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("doc_id,form_id,fixture_name", CASES)
def test_summary_total_unchanged(doc_id, form_id, fixture_name):
    """세전 금액 합계(total_ex)가 픽스처와 동일해야 한다."""
    rows_out, _xv = _run_case(doc_id, fixture_name)
    fixture = _load_fixture(fixture_name)

    act_total = int(sum(r.get("未収金額合計", 0) or 0 for r in rows_out))
    exp_total = fixture.get("summary", {}).get("total_ex")
    if exp_total is not None:
        assert act_total == exp_total, (
            f"summary total_ex: expected={exp_total}, actual={act_total}"
        )
