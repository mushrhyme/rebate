"""양식 정의 CRUD — form_definitions/ MD 파일 기반."""
import asyncio
import difflib
import hashlib
import re
from pathlib import Path

import anthropic as _anthropic

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...core.auth import get_current_user
from ...core.config import get_settings
from ...core.s3_store import read_json, write_json

router = APIRouter(prefix="/api/v3/forms", tags=["forms"])

_TBD_RE = re.compile(r"\bTBD\b")


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _list_forms(settings) -> list[dict]:
    forms_dir = settings.form_definitions_dir
    forms = []
    for md_file in sorted(forms_dir.glob("form_*.md")):
        if md_file.name.startswith("form_template"):
            continue
        content = md_file.read_text(encoding="utf-8")
        form_id = md_file.stem
        name_match = re.search(r"^#\s+(.+)", content, re.MULTILINE)
        name = name_match.group(1).strip() if name_match else form_id
        abbr_match = re.search(r"^-\s+\*\*약칭\*\*:\s*(.+)", content, re.MULTILINE)
        abbr = abbr_match.group(1).strip() if abbr_match else None
        num_match = re.search(r"form_(\d+)", form_id)
        num = str(int(num_match.group(1))) if num_match else form_id
        short_name = f"{num}_{abbr}" if abbr else form_id
        forms.append({
            "form_id": form_id,
            "name": name,
            "short_name": short_name,
            "tbd_count": len(_TBD_RE.findall(content)),
            "last_editor": None,
            "last_edited_at": None,
        })
    return forms


def _get_form_edit_log(form_id: str) -> list[dict]:
    return read_json(f"config/form_edit_logs/{form_id}.json") or []


def _append_form_edit_log(form_id: str, entry: dict) -> None:
    log = _get_form_edit_log(form_id)
    log.insert(0, entry)  # 최신 먼저
    write_json(f"config/form_edit_logs/{form_id}.json", log[:50])  # 최대 50개 보존


@router.get("")
async def list_forms(user: dict = Depends(get_current_user)):
    settings = get_settings()
    forms = _list_forms(settings)
    for form in forms:
        log = _get_form_edit_log(form["form_id"])
        if log:
            latest = log[0]
            form["last_editor"] = latest.get("display_name")
            form["last_edited_at"] = latest.get("saved_at")
    return forms


@router.get("/{form_id}/history")
async def get_form_history(
    form_id: str, limit: int = 10, user: dict = Depends(get_current_user)
):
    log = _get_form_edit_log(form_id)[:limit]
    result = []
    for entry in log:
        before = entry.get("content_before", "")
        after = entry.get("content_after", "")
        diff_lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=2))
        result.append({
            "id": entry.get("id", ""),
            "display_name": entry.get("display_name"),
            "saved_at": entry.get("saved_at"),
            "content_hash": entry.get("content_hash"),
            "diff": "\n".join(diff_lines[2:]),
        })
    return result


@router.get("/{form_id}")
async def get_form(form_id: str, user: dict = Depends(get_current_user)):
    settings = get_settings()
    path = settings.form_definitions_dir / f"{form_id}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="양식 없음")
    content = path.read_text(encoding="utf-8")
    return {
        "form_id": form_id,
        "content": content,
        "content_hash": _content_hash(content),
    }


class CreateFormBody(BaseModel):
    short_name: str
    memo: str = ""
    net_formula: str = ""
    cf_keywords: str = ""


@router.post("")
async def create_form(body: CreateFormBody, user: dict = Depends(get_current_user)):
    settings = get_settings()
    existing = sorted(settings.form_definitions_dir.glob("form_[0-9]*.md"))
    nums = [int(re.search(r"form_(\d+)", p.stem).group(1)) for p in existing if re.search(r"form_(\d+)", p.stem)]
    next_num = max(nums) + 1 if nums else 1
    form_id = f"form_{next_num:02d}"

    template_path = settings.form_definitions_dir / "form_template.md"
    template = template_path.read_text(encoding="utf-8") if template_path.exists() else ""

    content = (
        template
        .replace("{{form_id}}", form_id)
        .replace("{{short_name}}", body.short_name)
        .replace("{{memo}}", body.memo or "")
        .replace("{{net_formula}}", body.net_formula or "TBD")
        .replace("{{cf_keywords}}", body.cf_keywords or "TBD")
    ) if template else (
        f"# {form_id} — {body.short_name}\n\n"
        f"- 약칭: {body.short_name}\n"
        f"- 메모: {body.memo}\n"
    )

    out_path = settings.form_definitions_dir / f"{form_id}.md"
    out_path.write_text(content, encoding="utf-8")
    return {"form_id": form_id, "content": content, "content_hash": _content_hash(content)}


class ColdStartBody(BaseModel):
    short_name: str
    memo: str = ""
    page_images: list[str]  # base64 JPEG, 선택된 페이지
    form_num: int | None = None  # None이면 자동 배정


@router.post("/cold-start")
async def cold_start_analyze(body: ColdStartBody, user: dict = Depends(get_current_user)):
    settings = get_settings()
    existing = sorted(settings.form_definitions_dir.glob("form_[0-9]*.md"))
    nums = [int(re.search(r"form_(\d+)", p.stem).group(1)) for p in existing if re.search(r"form_(\d+)", p.stem)]

    if body.form_num is not None:
        form_id = f"form_{body.form_num:02d}"
        if (settings.form_definitions_dir / f"{form_id}.md").exists():
            raise HTTPException(status_code=409, detail=f"{form_id}는 이미 존재합니다.")
    else:
        next_num = max(nums) + 1 if nums else 1
        form_id = f"form_{next_num:02d}"

    template_path = settings.form_definitions_dir / "form_template.md"
    template = template_path.read_text(encoding="utf-8") if template_path.exists() else ""
    initial = (
        template
        .replace("{{form_id}}", form_id)
        .replace("{{short_name}}", body.short_name)
        .replace("{{memo}}", body.memo or "")
        .replace("{{net_formula}}", "TBD")
        .replace("{{cf_keywords}}", "TBD")
    )

    content_blocks: list[dict] = []
    for i, img_b64 in enumerate(body.page_images):
        content_blocks.append({"type": "text", "text": f"[선택된 페이지 {i + 1}]"})
        content_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
        })
    content_blocks.append({
        "type": "text",
        "text": (
            f'위 이미지들은 신규 청구서 양식 "{body.short_name}"의 대표 페이지들입니다.\n\n'
            "아래 form 정의 템플릿을 기반으로, 이미지에서 확인 가능한 항목을 채워 완성된 MD 파일을 작성해주세요.\n\n"
            "**작성 규칙:**\n"
            "1. 이미지에서 직접 확인 가능한 항목(컬럼명, 계층 구조, 페이지 역할, 합계 키, 식별 패턴 등)은 정확하게 채웁니다.\n"
            "2. 업무규칙이 필요하거나 이미지에서 확인 불가능한 항목(タイプ 분류, NET 계산식, データソース 등)은 `TBD`로 표시합니다.\n"
            "3. 일본어 컬럼명은 이미지에서 정확히 읽어 원문 그대로 사용합니다.\n"
            "4. 출력은 마크다운 코드블록(```) 없이 MD 파일 내용만 출력합니다. 다른 설명 없이 MD 내용만.\n\n"
            "---\n[템플릿 — 기본 정보 치환 완료]\n"
            f"{initial}"
        ),
    })

    client = _anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def _call() -> str:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            messages=[{"role": "user", "content": content_blocks}],
        )
        return resp.content[0].text.strip()

    generated = await asyncio.to_thread(_call)
    out_path = settings.form_definitions_dir / f"{form_id}.md"
    out_path.write_text(generated, encoding="utf-8")

    return {"form_id": form_id, "content": generated, "content_hash": _content_hash(generated)}


@router.post("/{form_id}/sync")
async def sync_form_config(form_id: str, user: dict = Depends(get_current_user)):
    """form_XX.md → config/form_types.json 동기화 (Claude 파싱)."""
    import json as _json

    settings = get_settings()
    md_path = settings.form_definitions_dir / f"{form_id}.md"
    if not md_path.exists():
        raise HTTPException(status_code=404, detail="양식 없음")
    md_content = md_path.read_text(encoding="utf-8")

    form_types_path = settings.workspace_root / "config" / "form_types.json"
    form_types: dict = _json.loads(form_types_path.read_text(encoding="utf-8")) if form_types_path.exists() else {}
    current_entry = form_types.get(form_id, {})

    # sync-form-config SKILL.md를 파싱 규칙의 단일 소스로 사용
    skill_path = settings.workspace_root / ".claude" / "skills" / "sync-form-config" / "SKILL.md"
    skill_content = skill_path.read_text(encoding="utf-8") if skill_path.exists() else ""

    prompt = f"""아래 sync-form-config 파싱 규칙(Step 2 전체)에 따라 form 정의 MD를 분석하고,
form_types.json의 해당 항목을 JSON으로만 반환하세요.
JSON만 출력하세요 (마크다운 코드블록 없이, 설명 없이, 오직 JSON 객체만).
Step 3(파일 저장)과 Step 4(변경 내역 보고)는 백엔드가 처리하므로 생략합니다.
파싱할 수 없는 항목(⚠️)은 아래 [현재 form_types.json 항목]의 기존 값을 그대로 유지하세요.

## sync-form-config 파싱 규칙
{skill_content}

## 현재 form_types.json 항목 (참고용 — 파싱 불가 항목은 이 값 유지)
{_json.dumps(current_entry, ensure_ascii=False, indent=2)}

## {form_id}.md
{md_content}"""

    client = _anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def _call() -> str:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()

    raw = await asyncio.to_thread(_call)

    # JSON 파싱 (코드블록 래핑 방어)
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    try:
        new_entry = _json.loads(raw)
    except Exception:
        raise HTTPException(status_code=500, detail=f"Claude 응답 파싱 실패: {raw[:200]}")

    # 변경 필드 감지
    changes = [k for k in new_entry if new_entry.get(k) != current_entry.get(k)]

    form_types[form_id] = new_entry
    form_types_path.write_text(
        _json.dumps(form_types, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {"ok": True, "form_id": form_id, "changes": changes}


class PatchFormBody(BaseModel):
    content: str


@router.patch("/{form_id}")
async def update_form(form_id: str, body: PatchFormBody, user: dict = Depends(get_current_user)):
    settings = get_settings()
    path = settings.form_definitions_dir / f"{form_id}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="양식 없음")
    path.write_text(body.content, encoding="utf-8")
    return {"ok": True}


class DeleteFormBody(BaseModel):
    password: str


@router.delete("/{form_id}")
async def delete_form(form_id: str, body: DeleteFormBody, user: dict = Depends(get_current_user)):
    import json as _json
    settings = get_settings()

    if not settings.admin_delete_password:
        raise HTTPException(status_code=403, detail="ADMIN_DELETE_PASSWORD 환경변수가 설정되지 않았습니다.")
    if body.password != settings.admin_delete_password:
        raise HTTPException(status_code=403, detail="관리자 비밀번호가 올바르지 않습니다.")

    path = settings.form_definitions_dir / f"{form_id}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="양식 없음")

    path.unlink()

    form_types_path = settings.workspace_root / "config" / "form_types.json"
    if form_types_path.exists():
        form_types = _json.loads(form_types_path.read_text(encoding="utf-8"))
        if form_id in form_types:
            del form_types[form_id]
            form_types_path.write_text(
                _json.dumps(form_types, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    return {"ok": True, "form_id": form_id}
