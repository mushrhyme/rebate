"""Phase 4 — NET 계산 (Python 결정적 코드) + 교차검증 (Claude).

NET 계산: scripts/phase4_calc.py (subprocess, LLM 없음)
교차검증: Claude が form_XX.md の교차검증 섹션을 읽어 computed vs document totals 비교
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import anthropic

from ..core.config import get_settings
from ..db.queries import accumulate_token_usage

log = logging.getLogger(__name__)

_XV_MODEL = "claude-haiku-4-5-20251001"


async def run_phase4(doc_id: str, run_id: str = "", skip_xv: bool = False) -> dict:
    """phase4_calc.py 실행 → Claude 교차검증 → phase4_output.json 반환.

    skip_xv=True: remap 재실행 시 사용. NET 재계산만 하고 기존 xv 결과를 유지.
    """
    settings = get_settings()
    script = settings.workspace_root / "scripts" / "phase4_calc.py"

    # ── NET 계산 (Python subprocess) ─────────────────────────────────────────
    # Windows 기본 콘솔 인코딩(cp932 등)으로는 스크립트의 em-dash 등 비ASCII 출력이
    # UnicodeEncodeError를 일으키므로, 자식 프로세스를 UTF-8 모드로 강제한다.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script), "--doc", doc_id, "--save",
        cwd=str(settings.workspace_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300.0)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("phase4_calc.py 실행 시간 초과 (5분)")

    if proc.returncode != 0:
        raise RuntimeError(f"phase4_calc.py 실패:\n{stderr.decode()}")

    out_path = settings.extracted_dir / doc_id / "phase4_output.json"
    if not out_path.exists():
        raise RuntimeError("phase4_output.json 생성 안 됨")

    phase4_data = json.loads(out_path.read_text(encoding="utf-8"))

    if skip_xv:
        log.info("[%s] 교차검증 건너뜀 (remap 재실행)", doc_id)
        return phase4_data

    # ── 교차검증 (Claude) ─────────────────────────────────────────────────────
    # Python calc xv가 있으면 그걸 그대로 사용.
    # Python 결과가 비어있을 때만 Claude를 시도한다 (레이블이 더 깔끔하고 결정적이기 때문).
    python_xv = phase4_data.get("xv", [])
    if python_xv:
        log.info("[%s] Python calc xv 사용 (%d개) — Claude 교차검증 건너뜀", doc_id, len(python_xv))
        return phase4_data

    xv_flags: dict = {}
    xv_results = await _run_cross_validation(doc_id, phase4_data, settings, run_id=run_id, error_flags=xv_flags)
    if xv_results is not None:
        phase4_data["xv"] = xv_results
        if xv_flags.get("xv_error"):
            phase4_data["xv_error"] = True
        out_path.write_text(
            json.dumps(phase4_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("[%s] Claude 교차검증 완료 — %d개 규칙", doc_id, len(xv_results))

    return phase4_data


async def _run_cross_validation(doc_id: str, phase4_data: dict, settings, run_id: str = "", error_flags: dict | None = None) -> list[dict] | None:
    """Claude が form_XX.md の교차검증 섹션을 읽고 검증 결과 반환.

    form_XX.md 없음 또는 교차검증 섹션 없음 → None (xv 유지).
    검증 실패(JSON 파싱 오류 등) → [] (빈 리스트로 교체).
    """
    form_id = phase4_data.get("form_id", "")
    form_path = settings.form_definitions_dir / f"{form_id}.md"
    if not form_path.exists():
        log.warning("[%s] %s.md 없음 — 교차검증 건너뜀", doc_id, form_id)
        return None

    form_md = form_path.read_text(encoding="utf-8")

    # 교차검증 섹션 존재 여부 확인 (없으면 건너뜀)
    if "교차검증" not in form_md and "cross_validation" not in form_md.lower():
        log.info("[%s] form_XX.md에 교차검증 섹션 없음 — 건너뜀", doc_id)
        return None

    # Phase 2 출력에서 cover/summary totals 읽기
    p2_path = settings.extracted_dir / doc_id / "phase2_output.json"
    p2_data: dict = {}
    if p2_path.exists():
        p2_data = json.loads(p2_path.read_text(encoding="utf-8"))

    cover_totals: dict = {}
    summary_totals: dict = {}
    customer_summaries: dict = {}
    for page in p2_data.get("pages", []):
        role = page.get("role", "")
        if role == "cover" and not cover_totals:
            cover_totals = page.get("totals") or {}
        elif role == "summary":
            summary_totals.update(page.get("totals") or {})
            customer_summaries.update(page.get("customer_summaries") or {})

    # Phase 3 items에서 得意先별 金額 합산 (税抜)
    p3_path = settings.extracted_dir / doc_id / "phase3_output.json"
    by_customer_detail: dict[str, int] = {}
    if p3_path.exists():
        p3_data = json.loads(p3_path.read_text(encoding="utf-8"))
        for item in p3_data.get("items", []):
            cust = item.get("customer", "")
            kin_gaku = int(item.get("columns", {}).get("金額", 0) or 0)
            if cust and kin_gaku:
                by_customer_detail[cust] = by_customer_detail.get(cust, 0) + kin_gaku

    # Phase 4 rows에서 집계 값 계산
    rows = phase4_data.get("rows", [])
    summary = phase4_data.get("summary", {})

    # detail_group_field를 form_types.json에서 읽어 cover_breakdown_vs_detail 집계에 사용
    form_types_path = settings.workspace_root / "config" / "form_types.json"
    form_config: dict = {}
    if form_types_path.exists():
        form_config = json.loads(form_types_path.read_text(encoding="utf-8")).get(form_id, {})

    breakdown_field: str | None = None
    breakdown_amount_field: str = "未収金額合計"
    for xv_cfg in form_config.get("cross_validation", []):
        if xv_cfg.get("type") == "cover_breakdown_vs_detail":
            breakdown_field = xv_cfg.get("detail_group_field")
            breakdown_amount_field = xv_cfg.get("detail_amount_field", "未収金額合計")
            break

    by_jisho: dict[str, int] = {}
    if breakdown_field:
        for r in rows:
            j = r.get(breakdown_field, "")
            if j:
                by_jisho[j] = by_jisho.get(j, 0) + int(r.get(breakdown_amount_field) or 0)

    computed = {
        "total_kin_gaku_ex_tax": summary.get("total_ex", 0),
        "by_tax_rate": summary.get("by_rate", {}),
        "by_jisho": by_jisho,
        "by_customer_detail": by_customer_detail,
    }

    # Claude 호출
    system = (
        "あなたは日本語請求書データの교차검증 담당입니다.\n\n"
        "## 양식 정의\n\n"
        f"{form_md}\n\n"
        "위 양식 정의의 '[Phase 4] NET 계산식 > 교차검증' 섹션에 정의된 규칙에 따라 "
        "사용자 메시지의 숫자 데이터를 검증하세요.\n\n"
        "사용자 메시지 구조:\n"
        "- computed.total_kin_gaku_ex_tax: Phase 4에서 집계한 전체 金額 합계 (消費税 行 제외, 税抜)\n"
        "- computed.by_tax_rate: 세율별 金額 합계 (税抜)\n"
        "- computed.by_jisho: 지소(事業所)별 金額 합계\n"
        "- computed.by_customer_detail: 得意先名별 detail 金額 합산 (税抜)\n"
        "- cover_totals: 청구서 표지 합계 (일본어 키 그대로)\n"
        "- summary_totals: summary 페이지 집계 합계 (일본어 키 그대로)\n"
        "- customer_summaries: summary 페이지 得意先別 소계 dict (得意先名 → 金額, 税抜)\n\n"
        "JSON만 출력합니다. 다른 설명은 불필요합니다.\n\n"
        "출력 형식:\n"
        '{"cross_validation": [\n'
        '  {\n'
        '    "rule": "①",\n'
        '    "description": "검증 내용 한 줄",\n'
        '    "computed": 12345678,\n'
        '    "expected_key": "cover_totals의 키명 (해당 없으면 null)",\n'
        '    "expected": 12345678,\n'
        '    "match": true,\n'
        '    "diff": 0\n'
        '  }\n'
        ']}\n\n'
        "교차검증 섹션이 없거나 검증 불가 시: {\"cross_validation\": []}"
    )

    user_content = json.dumps(
        {
            "computed": computed,
            "cover_totals": cover_totals,
            "summary_totals": summary_totals,
            "customer_summaries": customer_summaries,
        },
        ensure_ascii=False,
        indent=2,
    )

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    message = await client.messages.create(
        model=_XV_MODEL,
        max_tokens=1024,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
    )

    # 토큰 사용량 기록
    usage = message.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    await accumulate_token_usage(
        doc_id, "phase4_xv", usage.input_tokens, usage.output_tokens, _XV_MODEL,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        run_id=run_id,
    )

    raw = message.content[0].text.strip()
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()

    try:
        result = json.loads(raw)
        items = result.get("cross_validation", [])
        return [
            {
                "label": f"{item.get('rule', '')} {item.get('description', '')}".strip(),
                "actual": item.get("computed"),
                "ok": bool(item.get("match", False)),
                "expected": item.get("expected"),
                "diff": item.get("diff"),
            }
            for item in items
        ]
    except (json.JSONDecodeError, AttributeError) as e:
        log.error("[%s] 교차검증 JSON 파싱 실패: %s — raw: %r", doc_id, e, raw[:200])
        if error_flags is not None:
            error_flags["xv_error"] = True
        return []
