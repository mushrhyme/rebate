"""form_04 row anchor 생성기 — Phase 2 LLM 호출 전 후보 상품 행 앵커링.

왜 필요한가:
  현재 Phase 2는 LLM에게 "문서에서 item을 찾아라"고 시킨다.
  반복 표에서 LLM은 8줄 이상 연속 헤더·집계 행을 지난 뒤 첫 번째 상품 행을 놓친다.
  row anchor는 "MD에 이 후보 행들이 있다"는 사실을 Python이 먼저 앵커링하고,
  LLM은 각 row_id에 대해 item / not_item 중 하나로 답하도록 계약을 바꾼다.

앵커 필드:
  row_id              — "p{page:03d}:k{kanri_no}:r{idx:02d}" (문서 내 유일)
  page                — 페이지 번호
  kanri_no_hint       — 현재 管理No
  condition_type_hint — 定番条件 / 原価引き条件 / 導入条件 (없으면 None)
  jisho_hint          — 현재 入出荷支店 (없으면 "")
  raw_row             — 원본 MD 행 문자열 (strip 후)
  amount_hint         — 마지막 양수 정수 셀 = 金額 추정값 (없으면 None)
  row_index_in_kanri  — 블록 내 0-기반 인덱스
"""
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_RE_KANRI_HEADER   = re.compile(r'管理No\s*[：:]\s*(\d{5,8})')
_RE_CONDITION_TYPE = re.compile(r'(定番条件|原価引き条件|導入条件)')
_RE_JISHO          = re.compile(r'入出荷支店\s*[：:]\s*(\S+)')
_RE_TOTAL_CELL     = re.compile(r'計[：:]')

_HEADER_KEYWORDS = (
    '計上場所', '入出荷支店', '入出荷センター',
    '得意先', '管理No', '得意先又は商品', '整数部', '小数部',
    # 各ページ冒頭の文書ヘッダ行（請求書No./作成日/支払予定日 等）
    '請求書', '作成日', 'ご請求期', 'お支払予定', '未収取扱', '発行元', '販売促進', '項目',
)


def _is_product_row(line: str, cells: list[str]) -> bool:
    """상품 행 판정 — header/aggregate/separator 제외."""
    if len(cells) < 3:
        return False
    product = cells[1]
    if not product:
        return False
    if _RE_TOTAL_CELL.search(line):
        return False
    for kw in _HEADER_KEYWORDS:
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


def build_row_anchors_form04(output_dir: Path) -> list[dict]:
    """form_04 detail page MD 전체를 스캔해 후보 상품 행 앵커 목록을 반환.

    cover / payment_form 페이지는 관리No가 없으므로 앵커가 생성되지 않는다.
    detail 페이지에서만 실질적인 앵커가 생긴다.
    """
    anchors: list[dict] = []

    current_jisho:     str       = ""
    current_kanri:     str | None = None
    current_condition: str | None = None
    row_idx:           int        = 0

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
        # page_type_hint가 없거나 detail/unknown이면 통과
        hint_m = re.search(r'^page_type_hint:\s*(\w+)', content, re.MULTILINE | re.IGNORECASE)
        if hint_m:
            role = hint_m.group(1).lower()
            if role in ('cover', 'summary', 'payment_form'):
                continue

        # 페이지 경계에서 current_kanri 리셋 — 이전 페이지에서 이어진 kanri 상태가
        # 다음 페이지 상단의 문서 헤더 행을 product anchor로 잘못 등록하는 것을 방지
        current_kanri = None
        row_idx = 0

        for line in content.splitlines():
            if '|' not in line:
                continue
            cells = [c.strip() for c in line.split('|')]

            # ── 入出荷支店 헤더 감지 ──────────────────────────────
            m_jisho = _RE_JISHO.search(line)
            if m_jisho:
                current_jisho = m_jisho.group(1).strip()
                continue

            # ── 管理No 헤더 감지 ─────────────────────────────────
            m_kanri = _RE_KANRI_HEADER.search(line)
            if m_kanri and not _RE_TOTAL_CELL.search(line):
                current_kanri = m_kanri.group(1)
                m_cond = _RE_CONDITION_TYPE.search(line)
                current_condition = m_cond.group(1) if m_cond else None
                row_idx = 0
                continue

            if current_kanri is None:
                continue

            # ── 상품 행 판정 ──────────────────────────────────────
            if not _is_product_row(line, cells):
                continue

            amount_hint = _extract_amount_hint(cells)
            row_id = f"p{page_num:03d}:k{current_kanri}:r{row_idx:02d}"

            anchors.append({
                'row_id':              row_id,
                'page':                page_num,
                'kanri_no_hint':       current_kanri,
                'condition_type_hint': current_condition,
                'jisho_hint':          current_jisho,
                'raw_row':             line.strip(),
                'amount_hint':         amount_hint,
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
