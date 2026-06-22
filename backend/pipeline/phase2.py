"""Phase 2 — page MD → items[] JSON (Claude API, streaming)."""
import asyncio
import json
import logging
import os
import re
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

from ..core.config import get_settings
from ..db.queries import accumulate_token_usage
from ..tools.claude_retry import async_call_with_retry

_SYSTEM_PROMPT_CACHE: tuple[float, str] | None = None  # (mtime, prompt)
_MODEL = "claude-sonnet-4-6"
# 스트리밍 전체 상한(초). 정상 장문서(10분+)는 통과시키되 무한 hang만 차단해
# 동시처리 슬롯(semaphore)이 영구 점유되는 사고를 막는다.
# 기본 30분 — 64k 토큰 풀출력 worst-case에도 여유. 운영 튜닝은 PHASE2_STREAM_TIMEOUT env로.
_PHASE2_TIMEOUT = int(os.getenv("PHASE2_STREAM_TIMEOUT", "1800"))


def _get_system_prompt() -> str:
    """docs/phase2-prompt.md의 '## 프롬프트' 코드펜스 내용을 시스템 프롬프트로 사용.

    사용 섹션: '## 프롬프트' 코드펜스 안 (없으면 파일 전체) / 무시 섹션: 메타 설명·예시.
    mtime 기반 캐시 — md 수정 시 백엔드 재시작 없이 다음 호출부터 반영된다.
    """
    global _SYSTEM_PROMPT_CACHE
    prompt_path = get_settings().workspace_root / "docs" / "phase2-prompt.md"
    mtime = prompt_path.stat().st_mtime
    if _SYSTEM_PROMPT_CACHE is not None and _SYSTEM_PROMPT_CACHE[0] == mtime:
        return _SYSTEM_PROMPT_CACHE[1]
    raw = prompt_path.read_text(encoding="utf-8")
    # ## 프롬프트 섹션의 코드 펜스 안 내용만 추출 (메타 설명·예시 섹션 제외)
    m = re.search(r'## 프롬프트\s*\n+```[^\n]*\n', raw)
    if m:
        prompt_start = m.end()
        close = raw.rfind('\n```')
        content = raw[prompt_start:close] if close > prompt_start else raw[prompt_start:]
        used = "'## 프롬프트' 코드펜스"
    else:
        content = raw
        used = "파일 전체 ('## 프롬프트' 코드펜스 없음)"
    prompt = content.strip()
    logger.info(
        "phase2 시스템 프롬프트 로드 — %s %d자 사용 (phase2-prompt.md mtime=%.0f)",
        used, len(prompt), mtime,
    )
    _SYSTEM_PROMPT_CACHE = (mtime, prompt)
    return prompt


def _page_num(f: Path) -> int:
    m = re.search(r"page_(\d+)\.md", f.name)
    return int(m.group(1)) if m else 0


async def run_phase2(
    doc_id: str,
    form_id: str,
    output_dir: Path,
    page_range: tuple[int, int] | None = None,
    page_numbers: list[int] | None = None,
    run_id: str = "",
    row_anchors: list[dict] | None = None,
    write_output: bool = True,
) -> dict:
    """지정 페이지의 page MD + form 정의 → items[] JSON.
    page_range=(start, end): 연속 범위 (번들 분리용).
    page_numbers=[1,2,7,8,...]: 비연속 페이지 목록 (청크 분할용).
    row_anchors: 후보 상품 행 앵커 목록 (form_types.json row_anchor 설정이 있는 양식). None이면 기존 방식.
    스트리밍으로 10분 이상 요청도 처리 가능.
    """
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    system = _get_system_prompt()

    all_mds = sorted(output_dir.glob("page_*.md"), key=_page_num)
    if page_numbers is not None:
        nums_set = set(page_numbers)
        md_files = [f for f in all_mds if _page_num(f) in nums_set]
    elif page_range:
        start, end = page_range
        md_files = [f for f in all_mds if start <= _page_num(f) <= end]
    else:
        md_files = all_mds

    # phase2-prompt.md 스펙에 맞게 === Page N === 구분자 삽입
    combined_md = "\n\n".join(
        f"=== Page {_page_num(f)} ===\n{f.read_text(encoding='utf-8')}"
        for f in md_files
    )

    # row anchor 섹션 — LLM 계약 변경:
    #   "문서에서 item을 찾아라" → "이 row_id 목록을 빠짐없이 처리하라"
    anchor_section = ""
    if row_anchors:
        anchor_section = (
            "## row anchor 목록\n\n"
            "다음 후보 상품 행 앵커를 **모두 빠짐없이** 처리하라.\n"
            "- 상품 행이면: item JSON에 `\"row_id\"` 필드를 포함한다.\n"
            "- 상품 행이 아니면: `{\"row_id\": \"...\", \"not_item\": true}` 형식으로 items[]에 포함한다.\n"
            "**모든 row_id를 처리한다. 누락 금지.**\n\n"
            + json.dumps(row_anchors, ensure_ascii=False, indent=2)
            + "\n\n"
        )
    form_path = settings.form_definitions_dir / f"{form_id}.md"
    form_md = form_path.read_text(encoding="utf-8") if form_path.exists() else ""

    page_desc = f"p{md_files[0].stem.split('_')[1]}~p{md_files[-1].stem.split('_')[1]}" if md_files else "?"
    logger.info("[%s] Phase 2 요청 시작 (%d페이지: %s)", doc_id, len(md_files), page_desc)
    if row_anchors:
        logger.info("[%s] Phase 2 row anchor %d개 포함 (%s)", doc_id, len(row_anchors), page_desc)

    # 스트리밍 — 10분 초과 요청도 처리 가능 (Anthropic SDK 요구 사항).
    # 단, 전체를 _PHASE2_TIMEOUT 상한으로 감싸 무한 hang 시 슬롯 영구 점유를 막는다.
    async def _consume_stream():
        token_count = 0
        async with client.messages.stream(
            model=_MODEL,
            max_tokens=64000,
            system=[
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": f"## 양식 정의\n\n{form_md}", "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": anchor_section + combined_md + "\n\nJSON만 출력하세요. 설명, 주석, 마크다운 없이 순수 JSON만."}],
        ) as stream:
            async for chunk in stream.text_stream:
                token_count += len(chunk)
                if token_count % 20000 < len(chunk):
                    logger.info("[%s] Phase 2 스트리밍 중 (%s, ~%d자 수신)", doc_id, page_desc, token_count)
            return await stream.get_final_message()

    async def _consume_with_timeout():
        # per-attempt 타임아웃 — 재시도 시 매 시도가 _PHASE2_TIMEOUT 안에 끝나야 한다.
        # 재시도(429/5xx/connection)는 스트림을 처음부터 새로 연다.
        return await asyncio.wait_for(_consume_stream(), timeout=_PHASE2_TIMEOUT)

    try:
        # asyncio.TimeoutError는 비재시도 대상 → 기존 '타임아웃=실패' 동작 보존
        message = await async_call_with_retry(_consume_with_timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"[{doc_id}] Phase 2 스트리밍이 {_PHASE2_TIMEOUT}s 내 완료되지 않았습니다. "
            "무한 대기 차단 — 문서 분량 또는 네트워크 상태를 확인하세요."
        ) from exc

    logger.info("[%s] Phase 2 완료 (%s, out=%d토큰)", doc_id, page_desc, message.usage.output_tokens)

    if message.stop_reason == "max_tokens":
        raise RuntimeError(
            f"[{doc_id}] Phase 2 응답이 max_tokens(64000)에서 잘렸습니다. "
            "문서 분량이 너무 많거나 출력 형식을 점검하세요."
        )

    usage = message.usage
    await accumulate_token_usage(
        doc_id, "phase2",
        usage.input_tokens, usage.output_tokens,
        _MODEL,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        run_id=run_id,
    )

    raw = message.content[0].text if message.content else ""

    # 코드 펜스 안 JSON 추출
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()

    raw = raw.strip()
    if not raw:
        raise RuntimeError(
            f"[{doc_id}] Phase 2 Claude 응답이 비어 있습니다. "
            f"stop_reason={message.stop_reason}, content_blocks={len(message.content)}"
        )

    # JSON 시작 위치 탐색 (앞뒤 설명문이 붙은 경우)
    brace = raw.find("{")
    if brace > 0:
        raw = raw[brace:]

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(
            "[%s] Phase 2 JSON 파싱 실패 — stop_reason=%s\n"
            "--- raw (앞 1000자) ---\n%s\n--- end ---",
            doc_id, message.stop_reason, raw[:1000],
        )
        raise RuntimeError(
            f"[{doc_id}] Phase 2 JSON 파싱 실패 (stop_reason={message.stop_reason}): {e}\n"
            f"raw 앞 300자: {raw[:300]!r}"
        ) from e

    if write_output:
        out_path = output_dir / "phase2_output.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
