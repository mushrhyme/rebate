"""test_render_form_prose.py — 블록 → 실행 규칙 렌더 + 주입(멱등·prose 불변)

literate config 정본 통일: 사람이 읽는 규칙은 블록 생성물이라 드리프트 불가.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import scripts.render_form_prose as rp  # noqa: E402


_ENTRY = {
    "net": {"formula_type": "expr", "expr": "shikiri - teiban - c1",
            "vars": {"c1": "未収条件"}, "teiban_type": "定番条件"},
    "cross_validation": [
        {"type": "cover_honbai_vs_detail", "label": "Cover vs Detail", "cover_key": "本体合計金額"},
    ],
    "product_aggregate": {"strategy": "subset_subtract", "base_condition": "定番条件",
                          "qty_field": "数量", "amount_field": "金額"},
    "show_sections": ["xv"],
}


def test_render_substitutes_expr_tokens():
    out = rp.render_rules(_ENTRY)
    # expr 토큰이 실제 일본어 명칭으로 치환됨
    assert "仕切" in out and "定番条件" in out and "未収条件" in out
    assert "shikiri" not in out  # 변수 토큰이 그대로 노출되지 않음
    assert rp.BEGIN in out and rp.END in out


def test_render_uses_cross_validation_label():
    out = rp.render_rules(_ENTRY)
    assert "Cover vs Detail" in out
    assert "本体合計金額" in out


def test_inject_is_idempotent():
    md = "# form_x\n\n## 식별 패턴\nABC\n\n## [config] 실행 설정\n\n```json\n{}\n```\n"
    once = rp.inject_before_config(md, _ENTRY)
    twice = rp.inject_before_config(once, _ENTRY)
    assert once == twice, "주입이 멱등이 아님 — 재실행 시 중복/변형"
    # auto-rules가 [config] 앞에 위치
    assert once.index(rp.BEGIN) < once.index("## [config]")


def test_strip_auto_rules_restores_prose():
    md = "# form_x\n\n## 식별 패턴\nABC\n\n## [config] 실행 설정\n\n```json\n{}\n```\n"
    injected = rp.inject_before_config(md, _ENTRY)
    stripped = rp.strip_auto_rules(injected)
    assert rp.BEGIN not in stripped
    # 손으로 쓴 산문·블록은 보존
    assert "## 식별 패턴" in stripped and "## [config]" in stripped


def test_strip_auto_rules_noop_when_absent():
    md = "# form_x\n\n## 식별 패턴\nABC\n"
    assert rp.strip_auto_rules(md) == md
