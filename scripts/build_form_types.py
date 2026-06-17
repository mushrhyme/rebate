"""
build_form_types.py — form_definitions/form_XX.md의 [config] 정본 블록 → config/form_types.json

Literate config 단일 진실 소스의 빌드 단계.
정본 = 각 form_XX.md 안의 `## [config]` 섹션 첫 ```json 펜스.
이 스크립트는 그 블록들을 모아 결정적으로 config/form_types.json을 생성한다.

설계: docs/literate-config-migration.md

원칙:
- LLM 없음. JSON 파싱 + (선택) 스키마 검증뿐. 임의 코드 실행 없음.
- 블록 없음/JSON 깨짐/스키마 불일치 → 즉시 비0 종료 (숨은 기본값 없음).
- 출력은 현 form_types.json과 바이트 동일 직렬화 (ensure_ascii=False, indent=2, trailing newline 없음).

사용:
  python scripts/build_form_types.py            # config/form_types.json 갱신
  python scripts/build_form_types.py --check     # 빌드 결과가 현 파일과 동일한지만 검사 (CI 가드)
"""
import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).parent.parent
FORM_DEFS_DIR = BASE / "form_definitions"
OUTPUT_PATH = BASE / "config" / "form_types.json"
SCHEMA_PATH = BASE / "config" / "form_types.schema.json"

# form_NN.md 만 대상. form_template.md·form_XX.md(플레이스홀더)·_index.md 제외.
FORM_FILE_RE = re.compile(r"^form_\d{2}\.md$")

# `## [config]` 헤딩 이후 첫 ```json ... ``` 펜스 추출
_CONFIG_BLOCK_RE = re.compile(
    r"^\#\#\s*\[config\].*?\n.*?```json\s*\n(.*?)\n```",
    re.DOTALL | re.MULTILINE,
)


class BuildError(Exception):
    pass


def extract_config_block(md_text: str, source: str):
    """form_XX.md 본문에서 [config] 정본 블록(dict)을 추출.

    블록이 아예 없으면 None (미등록 초안 — 빌드 대상에서 제외).
    블록이 있는데 JSON이 깨졌거나 객체가 아니면 BuildError (실집행 정본의 손상).
    """
    m = _CONFIG_BLOCK_RE.search(md_text)
    if not m:
        return None
    raw = m.group(1)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BuildError(f"{source}: [config] 블록 JSON 파싱 실패 — {e}")
    if not isinstance(obj, dict):
        raise BuildError(f"{source}: [config] 블록이 JSON 객체가 아닙니다.")
    return obj


def build_forms() -> dict:
    """form_definitions/의 모든 form_NN.md에서 설정 객체를 모아 form_types dict 생성."""
    if not FORM_DEFS_DIR.is_dir():
        raise BuildError(f"form_definitions 디렉터리 없음: {FORM_DEFS_DIR}")

    forms: dict = {}
    # 파일명 정렬 → 결정적 키 순서 (현 form_types.json과 동일: form_01, form_04, ...)
    for md_path in sorted(FORM_DEFS_DIR.glob("form_*.md")):
        if not FORM_FILE_RE.match(md_path.name):
            continue  # form_template.md, form_XX.md 등 제외
        form_id = md_path.stem  # "form_04"
        obj = extract_config_block(md_path.read_text(encoding="utf-8"), md_path.name)
        if obj is None:
            # [config] 블록 없는 양식 = 미등록 초안. 빌드 제외하되 알린다(조용한 누락 방지).
            print(f"[build_form_types] 건너뜀(미등록 초안, [config] 블록 없음): {md_path.name}", file=sys.stderr)
            continue
        forms[form_id] = obj

    if not forms:
        raise BuildError("빌드할 [config] 블록을 가진 form_NN.md가 없습니다.")
    return forms


def validate_schema(forms: dict) -> None:
    """form_types.schema.json으로 검증 (jsonschema 미설치 시 건너뜀 — 가드 테스트가 별도 검증)."""
    if not SCHEMA_PATH.exists():
        return
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        return
    from jsonschema import Draft7Validator

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(forms), key=lambda e: list(e.path))
    if errors:
        details = "\n".join(f"  - {'/'.join(map(str, e.path))}: {e.message}" for e in errors)
        raise BuildError(f"스키마 검증 실패:\n{details}")


def serialize(forms: dict) -> str:
    """현 config/form_types.json과 바이트 동일한 직렬화 (trailing newline 없음)."""
    return json.dumps(forms, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="빌드 결과가 현 config/form_types.json과 동일한지만 검사 (쓰지 않음)",
    )
    args = parser.parse_args()

    try:
        forms = build_forms()
        validate_schema(forms)
    except BuildError as e:
        print(f"[build_form_types] 오류: {e}", file=sys.stderr)
        return 1

    rendered = serialize(forms)

    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        if current != rendered:
            print(
                "[build_form_types] --check 실패: form_XX.md [config] 블록에서 빌드한 결과가 "
                "config/form_types.json과 다릅니다. `python scripts/build_form_types.py`로 재빌드하세요.",
                file=sys.stderr,
            )
            return 1
        print("[build_form_types] --check OK: 블록 ↔ form_types.json 동치")
        return 0

    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"[build_form_types] {len(forms)}개 양식 → {OUTPUT_PATH.relative_to(BASE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
