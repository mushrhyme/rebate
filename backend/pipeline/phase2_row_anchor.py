"""Phase 2 row anchor 생성기 — form_types.json 설정 기반 범용 행 앵커링.

왜 필요한가:
  LLM이 반복 표에서 블록 헤더 직후 첫 번째 상품 행을 놓치는 문제를 방지한다.
  row anchor는 "MD에 이 후보 행들이 있다"는 사실을 Python이 먼저 앵커링하고,
  LLM은 각 row_id에 대해 item / not_item 중 하나로 답하도록 계약을 바꾼다.

설정 (form_types.json의 row_anchor 섹션):
  block_pattern    — 블록 헤더를 감지하는 정규식. 그룹 1: 블록 ID
  subgroup_pattern — 서브그룹 헤더를 감지하는 정규식. 그룹 1: 서브그룹명 (옵션)
  condition_pattern — 条件タイプ 감지 정규식 (옵션)
  total_pattern    — 합계 행을 감지하는 정규식
  header_keywords  — 제품 행 판별 시 제외할 키워드 목록

앵커 필드:
  row_id              — "p{page:03d}:k{block_id}:r{idx:02d}" (문서 내 유일)
  page                — 페이지 번호
  kanri_no_hint       — 현재 블록 ID (管理No 등)
  condition_type_hint — 条件タイプ (없으면 None)
  jisho_hint          — 서브그룹명 (없으면 "")
  raw_row             — 원본 MD 행 문자열 (strip 후)
  amount_hint         — 마지막 양수 정수 셀 = 金額 추정값 (없으면 None)
  row_index_in_kanri  — 블록 내 0-기반 인덱스
"""
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def _is_product_row(
    line: str,
    cells: list[str],
    header_keywords: tuple[str, ...],
    re_total: re.Pattern,
) -> bool:
    """상품 행 판정 — header/aggregate/separator 제외."""
    if len(cells) < 3:
        return False
    product = cells[1]
    if not product:
        return False
    if re_total.search(line):
        return False
    for kw in header_keywords:
        if kw in product:
            return False
    if re.match(r'^[*＊]+$', product):
        return False
    if re.match(r'^-+$', product):
        return False
    return True


def _extract_amount_hint(cells: list[str]) -> int | None:
    """마지막 양수 정수 셀 → 金額 추정 (rightmost positive integer)."""
    for c in reversed(cells):
        num = c.replace(',', '').replace(' ', '')
        if num.isdigit() and int(num) > 0:
            return int(num)
    return None


def build_row_anchors(row_anchor_config: dict, output_dir: Path) -> list[dict]:
    """form_types.json row_anchor 설정 기반 범용 행 앵커 생성.

    신규 양식 추가 시 코드 수정 없이 form_types.json의 row_anchor 섹션만으로 동작한다.
    """
    re_block      = re.compile(row_anchor_config["block_pattern"])
    re_subgroup   = re.compile(row_anchor_config["subgroup_pattern"]) if row_anchor_config.get("subgroup_pattern") else None
    re_condition  = re.compile(row_anchor_config["condition_pattern"]) if row_anchor_config.get("condition_pattern") else None
    re_total      = re.compile(row_anchor_config.get("total_pattern", r'計[：:]'))
    header_kws    = tuple(row_anchor_config.get("header_keywords", []))

    anchors: list[dict] = []
    current_subgroup:  str        = ""
    current_block:     str | None = None
    current_condition: str | None = None
    row_idx: int = 0

    md_files = sorted(
        output_dir.glob("page_*.md"),
        key=lambda f: int(re.search(r'(\d+)', f.name).group()),
    )

    for md_file in md_files:
        m_pg = re.search(r'page_(\d+)\.md', md_file.name)
        if not m_pg:
            continue
        page_num = int(m_pg.group(1))

        content = md_file.read_text(encoding='utf-8')

        # detail 페이지만 앵커 대상 — cover/summary/payment_form 제외
        hint_m = re.search(r'^page_type_hint:\s*(\w+)', content, re.MULTILINE | re.IGNORECASE)
        if hint_m:
            role = hint_m.group(1).lower()
            if role in ('cover', 'summary', 'payment_form'):
                continue

        # 페이지 경계에서 블록 리셋 — 이전 페이지 상태가 다음 페이지 헤더 행을 오염하는 것 방지
        current_block = None
        row_idx = 0

        for line in content.splitlines():
            if '|' not in line:
                continue
            cells = [c.strip() for c in line.split('|')]

            # 서브그룹 헤더 감지 (예: 入出荷支店)
            if re_subgroup:
                m_sub = re_subgroup.search(line)
                if m_sub:
                    current_subgroup = m_sub.group(1).strip()
                    continue

            # 블록 헤더 감지 (예: 管理No:1710151)
            m_block = re_block.search(line)
            if m_block and not re_total.search(line):
                current_block = m_block.group(1)
                current_condition = (
                    re_condition.search(line).group(1)
                    if re_condition and re_condition.search(line) else None
                )
                row_idx = 0
                continue

            if current_block is None:
                continue

            if not _is_product_row(line, cells, header_kws, re_total):
                continue

            row_id = f"p{page_num:03d}:k{current_block}:r{row_idx:02d}"
            anchors.append({
                'row_id':              row_id,
                'page':                page_num,
                'kanri_no_hint':       current_block,
                'condition_type_hint': current_condition,
                'jisho_hint':          current_subgroup,
                'raw_row':             line.strip(),
                'amount_hint':         _extract_amount_hint(cells),
                'row_index_in_kanri':  row_idx,
            })
            row_idx += 1

    return anchors


def save_row_anchors(output_dir: Path, anchors: list[dict]) -> None:
    path = output_dir / "phase2_row_anchors.json"
    path.write_text(json.dumps(anchors, ensure_ascii=False, indent=2), encoding='utf-8')


def load_row_anchors(output_dir: Path) -> list[dict]:
    path = output_dir / "phase2_row_anchors.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return []
