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

    # 정본 블록 보장 — 템플릿에 [config] 골격이 있으면 그대로, 없으면(폴백 등) 최소 골격 부착.
    if _extract_config_block(content, form_id) is None:
        content = _append_skeleton_config_block(content, body.short_name)

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
            "4. `## [config]` 실행 설정 블록은 **그대로 두거나 생략**합니다(JSON을 추측해 채우지 마세요). "
            "실행 규칙은 이후 채팅 '규칙 반영'으로 확정합니다 — 백엔드가 최소 골격을 보장합니다.\n"
            "5. 출력은 마크다운 코드블록(```) 없이 MD 파일 내용만 출력합니다. 다른 설명 없이 MD 내용만.\n\n"
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
    # 정본 블록 보장(결정적) — cold-start는 산문을 만들고, 실행 정본 블록은 최소 골격으로 직접 부착한다.
    # sync는 block-first only(아래 _run_form_sync_inner) — 블록 없는 양식을 산문에서 자동 파싱하지 않는다.
    # 실제 NET·교차검증 규칙은 이후 '규칙 반영'(apply_block_update)으로 채운다.
    if _extract_config_block(generated, form_id) is None:
        generated = _append_skeleton_config_block(generated, body.short_name)
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


def _append_skeleton_config_block(md_content: str, label: str) -> str:
    """산문 MD 끝에 [config] 정본 블록 *최소 골격*을 부착한다 (cold-start/create 부트스트랩).

    블록 없는 양식이 sync에서 시끄럽게 실패하지 않도록, 모든 신규 양식이 블록을 갖고 태어나게 한다.
    NET=仕切는 자리표시 — 실제 규칙은 채팅 '규칙 반영'(apply_block_update)으로 확정한다.
    스키마 유효 최소집합(label + net expr)만 둔다.
    """
    import json as _json
    skeleton = {"label": label, "net": {"formula_type": "expr", "expr": "shikiri"}}
    block = _json.dumps(skeleton, ensure_ascii=False, indent=2)
    section = (
        "\n\n---\n\n"
        "## [config] 실행 설정 (정본 · build_form_types.py가 읽음)\n\n"
        "> 이 블록이 이 양식의 **유일한 실행 정본**이다. `config/form_types.json`은 여기서 빌드된 생성물.\n"
        "> 아래는 **최소 골격**(NET=仕切 자리표시) — 실제 규칙은 채팅 **\"규칙 반영\"**으로 채운다.\n\n"
        f"```json\n{block}\n```\n"
    )
    return md_content.rstrip() + section


# ── 자동 [config] 블록 경로 제거(정본-only, P3 완주) ──────────────────────────
# 산문→구조 LLM 추론(_claude_parse_md_to_entry)·auto 블록 기록(_write_auto_block)·
# prose-sha 드리프트 추적은 모두 제거됐다. 블록이 유일한 정본이며, 신규 양식의 첫 블록은
# cold-start/create가 골격으로 부착하고(위 _append_skeleton_config_block), 실제 규칙은
# 채팅 '규칙 반영'(apply_block_update)으로 채운다. 사람이 읽는 실행 규칙 섹션은
# build_form_types(scripts)가 빌드 시 단일 출처로 재렌더한다.


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


def apply_block_update(form_id: str, new_block: dict) -> dict:
    """[config] 블록을 새 구조로 교체하고 json·auto-rules를 재생성한다 (결정적, LLM 없음).

    채팅→블록 경로(step 3)의 커밋 단계. form_types.json을 직접 쓰지 않고 블록을
    정본으로 갱신한 뒤 build로 재생성한다(단일 소스). 스키마 검증 통과 시에만 파일을 쓴다.
    Raises: FileNotFoundError, ValueError(스키마 위반), BuildError.
    """
    import sys
    import json as _json
    settings = get_settings()
    root = Path(str(settings.workspace_root)).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    md_path = settings.form_definitions_dir / f"{form_id}.md"
    if not md_path.exists():
        raise FileNotFoundError(f"양식 없음: {form_id}")

    # 1) 스키마 검증 — 파일 쓰기 전에. 잘못된 블록은 디스크에 들어가지 않는다.
    ft_path = root / "config" / "form_types.json"
    form_types = _json.loads(ft_path.read_text(encoding="utf-8")) if ft_path.exists() else {}
    merged = {**form_types, form_id: new_block}
    _validate_form_types_schema(merged, settings)

    # 2) 블록만 교체(산문·헤더 불변) → 3) build로 json 재생성 + auto-rules 재렌더(단일 소스)
    from scripts.build_form_types import replace_config_block, main as build_main
    md = md_path.read_text(encoding="utf-8")
    md_path.write_text(replace_config_block(md, new_block, f"{form_id}.md"), encoding="utf-8")
    if build_main([]) != 0:
        raise RuntimeError("build_form_types 실패 — 블록 갱신 확인 필요")

    # 4) 미러(블록 포함 MD + json) → 5) 와이어링 점검
    final_md = md_path.read_text(encoding="utf-8")
    mirror_form_md(form_id, final_md)
    _mirror_to_s3("config/form_types.json", ft_path.read_text(encoding="utf-8"))
    wiring = _run_wiring_check(form_id)
    log.info("[block-update] %s 블록 갱신 완료", form_id)
    return {"ok": True, "form_id": form_id, "wiring": wiring}


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

    # 정본-only(P3 완주): [config] 블록이 *유일한* 정본. 동기화는 블록을 빌드만 한다.
    #  · 산문→구조 LLM 추론은 표준 경로에서 영구 제거 — 블록 없는 양식을 조용히 자동생성하지 않는다.
    #  · 블록 없으면 시끄럽게 실패 → 신규 양식의 첫 블록은 cold-start/create가 골격으로 부착,
    #    실제 규칙은 채팅 '규칙 반영'(apply_block_update)으로 채운다.
    #  · 무음 no-op·블록 타입 혼재(auto/blockless) 구조적 제거. 설계: docs/literate-config-migration.md
    block_entry = _extract_config_block(md_content, form_id)
    if block_entry is None:
        raise ValueError(
            f"{form_id}: [config] 정본 블록이 없습니다 — 동기화는 블록을 빌드만 합니다. "
            f"cold-start 또는 채팅 '규칙 반영'으로 블록을 먼저 만드세요."
        )
    new_entry = block_entry
    log.info("[sync] %s [config] 블록 정본 빌드 (block-first only)", form_id)

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
        # 산문→블록 자동기록 경로 제거(정본-only). 블록은 항상 이미 존재하므로 MD를 다시 쓰지 않는다.

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

    # 정본-only: 블록이 유일한 정본. 없으면 시끄럽게 실패(산문 파싱 fallback 없음).
    block_entry = _extract_config_block(md_content, form_id)
    if block_entry is None:
        raise ValueError(
            f"{form_id}: [config] 정본 블록 없음 — cold-start/채팅 '규칙 반영'으로 블록을 먼저 만드세요."
        )
    return block_entry, md_content


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
