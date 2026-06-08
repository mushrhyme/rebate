"""
Phase 4 회귀 테스트.
run()을 직접 호출하고 반환값을 tests/fixtures/ 픽스처와 비교한다.

픽스처는 Phase 1에서 현재 코드로 생성한 "정답" 출력이다.
이후 DSL 전환, 코드 리팩터 등 어떤 변경 후에도 이 테스트가 통과해야 한다.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures"

# doc_id → form_id, 픽스처 파일명
CASES = [
    (
        "4월伊藤忠食品株式会社登録番号T2120001077362",
        "form_01",
        "form_01_expected.json",
    ),
    (
        "3월日本アクセスＣＶＳ①",
        "form_04",
        "form_04_expected.json",
    ),
]


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("doc_id,form_id,fixture_name", CASES)
def test_row_count_unchanged(doc_id, form_id, fixture_name):
    """행 수가 픽스처와 동일해야 한다."""
    from scripts.phase4_calc import run

    rows_out, _xv = run(doc_id, save=False)
    fixture = _load_fixture(fixture_name)
    assert len(rows_out) == len(fixture["rows"]), (
        f"행 수 불일치: expected={len(fixture['rows'])}, actual={len(rows_out)}"
    )


@pytest.mark.parametrize("doc_id,form_id,fixture_name", CASES)
def test_net_values_unchanged(doc_id, form_id, fixture_name):
    """NET 계산값 목록(정렬)이 픽스처와 동일해야 한다."""
    from scripts.phase4_calc import run

    rows_out, _xv = run(doc_id, save=False)
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
    from scripts.phase4_calc import run

    rows_out, _xv = run(doc_id, save=False)
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
    from scripts.phase4_calc import run

    _rows, xv = run(doc_id, save=False)
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
    from scripts.phase4_calc import run

    rows_out, _xv = run(doc_id, save=False)
    fixture = _load_fixture(fixture_name)

    act_total = int(sum(r.get("未収金額合計", 0) or 0 for r in rows_out))
    exp_total = fixture.get("summary", {}).get("total_ex")
    if exp_total is not None:
        assert act_total == exp_total, (
            f"summary total_ex: expected={exp_total}, actual={act_total}"
        )
