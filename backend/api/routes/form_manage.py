"""Form 정의 업데이트 채팅 — Claude API 연동."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import uuid as _uuid
from datetime import datetime, timezone

from ...core.auth import get_current_user
from ...core.config import get_settings
from ...core.s3_store import read_json, write_json


def _get_form_edit_log(form_id: str) -> list[dict]:
    return read_json(f"config/form_edit_logs/{form_id}.json") or []


def _append_form_edit_log(form_id: str, entry: dict) -> None:
    log = _get_form_edit_log(form_id)
    log.insert(0, entry)
    write_json(f"config/form_edit_logs/{form_id}.json", log[:50])


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]

router = APIRouter(prefix="/api/v3/form-manage", tags=["form-manage"])

_MODEL = "claude-sonnet-4-6"

_SYSTEM_TMPL = """\
당신은 form 정의 파일(form_XX.md)을 관리하는 전문가입니다.

현재 {form_id}.md 내용:
<form_content>
{form_content}
</form_content>

## 역할
사용자의 요청(이미지 또는 텍스트)을 분석해 form 정의 변경 제안을 작성합니다.

## 제안 포맷 (반드시 아래 형식으로 출력합니다)

## {form_id}.md 업데이트 제안

### 변경 사항 (N건)

#### 1. [섹션명]

현재:
(현재 섹션 내용 — 없으면 "(없음)")

변경 후:
(변경 후 섹션 내용)

---

저장하겠습니다. 수정할 내용이 있으면 말씀해 주세요.

## 규칙
- 이미지에서 읽지 못한 내용을 추론으로 채우지 않습니다. 불확실한 항목은 `(현업 확인 필요)`로 표기합니다.
- 변경 사항이 없으면 "변경 사항이 없습니다"라고 명확히 알립니다.
- 한국어로 답변합니다.\
"""

_APPLY_PROMPT = """\
아래 대화를 바탕으로 {form_id}.md 의 최종 업데이트된 전체 내용을 출력하세요.

현재 {form_id}.md 내용:
<current>
{current_content}
</current>

대화 내용:
<conversation>
{history}
</conversation>

지시사항:
- 대화에서 합의·승인된 변경 사항만 반영합니다.
- 언급되지 않은 섹션은 현재 내용 그대로 유지합니다.
- 파일 전체 내용만 출력하고, 설명이나 코드블록 래핑(```)은 추가하지 않습니다.\
"""


class ImageData(BaseModel):
    b64: str
    mime: str = "image/png"


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str
    image_b64: str | None = None   # legacy 단일 이미지
    image_mime: str | None = None  # legacy 단일 이미지
    images: list[ImageData] | None = None  # 다중 이미지


class ChatRequest(BaseModel):
    form_id: str
    messages: list[ChatMessage]
    expected_hash: str | None = None  # 낙관적 잠금용


def _to_claude_messages(messages: list[ChatMessage]) -> list[dict]:
    result = []
    for msg in messages:
        img_blocks: list[dict] = []
        if msg.images:
            img_blocks = [
                {"type": "image", "source": {"type": "base64", "media_type": img.mime, "data": img.b64}}
                for img in msg.images
            ]
        elif msg.image_b64:
            img_blocks = [
                {"type": "image", "source": {"type": "base64", "media_type": msg.image_mime or "image/png", "data": msg.image_b64}}
            ]

        if img_blocks:
            content: list[dict] | str = [
                *img_blocks,
                {"type": "text", "text": msg.content or "(이미지 첨부)"},
            ]
        else:
            content = msg.content
        result.append({"role": msg.role, "content": content})
    return result


_RULES_SYSTEM_TMPL = """\
당신은 form_XX.md의 [config] 실행 블록(JSON)을 편집하는 전문가입니다.
실행 규칙(NET 수식·교차검증·집계·출력)의 **정본은 이 블록**이며, 사람이 읽는 문장은 블록에서 자동 생성됩니다.

현재 {form_id} 블록:
<block>
{block_json}
</block>

## 출력 규칙
- 사용자 대화에서 **합의된 변경만** 반영해 **전체 블록 JSON 한 개**를 출력합니다.
- JSON 객체만 출력합니다 (마크다운 코드펜스·설명 없이).
- 회계 규칙을 임의로 추측하지 않습니다. 불확실하면 기존 값을 유지합니다.
- 아래 **허용 어휘 밖**(새 교차검증 종류·연산·집계 전략)이 필요하면 만들지 말고, 그 부분은 기존 값을 유지한 채
  변경하지 마세요. (그런 변경은 개발(T3)이 필요합니다.)

## 허용 어휘
- NET 수식 연산: 산술 `+` `-` `*` `/` 괄호, 비교 `<` `<=` `>` `>=` `==` `!=`, 논리 `and` `or` `not`,
  조건식 `A if 조건 else B`. 변수는 `shikiri`(仕切)·`teiban`(定番)·블록의 vars/computed_vars만.
  (함수 호출·외부 참조는 금지 — 결정적 수식만.) 예: `(c1 + c2) if c1 + c2 > 0 else fallback`
- 교차검증 type: {cv_types}
- 집계 전략: {strategies}
- 집계 방식(product_aggregate): `relationship`=`subset`(추가조건이 기준조건의 부분집합·차감 분해) 또는
  `independent`(조건들이 독립·차감 없이 나열). `group_by`로 묶음 기준 변경 가능(예: `["jisho","customer","product"]`).
  완전히 새로운 분해 알고리즘은 어휘 밖(개발 T3).\
"""


@router.post("/apply-rules")
async def apply_rules(body: ChatRequest, user: dict = Depends(get_current_user)):
    """채팅 자연어 → [config] 블록을 **직접** 갱신 (step 3, 산문→구조 재파싱 제거).

    Claude가 (현재 블록 + 허용 어휘 + 대화)로 새 블록 JSON을 만들고,
    forms.apply_block_update가 스키마 검증 후 블록 교체 + build로 json·문장 재생성한다.
    """
    settings = get_settings()
    form_path = settings.form_definitions_dir / f"{body.form_id}.md"
    if not form_path.exists():
        raise HTTPException(status_code=404, detail="양식 없음")

    # 현재 블록 + 엔진 허용 어휘 로드 (verify_form_wiring 단일 출처)
    import sys
    root = str(settings.workspace_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    from scripts.build_form_types import extract_config_block
    from scripts.verify_form_wiring import engine_cross_validation_types, AGGREGATE_STRATEGIES

    md_content = form_path.read_text(encoding="utf-8")
    cur_block = extract_config_block(md_content, f"{body.form_id}.md") or {}
    system = _RULES_SYSTEM_TMPL.format(
        form_id=body.form_id,
        block_json=json.dumps(cur_block, ensure_ascii=False, indent=2),
        cv_types=", ".join(sorted(engine_cross_validation_types())),
        strategies=", ".join(sorted(AGGREGATE_STRATEGIES)),
    )

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    resp = await client.messages.create(
        model=_MODEL, max_tokens=4096, system=system,
        messages=_to_claude_messages(body.messages),
    )
    # JSON 객체 추출 — Claude가 설명(예: "이 변경은 개발 필요")을 덧붙여도 본체 블록을 찾는다.
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw).strip().rstrip("`").strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise HTTPException(status_code=422, detail=f"블록 JSON을 찾지 못함: {raw[:200]}")
    note = raw[:m.start()].strip()   # JSON 앞 설명(어휘 밖 거부·개발 필요 안내 등)
    try:
        new_block = json.loads(m.group(0))
    except Exception:
        raise HTTPException(status_code=422, detail=f"블록 JSON 파싱 실패: {m.group(0)[:200]}")
    if not isinstance(new_block, dict):
        raise HTTPException(status_code=422, detail="블록이 JSON 객체가 아닙니다.")

    # 변경 없음(어휘 밖 요청을 Claude가 거부) → 파일 쓰지 않고 안내만 반환
    if new_block == cur_block:
        return {"ok": True, "form_id": body.form_id, "unchanged": True, "note": note, "wiring": None}

    from .forms import apply_block_update
    try:
        result = await asyncio.to_thread(apply_block_update, body.form_id, new_block)
    except ValueError as e:        # 스키마 위반
        raise HTTPException(status_code=400, detail=f"스키마 검증 실패(반영 안 함): {e}")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="양식 없음")
    return {**result, "note": note}


@router.post("/chat")
async def chat(body: ChatRequest, user: dict = Depends(get_current_user)):
    """변경 제안을 스트리밍으로 반환합니다."""
    settings = get_settings()
    form_path = settings.form_definitions_dir / f"{body.form_id}.md"
    if not form_path.exists():
        raise HTTPException(status_code=404, detail="양식 없음")

    form_content = form_path.read_text(encoding="utf-8")
    system = _SYSTEM_TMPL.format(form_id=body.form_id, form_content=form_content)
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def generate():
        try:
            async with client.messages.stream(
                model=_MODEL,
                max_tokens=4096,
                system=system,
                messages=_to_claude_messages(body.messages),
            ) as stream:
                async for text in stream.text_stream:
                    yield f"data: {json.dumps({'type': 'text', 'text': text}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/apply")
async def apply(body: ChatRequest, user: dict = Depends(get_current_user)):
    """대화에서 합의된 변경 사항을 form_XX.md 에 실제로 저장합니다 (스트리밍)."""
    settings = get_settings()
    form_path = settings.form_definitions_dir / f"{body.form_id}.md"
    if not form_path.exists():
        raise HTTPException(status_code=404, detail="양식 없음")

    current_content = form_path.read_text(encoding="utf-8")
    history = "\n\n".join(
        f"[{'사용자' if m.role == 'user' else 'Claude'}]: {m.content}"
        for m in body.messages
    )
    prompt = _APPLY_PROMPT.format(
        form_id=body.form_id,
        current_content=current_content,
        history=history,
    )

    # 충돌 감지 — 저장 전 파일 hash 검증
    if body.expected_hash:
        current_hash = _content_hash(current_content)
        if current_hash != body.expected_hash:
            log = _get_form_edit_log(body.form_id)
            if log:
                last = log[0]
                t = last.get("saved_at", "")[:16].replace("T", " ")
                detail = f"{last.get('display_name', '?')}님이 {t}에 수정했습니다. 최신 내용을 다시 불러온 뒤 저장해 주세요."
            else:
                detail = "파일이 외부에서 변경되었습니다. 최신 내용을 다시 불러온 뒤 저장해 주세요."
            raise HTTPException(status_code=409, detail=detail)

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def generate():
        try:
            full_text = ""
            async with client.messages.stream(
                model=_MODEL,
                max_tokens=8192,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    full_text += text
                    yield f"data: {json.dumps({'type': 'text', 'text': text}, ensure_ascii=False)}\n\n"

            updated = full_text.strip()
            if updated.startswith("```"):
                lines = updated.splitlines()
                end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
                updated = "\n".join(lines[1:end])

            # 정본 보호(step 3): 산문 편집은 [config] 블록을 절대 바꾸지 않는다.
            # Claude의 전체 재생성이 블록을 변형·누락했어도 원본 블록을 강제 복원한다.
            # 실행 규칙 변경은 /apply-rules(채팅→블록) 경로로만.
            try:
                import sys as _sys
                _root = str(settings.workspace_root)
                if _root not in _sys.path:
                    _sys.path.insert(0, _root)
                from scripts.build_form_types import (
                    extract_config_block as _xb, replace_config_block as _rb,
                )
                _old = _xb(current_content, f"{body.form_id}.md")
                if _old is not None:
                    if _xb(updated, f"{body.form_id}.md") is not None:
                        updated = _rb(updated, _old, f"{body.form_id}.md")
                    else:
                        updated = (updated.rstrip() + "\n\n---\n\n## [config] 실행 설정\n\n```json\n"
                                   + json.dumps(_old, ensure_ascii=False, indent=2) + "\n```\n")
            except Exception:
                pass

            form_path.with_suffix(".md.bak").write_text(current_content, encoding="utf-8")
            form_path.write_text(updated, encoding="utf-8")

            # 변경 이력 기록
            new_hash = _content_hash(updated)
            _append_form_edit_log(body.form_id, {
                "id": str(_uuid.uuid4()),
                "form_id": body.form_id,
                "user_id": user["user_id"],
                "display_name": user.get("display_name"),
                "content_hash": new_hash,
                "content_before": current_content,
                "content_after": updated,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            })

            # MD 저장 → S3 미러 (배포 가드 기준점).
            # config(form_types.json) 반영은 자동이 아니라 '미리보기 확인 후'로 게이트한다
            # (POST /forms/{id}/preview → /commit). 현업이 결과를 보고 승인해야 반영됨.
            from .forms import mirror_form_md
            await asyncio.to_thread(mirror_form_md, body.form_id, updated)

            tbd_count = len(re.findall(r"\bTBD\b", updated))
            yield f"data: {json.dumps({'type': 'done', 'tbd_count': tbd_count, 'content_hash': new_hash, 'auto_sync': 'gated'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
