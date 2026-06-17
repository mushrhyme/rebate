"""양식 정의 CRUD — form_definitions/ MD 파일 기반."""
import asyncio
import difflib
import hashlib
import logging
import re
from pathlib import Path

import anthropic as _anthropic

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...core.auth import get_current_user
from ...core.config import get_settings
from ...core.s3_store import read_json, write_json

router = APIRouter(prefix="/api/v3/forms", tags=["forms"])
log = logging.getLogger(__name__)

_TBD_RE = re.compile(r"\bTBD\b")

# form_types.json read-merge-write 직렬화 — 동시 sync 시 늦게 끝난 쪽이
# 먼저 끝난 쪽의 변경을 통째로 덮어쓰는 lost-update 방지
_form_types_lock = asyncio.Lock()

_SYNC_STATUS_KEY = "config/form_sync_status.json"
# net 수식·교차검증이 바뀌면 현업 검산이 필요 — UI 경고용 키 목록
_FORMULA_KEYS = ("net", "cross_validation", "preprocess")


def _compute_formula_impact(form_id: str, old_entry: dict, new_entry: dict) -> dict:
    """수식 변경의 영향을 골든 번들로 재계산해 가시화 (차단 아님).

    tests/fixtures/regression/<form_id>/ 번들(입력+마스터 박제, Sheets 독립)로
    변경 전/후 NET을 동일 입력에 대해 재계산하고 변동 행수·금액 차이를 반환한다.
    번들이 없거나 계산 실패 시 available=False — sync 자체는 절대 막지 않는다.

    반환: {available, rows_total, rows_changed, net_before, net_after, net_delta, samples}
          또는 {available: False, reason}
    """
    settings = get_settings()
    bundle = settings.workspace_root / "tests" / "fixtures" / "regression" / form_id
    extracted = bundle / "extracted"
    if not extracted.is_dir():
        return {"available": False, "reason": "골든 번들 없음 (gen_regression_fixture.py로 박제 필요)"}
    try:
        doc_id = next(d.name for d in extracted.iterdir() if d.is_dir())
    except StopIteration:
        return {"available": False, "reason": "번들에 doc 없음"}

    try:
        import sys
        if str(settings.workspace_root) not in sys.path:
            sys.path.insert(0, str(settings.workspace_root))
        import scripts.phase4_calc as pc

        saved_store = pc._sheets_store
        saved_entry = pc.FORM_TYPES.get(form_id)
        pc._sheets_store = None  # 번들의 로컬 마스터 강제 사용 (Sheets 독립)
        try:
            pc.FORM_TYPES[form_id] = old_entry
            before, _ = pc.run(doc_id, save=False, base_dir=str(bundle))
            pc.FORM_TYPES[form_id] = new_entry
            after, _ = pc.run(doc_id, save=False, base_dir=str(bundle))
        finally:
            if saved_entry is not None:
                pc.FORM_TYPES[form_id] = saved_entry
            else:
                pc.FORM_TYPES.pop(form_id, None)
            pc._sheets_store = saved_store
    except Exception as e:
        log.warning("[sync] %s 영향 재계산 실패 — 가시화 생략: %s", form_id, e, exc_info=True)
        return {"available": False, "reason": f"재계산 실패: {type(e).__name__}"}

    # 수식 변경은 행 수를 바꾸지 않으므로(행은 items가 결정) 위치 정렬 diff가 유효
    def _net_sum(rows):
        return round(sum(r["NET"] for r in rows if r.get("NET") is not None), 2)

    samples = []
    rows_changed = 0
    for b, a in zip(before, after):
        if b.get("NET") != a.get("NET"):
            rows_changed += 1
            if len(samples) < 5:
                samples.append({
                    "product": a.get("商品名") or a.get("product_ocr") or "",
                    "net_before": b.get("NET"),
                    "net_after": a.get("NET"),
                })
    return {
        "available": True,
        "doc_id": doc_id,
        "rows_total": len(after),
        "rows_changed": rows_changed,
        "net_before": _net_sum(before),
        "net_after": _net_sum(after),
        "net_delta": round(_net_sum(after) - _net_sum(before), 2),
        "samples": samples,
    }


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _mirror_to_s3(key: str, text: str) -> None:
    """런타임 변경 파일을 S3에 미러 (best-effort).

    EC2에서 자란 form_types.json·form MD가 다음 배포의 로컬 구버전에
    덮이지 않도록, 배포 가드(scripts/deploy_backend.sh)가 이 미러를 기준으로
    로컬과 비교한다. 재해 복구용 사본 역할도 겸한다."""
    try:
        from ...core.s3_store import write_text
        write_text(key, text)
    except Exception:
        log.warning("S3 미러 기록 실패: %s", key, exc_info=True)


def mirror_form_md(form_id: str, content: str) -> None:
    """form MD 저장 직후 호출 — S3 미러 갱신."""
    _mirror_to_s3(f"config/form_definitions/{form_id}.md", content)


def _update_sync_status(form_id: str, entry: dict) -> None:
    """양식별 마지막 sync 결과 기록 (현업 UI 표시용 — 실패가 묻히지 않게)."""
    try:
        status = read_json(_SYNC_STATUS_KEY) or {}
        status[form_id] = entry
        write_json(_SYNC_STATUS_KEY, status)
    except Exception:
        log.warning("sync status 기록 실패: %s", form_id, exc_info=True)


def schedule_auto_sync(form_id: str) -> None:
    """MD 저장 직후 form_types.json 자동 동기화를 백그라운드로 실행.

    실패해도 MD 저장 자체는 유효 — form_types.json은 기존값을 유지하고
    로그로 수동 sync 필요를 알린다."""
    async def _run():
        try:
            res = await run_form_sync(form_id)
            log.info("[auto-sync] %s 동기화 완료 — 변경 필드: %s", form_id, res.get("changes"))
        except Exception:
            log.exception(
                "[auto-sync] %s 동기화 실패 — form_types.json은 기존값 유지. "
                "수동 sync(/api/v3/forms/%s/sync) 필요", form_id, form_id,
            )
    asyncio.create_task(_run())


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
    sync_status = read_json(_SYNC_STATUS_KEY) or {}
    for form in forms:
        edit_log = _get_form_edit_log(form["form_id"])
        if edit_log:
            latest = edit_log[0]
            form["last_editor"] = latest.get("display_name")
            form["last_edited_at"] = latest.get("saved_at")
        form["sync_status"] = sync_status.get(form["form_id"])
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
    await asyncio.to_thread(mirror_form_md, form_id, generated)
    schedule_auto_sync(form_id)

    return {"form_id": form_id, "content": generated, "content_hash": _content_hash(generated)}


def _validate_form_types_schema(form_types: dict, settings) -> None:
    """병합 결과를 config/form_types.schema.json으로 검증. 위반 시 ValueError."""
    import json as _json
    schema_path = settings.workspace_root / "config" / "form_types.schema.json"
    if not schema_path.exists():
        return
    try:
        import jsonschema
    except ImportError:
        log.warning("jsonschema 미설치 — sync 결과 스키마 검증 생략")
        return
    try:
        jsonschema.validate(form_types, _json.loads(schema_path.read_text(encoding="utf-8")))
    except jsonschema.ValidationError as e:
        raise ValueError(f"sync 결과가 form_types 스키마 위반 — 기존 설정 유지: {e.message}") from e


def _extract_config_block(md_content: str, form_id: str):
    """form_XX.md의 `[config]` 정본 블록(dict) 추출. 없으면 None (미마이그레이션 양식).

    Literate config 단일 진실 소스. 추출 로직은 scripts/build_form_types.py를 단일 출처로 재사용해
    빌드 가드(tests/unit/test_literate_config_guard)와 런타임 sync가 동일 규칙을 쓰게 한다.
    블록이 있는데 JSON이 깨졌으면 BuildError를 던진다(정본 손상은 시끄럽게).
    설계: docs/literate-config-migration.md
    """
    import sys
    settings = get_settings()
    root = str(settings.workspace_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    from scripts.build_form_types import extract_config_block
    return extract_config_block(md_content, f"{form_id}.md")


# ── 자동 [config] 블록 (UI 동기화로 산문→블록 생성, 개발자 손 안 타게) ──────────
# 손으로 만든 정본 블록(form_01/04)과 구분: 자동 블록은 마커 주석을 달고 prose-sha로
# 산문 변경을 감지한다. 산문이 바뀌면 재생성, 손 블록은 절대 산문에서 덮어쓰지 않는다.

def _prose_only(md_content: str) -> str:
    """[config] 섹션을 제거한 산문 부분 (prose-sha·블록 재작성 공통 기준)."""
    head = md_content.split("## [config]", 1)[0].rstrip()
    if head.endswith("---"):
        head = head[:-3].rstrip()
    return head


def _prose_sha(md_content: str) -> str:
    """산문(블록 제외)의 해시 — 자동 블록이 최신 산문에서 나온 것인지 판정."""
    return hashlib.sha256(_prose_only(md_content).encode("utf-8")).hexdigest()[:16]


def _block_is_auto(md_content: str) -> tuple[bool, str | None]:
    """[config] 섹션이 자동 생성 블록인지 + 기록된 prose-sha. (손 블록이면 (False, None))"""
    idx = md_content.find("## [config]")
    if idx == -1:
        return False, None
    section = md_content[idx:]
    if "config-block: auto" not in section:
        return False, None
    m = re.search(r"prose-sha:\s*([0-9a-f]+)", section)
    return True, (m.group(1) if m else None)


def _write_auto_block(md_content: str, entry: dict, prose_sha: str) -> str:
    """산문은 그대로 두고, 끝에 자동 [config] 블록(마커·prose-sha 포함)을 기록한 새 MD 반환."""
    import json as _json
    block = _json.dumps(entry, ensure_ascii=False, indent=2)
    prose = _prose_only(md_content)
    section = (
        "\n\n---\n\n"
        "## [config] 실행 설정 (자동 생성 — 산문에서 빌드, 직접 편집 금지)\n\n"
        f"<!-- config-block: auto; prose-sha: {prose_sha} -->\n"
        "> 이 블록은 동기화 시 산문에서 자동 생성됩니다. 직접 고치지 말고 위 산문을 수정한 뒤 동기화하세요.\n\n"
        f"```json\n{block}\n```\n"
    )
    return prose + section


async def _claude_parse_md_to_entry(form_id: str, md_content: str, current_entry: dict, settings) -> dict:
    """레거시 폴백 — [config] 블록 없는 양식에서 Claude가 sync-form-config 규칙으로 산문→JSON 추론.

    literate config 마이그레이션 완료 양식은 _extract_config_block가 결정적으로 처리하므로
    이 경로를 타지 않는다. 미마이그레이션·신규(블록 미작성) 양식만 여기로 폴백한다.
    """
    import json as _json
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
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    try:
        return _json.loads(raw)
    except Exception:
        raise ValueError(f"Claude 응답 파싱 실패: {raw[:200]}")


# 런타임 post-sync 훅에서 무조건 안전한(가산적·결정적) safe 게이트만 자동수정한다.
# build_check/block(전역 재빌드)은 혼재 상태에서 블록 없는 항목을 떨굴 수 있어 자동적용 제외 → 보고만.
_WIRING_AUTOFIX_GATES = {"index"}


def _run_wiring_check(form_id: str) -> dict:
    """post-sync 와이어링 검증 훅 — 동기화 직후 gap을 결정적으로 점검.

    scripts/verify_form_wiring를 단일 출처로 재사용한다(스킬·CLI와 동일 게이트).
    safe(가산적) 등급만 자동수정 + S3 미러 반영, owner(현업)·dev(개발자 T3)는 보고만.
    회계 규칙·엔진 코드는 절대 자동 생성하지 않는다.
    """
    import sys
    import json as _json
    settings = get_settings()
    root = Path(str(settings.workspace_root)).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from scripts import verify_form_wiring as vfw
    except Exception as e:
        log.warning("[wiring] verify 모듈 로드 실패 — 검증 생략: %s", e)
        return {"available": False}

    # 스크립트의 기준 경로와 워크스페이스가 다르면(테스트 등) 실파일 오염 방지로 생략
    if vfw.BASE.resolve() != root:
        return {"available": False, "reason": "workspace != script base"}

    config_path = root / "config" / "form_types.json"
    data = _json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    cv_types = vfw.engine_cross_validation_types()
    findings = [vfw.check_global()] + vfw.verify(form_id, data, cv_types)

    safe_fixed: list[str] = []
    safe_pending: list[str] = []
    applied = set()
    for f in findings:
        if f.grade != "safe" or not f.fixer:
            continue
        if f.gate in _WIRING_AUTOFIX_GATES and f.fixer not in applied:
            try:
                safe_fixed.append(f.fixer())
                applied.add(f.fixer)
            except Exception as e:
                log.warning("[wiring] safe 수정 실패 (%s): %s", f.gate, e)
        else:
            safe_pending.append(f.message)

    if safe_fixed:
        try:
            _mirror_to_s3("config/form_types.json", config_path.read_text(encoding="utf-8"))
            idx = settings.form_definitions_dir / "_index.md"
            if idx.exists():
                _mirror_to_s3("config/form_definitions/_index.md", idx.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("[wiring] safe 수정 미러 실패: %s", e)

    owner = [f.message for f in findings if f.grade == "owner"]
    dev = [f.message for f in findings if f.grade == "dev"]
    result = {
        "available": True,
        "safe_fixed": safe_fixed,
        "safe_pending": safe_pending,
        "owner": owner,
        "dev": dev,
        "needs_attention": bool(owner or dev or safe_pending),
    }
    log.info("[wiring] %s — safe수정 %d, 보류 %d, 현업 %d, 개발 %d",
             form_id, len(safe_fixed), len(safe_pending), len(owner), len(dev))
    return result


async def run_form_sync(form_id: str) -> dict:
    """form_XX.md → config/form_types.json 동기화 (블록 우선 결정적 추출 + 스키마 검증 게이트).

    검증 실패 시 form_types.json을 변경하지 않고 예외를 던진다.
    성공·실패 모두 _update_sync_status로 기록한다 (현업 가시성).
    Raises:
        FileNotFoundError: 양식 MD 없음
        ValueError: Claude 응답 파싱 실패 또는 스키마 위반
    """
    try:
        return await _run_form_sync_inner(form_id)
    except Exception as e:
        await asyncio.to_thread(_update_sync_status, form_id, {
            "ok": False, "error": str(e)[:300], "synced_at": _now_iso(),
        })
        raise


async def _run_form_sync_inner(form_id: str) -> dict:
    import json as _json

    settings = get_settings()
    md_path = settings.form_definitions_dir / f"{form_id}.md"
    if not md_path.exists():
        raise FileNotFoundError(f"양식 없음: {form_id}")
    md_content = md_path.read_text(encoding="utf-8")

    form_types_path = settings.workspace_root / "config" / "form_types.json"
    form_types: dict = _json.loads(form_types_path.read_text(encoding="utf-8")) if form_types_path.exists() else {}
    current_entry = form_types.get(form_id, {})

    # Literate config — 블록 처리 정책:
    #  · 손 정본 블록(form_01/04 등, auto 마커 없음) → 산문 재파싱 금지, block-first (개발자 튜닝 보존).
    #  · 자동 블록(UI 동기화가 만든 것) → 산문이 정본. 산문이 바뀌면(prose-sha 불일치) 재생성.
    #  · 블록 없음(신규) → 산문 파싱 후 자동 블록 생성 → 개발자 손 안 타게.
    block_entry = _extract_config_block(md_content, form_id)
    is_auto, stored_sha = _block_is_auto(md_content)
    cur_prose_sha = _prose_sha(md_content)
    regenerate = block_entry is None or (is_auto and stored_sha != cur_prose_sha)

    if block_entry is not None and not regenerate:
        new_entry = block_entry
        generated_block = False
        log.info("[sync] %s [config] 블록 사용 — 결정적 추출 (LLM 생략)", form_id)
    else:
        new_entry = await _claude_parse_md_to_entry(form_id, md_content, current_entry, settings)
        generated_block = True
        log.info("[sync] %s 산문 파싱 → [config] 자동 블록 생성/갱신", form_id)

    # 병합·검증·쓰기만 직렬화 (Claude 폴백 시 호출은 위에서 이미 락 밖에서 끝남)
    async with _form_types_lock:
        # 락 안에서 최신본 re-read — 대기 중 다른 sync가 쓴 변경을 보존
        form_types = (
            _json.loads(form_types_path.read_text(encoding="utf-8"))
            if form_types_path.exists() else {}
        )
        fresh_current = form_types.get(form_id, {})

        # 개발자 관리 필드 보존 — MD에서 파생되지 않는 필드가 sync로 유실되지 않게 재부착.
        # recovery_cell_map: phase2_verify 결정적 복구의 셀 인덱스 (코드 하드코딩 금지 게이트)
        prev_row_anchor = fresh_current.get("row_anchor") or {}
        new_row_anchor = new_entry.get("row_anchor")
        if (
            isinstance(new_row_anchor, dict)
            and "recovery_cell_map" in prev_row_anchor
            and "recovery_cell_map" not in new_row_anchor
        ):
            new_row_anchor["recovery_cell_map"] = prev_row_anchor["recovery_cell_map"]
            log.info("[sync] %s row_anchor.recovery_cell_map 보존 (개발자 관리 필드)", form_id)

        changes = [k for k in new_entry if new_entry.get(k) != fresh_current.get(k)]

        # 검증 게이트 — 통과 시에만 교체, 실패 시 기존 form_types.json 유지
        merged = {**form_types, form_id: new_entry}
        _validate_form_types_schema(merged, settings)

        form_types[form_id] = new_entry
        # 키를 form_id 정렬로 직렬화 — build_form_types.py(파일명 정렬)와 동일 순서를 보장해
        # 동기화 결과가 build --check(가드·배포·CI)와 어긋나지 않게 한다.
        text = _json.dumps(dict(sorted(form_types.items())), ensure_ascii=False, indent=2)
        form_types_path.write_text(text, encoding="utf-8")
        await asyncio.to_thread(_mirror_to_s3, "config/form_types.json", text)

        # 산문에서 (재)생성한 경우: 파싱·보존이 끝난 최종 구조를 [config] 자동 블록으로
        # MD에 기록(정본화) — 다음 동기화부터는 결정적 경로. 개발자가 파일을 손대지 않는다.
        if generated_block:
            new_md = _write_auto_block(md_content, new_entry, cur_prose_sha)
            md_path.write_text(new_md, encoding="utf-8")
            await asyncio.to_thread(mirror_form_md, form_id, new_md)
            log.info("[sync] %s [config] 자동 블록 기록 (prose-sha=%s)", form_id, cur_prose_sha)

    formula_changed = any(k in changes for k in _FORMULA_KEYS)

    # 수식이 바뀌었으면 골든 번들로 영향(변동 행수·금액)을 재계산해 가시화 (차단 아님).
    impact: dict | None = None
    if formula_changed:
        impact = await asyncio.to_thread(
            _compute_formula_impact, form_id, fresh_current, new_entry
        )
        if impact.get("available"):
            log.info(
                "[sync] %s 수식 영향 — %d/%d행 변동, NET %s→%s (Δ%s)",
                form_id, impact["rows_changed"], impact["rows_total"],
                impact["net_before"], impact["net_after"], impact["net_delta"],
            )

    # post-sync 와이어링 훅 — "MD만 생기고 엔진엔 안 붙은" gap 자동 점검.
    # safe(가산적)는 자동수정, owner(현업)·dev(개발자 T3)는 보고만. 검증 실패가 sync를 막지 않는다.
    try:
        wiring = await asyncio.to_thread(_run_wiring_check, form_id)
    except Exception as e:
        log.warning("[wiring] %s 검증 훅 실패(무시): %s", form_id, e)
        wiring = {"available": False}

    await asyncio.to_thread(_update_sync_status, form_id, {
        "ok": True,
        "changes": changes,
        "formula_changed": formula_changed,  # 수식·검증 규칙 변경 → 현업 검산 필요 경고
        "impact": impact,                    # 골든 재계산 영향 (가시화 — None=수식 무변경)
        "wiring": wiring,                     # post-sync 와이어링 점검 (현업/개발 할 일 가시화)
        "synced_at": _now_iso(),
        "md_hash": _content_hash(md_content),
        "error": None,
    })

    return {
        "ok": True, "form_id": form_id, "changes": changes,
        "formula_changed": formula_changed, "impact": impact, "wiring": wiring,
    }


@router.post("/{form_id}/sync")
async def sync_form_config(form_id: str, user: dict = Depends(get_current_user)):
    """form_XX.md → config/form_types.json 동기화 (Claude 파싱)."""
    try:
        return await run_form_sync(form_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="양식 없음")
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 미리보기/커밋 (Form 관리: md 수정 → 반영 전 샘플 재계산 확인) ───────────────
async def _md_to_config(form_id: str) -> tuple[dict, str]:
    """form_XX.md → Claude 파싱 → config 항목(dict). 쓰지 않는다 (md_content 동반)."""
    import json as _json
    settings = get_settings()
    md_path = settings.form_definitions_dir / f"{form_id}.md"
    if not md_path.exists():
        raise FileNotFoundError(f"양식 없음: {form_id}")
    md_content = md_path.read_text(encoding="utf-8")
    ft_path = settings.workspace_root / "config" / "form_types.json"
    form_types = _json.loads(ft_path.read_text(encoding="utf-8")) if ft_path.exists() else {}
    current_entry = form_types.get(form_id, {})

    # Literate config: 손 블록·최신 자동 블록이면 결정적 추출, 산문 변경된 자동 블록·신규는 재파싱.
    block_entry = _extract_config_block(md_content, form_id)
    is_auto, stored_sha = _block_is_auto(md_content)
    regenerate = block_entry is None or (is_auto and stored_sha != _prose_sha(md_content))
    if block_entry is not None and not regenerate:
        return block_entry, md_content
    new_entry = await _claude_parse_md_to_entry(form_id, md_content, current_entry, settings)
    return new_entry, md_content


def _recompute_sample(form_id: str, entry: dict, doc_id: str) -> dict:
    """주어진 config(entry)로 샘플 doc를 재계산(쓰기 없음) → phase4 payload."""
    import sys
    settings = get_settings()
    if str(settings.workspace_root) not in sys.path:
        sys.path.insert(0, str(settings.workspace_root))
    import scripts.phase4_calc as pc
    saved = pc.FORM_TYPES.get(form_id)
    try:
        pc.FORM_TYPES[form_id] = entry
        payload = pc.run(doc_id, save=False, return_payload=True)
    finally:
        if saved is not None:
            pc.FORM_TYPES[form_id] = saved
        else:
            pc.FORM_TYPES.pop(form_id, None)
    return payload


def _config_changes(cur: dict, new: dict) -> list[dict]:
    keys = sorted(set(cur) | set(new))
    return [{"field": k, "from": cur.get(k), "to": new.get(k)} for k in keys if cur.get(k) != new.get(k)]


class PreviewBody(BaseModel):
    doc_id: str


@router.post("/{form_id}/preview")
async def preview_form_change(form_id: str, body: PreviewBody, user: dict = Depends(get_current_user)):
    """현행 md 기준 config를 (쓰지 않고) 만들어 샘플 doc 재계산 → 결과 미리보기."""
    import json as _json
    try:
        new_entry, _ = await _md_to_config(form_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="양식 없음")
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    settings = get_settings()
    ft_path = settings.workspace_root / "config" / "form_types.json"
    cur_entry = (_json.loads(ft_path.read_text(encoding="utf-8")).get(form_id, {})
                 if ft_path.exists() else {})
    try:
        result = await asyncio.to_thread(_recompute_sample, form_id, new_entry, body.doc_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"샘플 재계산 실패: {type(e).__name__}: {e}")
    return {"result": result, "config_changes": _config_changes(cur_entry, new_entry), "new_entry": new_entry}


class CommitBody(BaseModel):
    config: dict


@router.post("/{form_id}/commit")
async def commit_form_config(form_id: str, body: CommitBody, user: dict = Depends(get_current_user)):
    """미리보기에서 확인한 config를 form_types.json에 반영(동결). md 재파싱 없이 그대로 쓴다."""
    import json as _json
    settings = get_settings()
    ft_path = settings.workspace_root / "config" / "form_types.json"
    new_entry = body.config
    async with _form_types_lock:
        form_types = _json.loads(ft_path.read_text(encoding="utf-8")) if ft_path.exists() else {}
        fresh_current = form_types.get(form_id, {})
        prev_ra = fresh_current.get("row_anchor") or {}
        new_ra = new_entry.get("row_anchor")
        if isinstance(new_ra, dict) and "recovery_cell_map" in prev_ra and "recovery_cell_map" not in new_ra:
            new_ra["recovery_cell_map"] = prev_ra["recovery_cell_map"]
        changes = [k for k in new_entry if new_entry.get(k) != fresh_current.get(k)]
        merged = {**form_types, form_id: new_entry}
        try:
            _validate_form_types_schema(merged, settings)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"스키마 검증 실패: {e}")
        form_types[form_id] = new_entry
        # build_form_types.py와 동일한 form_id 정렬로 직렬화 (build --check 어긋남 방지)
        text = _json.dumps(dict(sorted(form_types.items())), ensure_ascii=False, indent=2)
        ft_path.write_text(text, encoding="utf-8")
        await asyncio.to_thread(_mirror_to_s3, "config/form_types.json", text)
    return {"ok": True, "form_id": form_id, "changes": changes,
            "message": "config 반영 완료 — 문서 재분석 시 적용됩니다."}


class PatchFormBody(BaseModel):
    content: str


@router.patch("/{form_id}")
async def update_form(form_id: str, body: PatchFormBody, user: dict = Depends(get_current_user)):
    settings = get_settings()
    path = settings.form_definitions_dir / f"{form_id}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="양식 없음")
    path.write_text(body.content, encoding="utf-8")
    await asyncio.to_thread(mirror_form_md, form_id, body.content)
    schedule_auto_sync(form_id)
    return {"ok": True, "auto_sync": "started"}


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
                _json.dumps(dict(sorted(form_types.items())), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    return {"ok": True, "form_id": form_id}
