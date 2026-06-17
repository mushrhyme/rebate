"""test_literate_config_guard.py — Literate config 단일 진실 소스 가드

설계: docs/literate-config-migration.md

이 테스트가 강제하는 불변식:
  config/form_types.json == build(form_definitions/form_XX.md의 [config] 정본 블록)

누군가 form_types.json을 손으로 고치거나, MD 블록을 고치고 재빌드를 빠뜨리면 적색.
즉 "정본은 MD 블록 하나, JSON은 생성물"을 실집행으로 보증한다.

실행: pytest tests/unit/test_literate_config_guard.py -v
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

_BUILD_PATH = ROOT / "scripts" / "build_form_types.py"
_CONFIG_PATH = ROOT / "config" / "form_types.json"


def _load_build_module():
    spec = importlib.util.spec_from_file_location("build_form_types", _BUILD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bft = _load_build_module()


def test_committed_json_equals_build_output():
    """config/form_types.json이 MD [config] 블록 빌드 결과와 바이트 동일해야 한다.

    실패 = 정본(MD 블록)과 생성물(JSON) 드리프트.
    해결: `python scripts/build_form_types.py` 로 재빌드.
    """
    built = bft.serialize(bft.build_forms())
    current = _CONFIG_PATH.read_text(encoding="utf-8")
    assert current == built, (
        "form_types.json이 form_XX.md [config] 블록 빌드 결과와 다릅니다. "
        "`python scripts/build_form_types.py`로 재빌드하세요."
    )


def test_build_values_match_committed_json():
    """값(파싱 결과) 동일성 — 직렬화 공백과 무관하게 의미가 같아야 한다."""
    built = bft.build_forms()
    current = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    assert built == current


def test_every_registered_form_has_block():
    """form_types.json의 모든 양식은 대응 MD에 [config] 블록을 가져야 한다.

    (배포 유실로 블록이 사라지면 빌드에서 누락 → 여기서 잡힌다.)
    """
    built = bft.build_forms()
    current = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    missing = set(current) - set(built)
    assert not missing, f"[config] 블록이 없어 빌드에서 누락된 등록 양식: {missing}"


def test_block_missing_returns_none():
    """블록 없는 MD는 None (미등록 초안) — 오류가 아니라 건너뜀."""
    assert bft.extract_config_block("# form_99\n본문만 있음\n", "form_99.md") is None


def test_broken_block_raises():
    """블록이 있는데 JSON이 깨지면 BuildError (정본 손상은 시끄럽게)."""
    bad = "## [config] x\n\n```json\n{ not valid json }\n```\n"
    with pytest.raises(bft.BuildError):
        bft.extract_config_block(bad, "form_bad.md")


def test_replace_config_block_swaps_json_keeps_prose():
    """채팅→블록 경로의 핵심: 블록 JSON만 교체하고 산문·헤더는 보존."""
    md = ("# t\n\n## 식별 패턴\nABC\n\n## [config] 실행 설정\n\n"
          "```json\n{\n  \"label\": \"old\"\n}\n```\n\n끝줄\n")
    new = bft.replace_config_block(md, {"label": "new", "x": 1}, "t.md")
    assert bft.extract_config_block(new, "t.md") == {"label": "new", "x": 1}
    assert "## 식별 패턴" in new and "ABC" in new and "끝줄" in new  # 산문 보존
    assert "## [config] 실행 설정" in new  # 헤더 보존


def test_replace_config_block_no_block_raises():
    with pytest.raises(bft.BuildError):
        bft.replace_config_block("# t\n블록 없음\n", {"a": 1}, "t.md")
