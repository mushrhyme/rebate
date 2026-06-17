"""replace_config_block 라운드트립 테스트 — dsl_apply의 정본(MD 블록) 갱신 경로.

dsl_apply가 form_types.json을 직접 쓰던 것을 'MD [config] 블록 갱신 + 재빌드'로
바꿨다(literate-config 단일소스 정합). 그 블록 쓰기 헬퍼가 산문을 보존하고
정확한 span만 치환하는지 고정한다.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from scripts.build_form_types import (  # noqa: E402
    extract_config_block, replace_config_block, render_block, BuildError,
)

_MD = """# form_99 — 테스트

설명 산문(앞).

## [config] 실행 설정 (정본)

> 이 블록이 정본.

```json
{
  "label": "테스트",
  "product_aggregate": { "strategy": "subset_subtract", "base_condition": "定番条件" }
}
```

설명 산문(뒤) — 보존돼야 함.
"""


def test_roundtrip_replace_then_extract():
    block = extract_config_block(_MD, "form_99.md")
    block["product_aggregate"]["base_condition"] = "新基準条件"
    new_md = replace_config_block(_MD, block, "form_99.md")
    # 재추출 == 수정본
    assert extract_config_block(new_md, "form_99.md") == block
    assert extract_config_block(new_md, "form_99.md")["product_aggregate"]["base_condition"] == "新基準条件"


def test_prose_preserved():
    block = extract_config_block(_MD, "form_99.md")
    new_md = replace_config_block(_MD, block, "form_99.md")
    assert "설명 산문(앞)." in new_md
    assert "설명 산문(뒤) — 보존돼야 함." in new_md
    assert "> 이 블록이 정본." in new_md


def test_block_format_matches_build_serialization():
    """블록 직렬화 규약(ensure_ascii=False, indent=2)이 build serialize와 동일."""
    obj = {"label": "農心", "x": 1}
    assert render_block(obj) == '{\n  "label": "農心",\n  "x": 1\n}'


def test_no_block_raises():
    try:
        replace_config_block("# 블록 없는 MD\n본문뿐.", {"a": 1}, "form_99.md")
    except BuildError:
        return
    raise AssertionError("블록 없는 MD에서 BuildError가 발생하지 않음")
