"""docs/output-format.md ↔ 출력 코드 contract test.

출력 컬럼은 전 양식 공통·고정이라 런타임 config가 아닌 코드 상수(_COLS)로 두되,
docs/output-format.md '## 컬럼 목록' 표와의 일치를 이 테스트로 강제한다.
→ md를 고치면 이 테스트가 깨져서 코드 반영을 잊을 수 없고, 그 역도 같다.
(md-driven 등급: D → B. 문서·코드 괴리가 CI에서 잡힌다.)
"""
import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]


def _parse_output_format_md() -> list[tuple[str, str, str]]:
    """'## 컬럼 목록' 표에서 (Excel열, 일본어명, 한국어 설명) 추출."""
    text = (BASE / "docs" / "output-format.md").read_text(encoding="utf-8")
    section = text.split("## 컬럼 목록", 1)[1].split("\n---", 1)[0]
    rows: list[tuple[str, str, str]] = []
    for line in section.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        col = cells[0]
        # 헤더·구분선 행 제외 — 데이터 행은 Excel 열 문자(B~BB)
        if not re.fullmatch(r"[A-Z]{1,2}", col):
            continue
        rows.append((col, cells[1], cells[2]))
    return rows


def test_sap_cols_match_output_format_md():
    """sap.py _COLS의 (열, 일본어, 한국어)가 output-format.md 표와 정확히 일치."""
    from backend.api.routes.sap import _COLS

    md_rows = _parse_output_format_md()
    assert len(md_rows) >= 50, "output-format.md 컬럼 표 파싱 실패 (행 수 부족)"

    code_rows = [(c, jp, ko) for c, ko, jp, _ in _COLS]

    md_map = {c: (jp, ko) for c, jp, ko in md_rows}
    code_map = {c: (jp, ko) for c, jp, ko in code_rows}

    assert set(md_map) == set(code_map), (
        f"컬럼 집합 불일치 — md에만: {set(md_map) - set(code_map)}, "
        f"코드에만: {set(code_map) - set(md_map)}"
    )

    mismatches = []
    for col in md_map:
        md_jp, md_ko = md_map[col]
        code_jp, code_ko = code_map[col]
        if md_jp != code_jp:
            mismatches.append(f"{col}: 일본어명 md={md_jp!r} code={code_jp!r}")
        if md_ko != code_ko:
            mismatches.append(f"{col}: 한국어 md={md_ko!r} code={code_ko!r}")
    assert not mismatches, "docs/output-format.md ↔ sap.py 불일치:\n" + "\n".join(mismatches)


def test_tax_rules_config_exists_and_valid():
    """세율은 config/tax_rules.json이 단일 출처 — 필수 키와 타입을 고정."""
    rules = json.loads((BASE / "config" / "tax_rules.json").read_text(encoding="utf-8"))
    assert isinstance(rules["zero_rated_types"], list) and rules["zero_rated_types"]
    assert isinstance(rules["rate_keywords"], dict) and rules["rate_keywords"]
    assert isinstance(rules["default_rate"], (int, float))
    assert "8" in rules["bracket_rates"] and "10" in rules["bracket_rates"]
    for v in rules["rate_keywords"].values():
        assert 0.0 <= float(v) <= 1.0
    # 일본 소비세 표준 케이스 회귀 고정
    assert rules["rate_keywords"]["10%"] == 0.10
    assert rules["default_rate"] == 0.08


def test_type_tax_rate_reads_config():
    """phase4._type_tax_rate가 config 값으로 동작 (기존 하드코딩 동작과 동일해야 함)."""
    from backend.pipeline.phase4 import _type_tax_rate

    assert _type_tax_rate("非課税") == 0.0
    assert _type_tax_rate("ロットアウト") == 0.0
    assert _type_tax_rate("") == 0.0
    assert _type_tax_rate("販促費10%") == 0.10
    assert _type_tax_rate("販促費8%") == 0.08
    assert _type_tax_rate("条件") == 0.08


def test_form_04_recovery_cell_map_pinned():
    """form_04 결정적 복구 셀 인덱스가 form_types.json에 존재 — sync가 지우면 여기서 잡힌다."""
    cfg = json.loads((BASE / "config" / "form_types.json").read_text(encoding="utf-8"))
    cm = cfg["form_04"]["row_anchor"]["recovery_cell_map"]
    assert cm["product"] == 1
    assert cm["nyusuu"] == 2
    assert cm["qty"] == 3
    assert cm["cond"] == 5
    assert cm["biko"] == 8
    assert cm["cond_field"] == "未収条件"
    assert cm["cond_scale"] == 0.01


def test_phase3_tool_use_product_prompt_md_exists():
    """제품 매핑 Tool Use 프롬프트가 docs md에서 로드 가능해야 한다 (인라인 금지)."""
    from backend.pipeline.phase3_fallback import _get_product_system_prompt

    prompt = _get_product_system_prompt()
    assert "매핑 판단 규칙" in prompt
    assert "not_found" in prompt
    # 코드펜스 밖 안내문이 섞이지 않았는지
    assert "phase3_fallback.py" not in prompt
