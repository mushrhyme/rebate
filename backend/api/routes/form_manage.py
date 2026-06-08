"""Form 정의 업데이트 채팅 — Claude API 연동."""
from __future__ import annotations

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


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str
    image_b64: str | None = None
    image_mime: str | None = None


class ChatRequest(BaseModel):
    form_id: str
    messages: list[ChatMessage]
    expected_hash: str | None = None  # 낙관적 잠금용


def _to_claude_messages(messages: list[ChatMessage]) -> list[dict]:
    result = []
    for msg in messages:
        if msg.role == "user" and msg.image_b64:
            content: list[dict] | str = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": msg.image_mime or "image/png",
                        "data": msg.image_b64,
                    },
                },
                {"type": "text", "text": msg.content or "(이미지 첨부)"},
            ]
        else:
            content = msg.content
        result.append({"role": msg.role, "content": content})
    return result


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

            tbd_count = len(re.findall(r"\bTBD\b", updated))
            yield f"data: {json.dumps({'type': 'done', 'tbd_count': tbd_count, 'content_hash': new_hash})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
