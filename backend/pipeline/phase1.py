"""Phase 1 — OCR txt → page MD (Claude API)."""
import asyncio
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

import anthropic

from ..core.config import get_settings
from ..db.queries import accumulate_token_usage

_SYSTEM_PROMPT_CACHE: tuple[float, str] | None = None  # (mtime, prompt)
_MODEL = "claude-haiku-4-5-20251001"

# 문서 내 페이지 병렬 호출 상한 (env: MAX_CONCURRENT_PHASE1_PAGES, 기본 5)
# 파이프라인 5개 동시 × 페이지 5개 = 최대 25개 Haiku 동시 호출
_page_semaphore: asyncio.Semaphore | None = None


def _get_page_semaphore() -> asyncio.Semaphore:
    global _page_semaphore
    if _page_semaphore is None:
        limit = int(os.getenv("MAX_CONCURRENT_PHASE1_PAGES", "5"))
        _page_semaphore = asyncio.Semaphore(limit)
    return _page_semaphore


def _get_system_prompt() -> str:
    """docs/phase1-prompt.md의 '## 프롬프트' 코드펜스 내용을 시스템 프롬프트로 사용.

    사용 섹션: '## 프롬프트' 코드펜스 안 / 무시 섹션: '## 검증 기준' 이하 + 펜스 밖 텍스트.
    mtime 기반 캐시 — md 수정 시 백엔드 재시작 없이 다음 호출부터 반영된다.
    """
    global _SYSTEM_PROMPT_CACHE
    prompt_path = get_settings().workspace_root / "docs" / "phase1-prompt.md"
    mtime = prompt_path.stat().st_mtime
    if _SYSTEM_PROMPT_CACHE is not None and _SYSTEM_PROMPT_CACHE[0] == mtime:
        return _SYSTEM_PROMPT_CACHE[1]
    raw = prompt_path.read_text(encoding="utf-8")
    # 검증 기준 섹션 제거 — Phase B 변환 호출에는 불필요한 오버헤드
    content = re.split(r'\n---\s*\n\s*## 검증 기준', raw)[0]
    # ## 프롬프트 섹션의 코드 펜스 안 내용만 추출
    m = re.search(r'## 프롬프트\s*\n+```[^\n]*\n', content)
    if m:
        prompt_start = m.end()
        close = content.rfind('\n```')
        content = content[prompt_start:close] if close > prompt_start else content[prompt_start:]
    prompt = content.strip()
    log.info(
        "phase1 시스템 프롬프트 로드 — '## 프롬프트' 코드펜스 %d자 사용, "
        "'## 검증 기준' 이하 제외 (phase1-prompt.md mtime=%.0f)", len(prompt), mtime,
    )
    _SYSTEM_PROMPT_CACHE = (mtime, prompt)
    return prompt


def _parse_ocr_tables(ocr_text: str) -> list[dict]:
    """OCR txt의 --- tables --- 섹션에서 테이블 목록 파싱.
    반환: [{cols: int, rows: int}, ...] — 연속된 탭 행 그룹 단위.
    """
    marker = "--- tables ---"
    idx = ocr_text.lower().find(marker)
    if idx == -1:
        return []

    table_section = ocr_text[idx + len(marker):]
    tables: list[dict] = []
    current: list[str] = []

    for line in table_section.splitlines():
        if "\t" in line:
            current.append(line)
        else:
            if current:
                col_counts = [ln.count("\t") + 1 for ln in current]
                tables.append({"cols": max(col_counts), "rows": max(0, len(current) - 1)})
                current = []

    if current:
        col_counts = [ln.count("\t") + 1 for ln in current]
        tables.append({"cols": max(col_counts), "rows": max(0, len(current) - 1)})

    return tables


def _parse_md_tables(md_text: str) -> list[dict]:
    """MD 텍스트에서 마크다운 테이블 파싱.
    반환: [{cols: int, rows: int}, ...] — 헤더 기준 컬럼 수, 데이터 행 수.
    """
    tables: list[dict] = []
    in_table = False
    header_cols = 0
    data_rows = 0

    for line in md_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            parts = [p.strip() for p in stripped.split("|")[1:-1]]
            if all(set(p) <= set("-: ") for p in parts):
                continue  # 구분 행
            if not in_table:
                in_table = True
                header_cols = len(parts)
                data_rows = 0
            else:
                data_rows += 1
        else:
            if in_table:
                tables.append({"cols": header_cols, "rows": data_rows})
                in_table = False
                header_cols = 0
                data_rows = 0

    if in_table:
        tables.append({"cols": header_cols, "rows": data_rows})

    return tables


def _validate_page_md(ocr_text: str, md_text: str, page_num: int) -> list[dict]:
    """T1/T2/T4 구조 검증.
    T1: OCR 테이블 있는데 MD 테이블 없음 (high)
    T2: 테이블 컬럼 수 손실 2개 이상 (medium)
    T4: 테이블 행 수 60% 이상 손실 (medium, OCR 3행 이상 테이블만)
    반환: 실패 항목 목록 [{page, check, severity, detail}, ...].
    """
    issues: list[dict] = []
    ocr_tables = _parse_ocr_tables(ocr_text)
    md_tables = _parse_md_tables(md_text)

    # T1 — OCR에 테이블이 있는데 MD에 테이블이 전혀 없음
    if ocr_tables and not md_tables:
        issues.append({
            "page": page_num, "check": "T1", "severity": "high",
            "detail": f"OCR {len(ocr_tables)}개 테이블 있으나 MD 테이블 없음",
        })
        return issues  # T2/T4는 MD 테이블이 있어야 비교 가능

    # T2/T4 — 테이블 수가 일치할 때만 개별 테이블 비교
    # Phase 1이 보조 행(헤더 반복·집계행)을 병합할 수 있으므로 T4는 40% 미만 임계값 적용
    if ocr_tables and md_tables and len(ocr_tables) == len(md_tables):
        for i, (ocr_t, md_t) in enumerate(zip(ocr_tables, md_tables)):
            if ocr_t["cols"] > md_t["cols"] + 2:
                issues.append({
                    "page": page_num, "check": "T2", "severity": "medium",
                    "detail": (
                        f"테이블 {i+1}: OCR {ocr_t['cols']}컬럼 → MD {md_t['cols']}컬럼 "
                        f"(손실 {ocr_t['cols'] - md_t['cols']}개)"
                    ),
                })
            if ocr_t["rows"] >= 3 and md_t["rows"] < ocr_t["rows"] * 0.4:
                pct = round(md_t["rows"] / ocr_t["rows"] * 100)
                issues.append({
                    "page": page_num, "check": "T4", "severity": "medium",
                    "detail": (
                        f"테이블 {i+1}: OCR {ocr_t['rows']}행 → MD {md_t['rows']}행 "
                        f"({pct}% 보존, 60% 미만)"
                    ),
                })

    return issues


def _cleanup_md(text: str) -> str:
    """코드블록 래핑·검증 섹션·doc_id 필드 제거."""
    lines = text.split("\n")
    if lines and lines[0].strip() in ("```", "```markdown"):
        close = next((i for i, l in enumerate(lines) if i > 0 and l.strip() == "```"), None)
        text = "\n".join(lines[1:close]) if close else text
    text = re.split(r"\n---\s*\n\s*##\s*(검증|検証)", text)[0].rstrip()
    text = re.sub(r"\ndoc_id:[^\n]*", "", text)
    return text + "\n"


async def run_phase1(doc_id: str, pages_dir: Path, output_dir: Path, run_id: str = "") -> list[Path]:
    """각 page_NNN.ocr.txt → page_NNN.md 변환.
    첫 페이지는 단독 await(캐시 생성), 나머지는 병렬 실행(캐시 히트).
    """
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    system = _get_system_prompt()

    ocr_files = sorted(pages_dir.glob("page_*.ocr.txt"))
    output_dir.mkdir(parents=True, exist_ok=True)

    _PAGE_TIMEOUT = float(os.getenv("PHASE1_PAGE_TIMEOUT", "120"))

    async def _convert(ocr_file: Path) -> tuple[Path, int, int, int, int, list[dict]]:
        page_num = int(ocr_file.name.split("_")[1].split(".")[0])
        ocr_text = await asyncio.to_thread(ocr_file.read_text, "utf-8")
        async with _get_page_semaphore():
            message = await asyncio.wait_for(
                client.messages.create(
                    model=_MODEL,
                    max_tokens=16384,
                    system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": f"=== Page {page_num} ===\n\n{ocr_text}"}],
                ),
                timeout=_PAGE_TIMEOUT,
            )
        if message.stop_reason == "max_tokens":
            import logging
            logging.getLogger(__name__).warning(
                "page_%03d: max_tokens reached — output truncated. Consider splitting the page.", page_num
            )
        cleaned = _cleanup_md(message.content[0].text)
        out_path = output_dir / f"page_{page_num:03d}.md"
        await asyncio.to_thread(out_path.write_text, cleaned, "utf-8")
        issues = _validate_page_md(ocr_text, cleaned, page_num)
        usage = message.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
        return out_path, usage.input_tokens, usage.output_tokens, cache_read, cache_creation, issues

    if not ocr_files:
        return []

    paths: list[Path] = []
    all_issues: list[dict] = []
    total_in = total_out = total_cr = total_cc = 0

    # 첫 페이지 단독 처리 → 시스템 프롬프트 캐시 생성
    first = await _convert(ocr_files[0])
    paths.append(first[0])
    total_in += first[1]; total_out += first[2]
    total_cr += first[3]; total_cc += first[4]
    all_issues.extend(first[5])

    # 나머지 페이지 병렬 처리 → 캐시 히트 (입력 토큰 ~90% 절감)
    if len(ocr_files) > 1:
        rest = await asyncio.gather(*[_convert(f) for f in ocr_files[1:]])
        for path, inp, out, cr, cc, issues in rest:
            paths.append(path)
            total_in += inp; total_out += out
            total_cr += cr; total_cc += cc
            all_issues.extend(issues)

    if total_in or total_out:
        await accumulate_token_usage(
            doc_id, "phase1", total_in, total_out, _MODEL,
            cache_read_tokens=total_cr,
            cache_creation_tokens=total_cc,
            run_id=run_id,
        )

    if all_issues:
        warnings_path = output_dir / "phase1_warnings.json"
        warnings_path.write_text(
            json.dumps(all_issues, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.warning("[%s] Phase 1 구조 검증 이슈 %d건 → phase1_warnings.json", doc_id, len(all_issues))

    return sorted(paths)
