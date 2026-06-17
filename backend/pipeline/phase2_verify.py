"""Phase 2 역산 검증 — 管理No計 vs items 합산 비교 + 핀포인트 재요청.

검증 레이어 2단계:
  1차 (row anchor) — form_04 한정. phase2_row_anchors.json이 있으면
                     LLM이 누락한 row_id를 직접 탐지해 Python으로 복구.
  2차 (管理No計)   — 전 양식. MD의 管理No 합계와 items 금액 합산 비교.
                     미일치 시 결정적 복구 → Haiku 재요청 순으로 처리.
"""
import hashlib
import json
import logging
import re
from pathlib import Path

import anthropic

from ..core.config import get_settings
from ..db.queries import accumulate_token_usage
from .phase2_row_anchor import load_row_anchors

logger = logging.getLogger(__name__)
_VERIFY_MODEL = "claude-haiku-4-5-20251001"

# 管理No 헤더 행: "管理No:1565570" / "管理No: 1565570" / "管理No：1565570"
_RE_KANRI_HEADER = re.compile(r'管理No\s*[：:]\s*(\d{5,8})')
# 計: or 計： (全角・半角)
_RE_TOTAL_CELL = re.compile(r'計[：:]')
# 得意先/入出荷/支店/計上場所 계 행은 管理No計가 아님
_EXCLUDE_KEYWORDS = ('得意先', '入出荷', '支店', '計上場所', '売上未収')

# form_01용: 請求伝票番号 감지 (cells[1] = "4-A8001" 형식)
_RE_INVOICE_BLOCK = re.compile(r'^\|\s*(\d+[-][A-Z0-9]\d+)\s*\|')
# form_01용: 請求伝票番号 小計 행 감지
_RE_INVOICE_TOTAL = re.compile(r'請求伝票番号\s*小計')


def _extract_kanri_total(line: str) -> int | None:
    """管理No計 행에서 金額 추출. 해당 행이 아니면 None.

    다양한 OCR 분열 패턴을 처리:
      | * | 管理No | 計: | 6,486 | |
      | ＊ | 管 | 理 No | | 計: | 10750 |
      | * | 管 | 理 No | | 計: | 105790 |
    """
    if '|' not in line:
        return None

    cells = [c.strip() for c in line.split('|')]
    text  = ' '.join(cells)

    # 管理No計 행 판정 — 두 가지 OCR 패턴을 처리:
    #   패턴 A: 셀에 管理No 텍스트 포함 (page8·27·33 형식)
    #   패턴 B: 셀에 단일 ＊/＊만 있음, 管理No 텍스트 없음 (page3 형식)
    # 得意先計(**) / センター計(***) 는 셀이 ** 이상이어서 has_single_star = False
    has_kanri       = ('管理No' in text) or ('管' in text and '理' in text and 'No' in text)
    has_single_star = any(c in ('*', '＊') for c in cells)
    if not (has_kanri or has_single_star) or not _RE_TOTAL_CELL.search(text):
        return None
    # 得意先計 / 入出荷センター計 등은 제외
    if any(kw in text for kw in _EXCLUDE_KEYWORDS):
        return None

    # 計: 셀에서 金額 추출 — 두 가지 위치를 모두 확인:
    #   위치 A: 計: 와 값이 같은 셀 (page21 형식: "* / 管理No / 計: 77,406")
    #   위치 B: 計: 다음 셀 (page8·27·33 형식)
    for i, cell in enumerate(cells):
        if _RE_TOTAL_CELL.search(cell):
            # 위치 A: 같은 셀 안에 숫자
            m = re.search(r'計[：:]\s*([\d,]+)', cell)
            if m:
                num = m.group(1).replace(',', '')
                if num.isdigit():
                    return int(num)
            # 위치 B: 다음 셀에 숫자
            for j in range(i + 1, len(cells)):
                num = cells[j].replace(',', '').replace(' ', '')
                if num.isdigit():
                    return int(num)
    return None


def _parse_kanri_totals(output_dir: Path) -> dict[str, dict]:
    """전체 page MD를 순서대로 읽어 {kanri_no: {page, total, block_text}} 반환.

    管理No 블록이 페이지에 걸치는 경우(cross-page) current_kanri/block_lines를
    파일 간에도 유지해 올바른 block_text를 구성한다.
    """
    result: dict[str, dict] = {}
    current_kanri: str | None = None
    block_lines:   list[str]  = []
    current_page:  int        = 0

    md_files = sorted(
        output_dir.glob("page_*.md"),
        key=lambda f: int(re.search(r'(\d+)', f.name).group()),
    )

    for md_file in md_files:
        m_pg = re.search(r'page_(\d+)\.md', md_file.name)
        if not m_pg:
            continue
        page_num = int(m_pg.group(1))

        for line in md_file.read_text(encoding='utf-8').splitlines():
            # ── 헤더 감지 A: 표준 "管理No:12345" ────────────────
            m = _RE_KANRI_HEADER.search(line)
            if m and not _RE_TOTAL_CELL.search(line):
                current_kanri = m.group(1)
                current_page  = page_num
                block_lines   = [line]
                continue

            # ── 헤더 감지 B: 셀 형식 "1565543 定番条件" (page 5 등) ─
            # 스프레드시트 표에서 管理No가 별도 컬럼에 있는 경우
            if '|' in line and not _RE_TOTAL_CELL.search(line):
                _kanri_from_cell: str | None = None
                for _cell in (c.strip() for c in line.split('|')):
                    _m2 = re.match(r'^(\d{7})\s+[^\d\s]', _cell)
                    if _m2:
                        _kanri_from_cell = _m2.group(1)
                        break
                if _kanri_from_cell:
                    current_kanri = _kanri_from_cell
                    current_page  = page_num
                    block_lines   = [line]
                    continue

            if current_kanri is None:
                continue

            block_lines.append(line)

            # ── 計 행 감지 → 블록 확정 ──────────────────────────
            total = _extract_kanri_total(line)
            if total is not None:
                result[current_kanri] = {
                    'page':       current_page,
                    'total':      total,
                    'block_text': '\n'.join(block_lines),
                }
                current_kanri = None
                block_lines   = []

    return result


def _parse_invoice_totals(output_dir: Path) -> dict[str, dict]:
    """form_01용: 請求伝票番号 小計 행 → {invoice_no_prefix: {page, total}} 추출.

    MD 구조:
      | 4-A8001 | 4-A8001-01 | ... | 340,584 |   ← 블록 시작 (cells[1] = 伝票番号)
      | | 4-A8001-02 | ... | 70,656 |
      | | | 請求伝票番号 小計 | ... | 1,522,632 |  ← 소계 행
    """
    result: dict[str, dict] = {}
    current_invoice: str | None = None
    current_page: int = 0

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

        # detail 페이지만 대상
        hint_m = re.search(r'^page_type_hint:\s*(\w+)', content, re.MULTILINE | re.IGNORECASE)
        if hint_m and hint_m.group(1).lower() in ('cover', 'summary', 'payment_form'):
            continue

        for line in content.splitlines():
            if '|' not in line:
                continue

            # 請求伝票番号 감지 (cells[1] = "4-A8001" 형식)
            m_inv = _RE_INVOICE_BLOCK.match(line)
            if m_inv:
                current_invoice = m_inv.group(1)
                current_page = page_num
                continue

            # 請求伝票番号 小計 행 감지
            if current_invoice and _RE_INVOICE_TOTAL.search(line):
                cells = [c.strip() for c in line.split('|')]
                total = None
                for c in reversed(cells):
                    num = c.replace(',', '').replace(' ', '')
                    if num.isdigit() and int(num) > 0:
                        total = int(num)
                        break
                if total is not None:
                    result[current_invoice] = {'page': current_page, 'total': total}
                current_invoice = None

    return result


def _insert_after_kanri(
    base: list[dict], kanri_no: str, new_items: list[dict]
) -> list[dict]:
    """복구 항목을 해당 kanri_no의 마지막 기존 항목 바로 뒤에 삽입한다.
    해당 kanri_no가 없으면 같은 source_page의 마지막 항목 뒤, 그것도 없으면 끝에 추가.
    """
    last_idx = -1
    for i, item in enumerate(base):
        if str(item.get('kanri_no', '')) == str(kanri_no):
            last_idx = i

    if last_idx < 0:
        # kanri_no 미매칭 → source_page 기준 삽입
        insert_page = min(new_items[0].get('source_pages', [9999])) if new_items else 9999
        for i, item in enumerate(base):
            item_page = min(item.get('source_pages', [0])) if item.get('source_pages') else 0
            if item_page <= insert_page:
                last_idx = i

    result = list(base)
    for j, item in enumerate(new_items):
        result.insert(last_idx + 1 + j, item)
    return result


def _load_recovery_cell_map(form_id: str) -> dict | None:
    """form_types.json `row_anchor.recovery_cell_map` 로드 — 결정적 복구의 셀 인덱스 정의.

    없으면 None — 결정적 복구를 건너뛰고 Haiku 폴백으로 처리한다.
    양식별 테이블 레이아웃을 코드에 하드코딩하지 않기 위한 명시적 게이트.
    (예: form_04 = {"product": 1, "nyusuu": 2, "qty": 3, "cond": 5, "biko": 8,
                    "cond_field": "未収条件", "cond_scale": 0.01, ...})
    """
    try:
        path = get_settings().workspace_root / "config" / "form_types.json"
        cfg = json.loads(path.read_text(encoding='utf-8')).get(form_id, {})
        cell_map = (cfg.get('row_anchor') or {}).get('recovery_cell_map')
        return cell_map if isinstance(cell_map, dict) else None
    except Exception:
        logger.warning("recovery_cell_map 로드 실패 (%s) — 결정적 복구 비활성", form_id, exc_info=True)
        return None


def _recover_from_anchor(
    anchor: dict,
    template: dict,
    cell_map: dict | None,
) -> dict | None:
    """단일 row anchor → item 복구. 셀 인덱스는 form_types.json recovery_cell_map에서 온다.

    template item(같은 kanri_no의 다른 item)에서 상속 필드(customer, source_pages 등)를 가져온다.
    cell_map이 없으면(=양식에 recovery_cell_map 미정의) None 반환 — Haiku 폴백.
    amount_hint가 없거나 product 셀이 비어 있어도 None 반환 (Haiku 폴백).
    """
    if not cell_map:
        return None

    line  = anchor['raw_row']
    cells = [c.strip() for c in line.split('|')]
    # 상품 컬럼 위치: anchor가 페이지 헤더에서 탐지한 값을 우선 사용(넓은 헤더 페이지는
    # 선행 컬럼만큼 밀려 있음). 나머지 cell_map 인덱스는 그 차이(offset)만큼 보정한다.
    base_product = int(cell_map.get('product', 1))
    product_idx  = int(anchor.get('product_cell', base_product))
    offset       = product_idx - base_product
    if len(cells) <= product_idx:
        return None

    product = cells[product_idx]
    if not product:
        return None

    amount = anchor.get('amount_hint')
    if amount is None:
        return None

    def _si(s: str) -> int | None:
        s2 = re.sub(r'[個件,. :]', '', s)
        return int(s2) if s2.isdigit() else None

    nyusuu_idx = cell_map.get('nyusuu')
    if nyusuu_idx is not None:
        nyusuu_idx = int(nyusuu_idx) + offset
    nyusuu = _si(cells[nyusuu_idx]) if nyusuu_idx is not None and len(cells) > nyusuu_idx else None

    qty = None
    qty_idx = int(cell_map['qty']) + offset if cell_map.get('qty') is not None else product_idx + 2
    for c in cells[qty_idx:]:
        if '個' in c:
            qty = _si(c)
            break

    biko = ''
    for c in reversed(cells):
        if c == '*':
            biko = '*'
            break
        if c:
            break

    new_item = {k: v for k, v in template.items() if k not in ('product', 'columns', 'row_id')}
    new_item['product']      = product
    new_item['kanri_no']     = anchor['kanri_no_hint']
    new_item['row_id']       = anchor['row_id']
    new_item['source_pages'] = [anchor['page']]
    if anchor.get('jisho_hint'):
        new_item['jisho'] = anchor['jisho_hint']
    if anchor.get('condition_type_hint'):
        new_item['condition_type'] = anchor['condition_type_hint']

    cols = dict(template.get('columns', {}))
    cols['金額'] = amount
    if nyusuu is not None: cols['入数']  = nyusuu
    if qty     is not None: cols['数量'] = qty
    cols['備考'] = biko
    new_item['columns'] = cols
    return new_item


def _check_anchor_coverage(
    anchors:        list[dict],
    items:          list[dict],
    items_by_kanri: dict[str, list[dict]],
    cell_map:       dict | None = None,
) -> dict[str, list[dict]]:
    """row anchor → LLM item[] 커버리지 비교. 누락 row_id를 복구해 반환.

    반환값: {kanri_no: [복구된 item, ...]}
    LLM이 `not_item: true`로 표시한 anchor는 누락으로 간주하지 않는다.

    template 우선순위:
      1순위: 같은 kanri_no의 기존 item
      2순위: 같은 jisho_hint의 다른 item (블록 전체 누락 시 fallback)
    """
    # LLM이 처리한 row_id 집합 (not_item 포함)
    processed_ids: set[str] = set()
    for item in items:
        rid = item.get('row_id')
        if rid:
            processed_ids.add(rid)

    # anchor별 누락 탐지
    missing_by_kanri: dict[str, list[dict]] = {}
    for anchor in anchors:
        if anchor['row_id'] not in processed_ids:
            k = anchor['kanri_no_hint']
            missing_by_kanri.setdefault(k, []).append(anchor)

    if not missing_by_kanri:
        return {}

    if not cell_map:
        # recovery_cell_map 미정의 양식 — 결정적 복구를 시도하지 않는다 (명시적 게이트).
        # 누락 행은 2차 검증(管理No計)의 Haiku 폴백 경로에서 처리된다.
        logger.info(
            "anchor 누락 %d건 감지했으나 recovery_cell_map 미정의 — 결정적 복구 건너뜀 "
            "(form_types.json row_anchor.recovery_cell_map 추가 시 활성화)",
            sum(len(v) for v in missing_by_kanri.values()),
        )
        return {}

    # jisho → 대표 item 인덱스 (2순위 fallback용)
    real_items = [it for it in items if not it.get('not_item')]
    jisho_template: dict[str, dict] = {}
    for it in real_items:
        j = it.get('jisho', '')
        if j and j not in jisho_template:
            jisho_template[j] = it

    # 누락된 anchor를 item으로 복구
    recovered: dict[str, list[dict]] = {}
    for kanri_no, missing_anchors in missing_by_kanri.items():
        templates = items_by_kanri.get(kanri_no, [])
        if not templates:
            # fallback: 같은 지소의 다른 item을 template으로 사용
            jisho_hint = missing_anchors[0].get('jisho_hint', '')
            fallback = jisho_template.get(jisho_hint)
            if fallback:
                templates = [fallback]
        if not templates:
            continue
        new_items = []
        for anchor in missing_anchors:
            item = _recover_from_anchor(anchor, templates[0], cell_map)
            if item:
                new_items.append(item)
        if new_items:
            recovered[kanri_no] = new_items

    return recovered


def _detect_and_fix_ocr_misread(
    existing_items: list[dict],
    kanri_no: str,
    expected_total: int,
    actual_total: int,
) -> tuple[list[dict], str] | None:
    """오버카운팅 케이스: 数量×未収条件 산술 검증으로 金額 오독 항목 탐지 후 수정.

    알고리즘:
      1. diff = expected - actual (음수)
      2. 각 item에 대해 candidate_correct = 金額 + diff (음수를 더해 금액 감소)
      3. candidate_correct > 0 이고 数量 × 未収条件 ≈ candidate_correct 이면 OCR 오독 확정
      4. 해당 item의 金額을 candidate_correct로 수정한 items 반환

    예: 1565505 — 金額=31209(오독), 数量=168, 未収条件=19.1
        168 × 19.1 = 3208.8 ≈ 3209 = candidate_correct (=31209 + (118526-146526))
    """
    diff = expected_total - actual_total  # negative for overcounting
    if diff >= 0:
        return None

    for i, item in enumerate(existing_items):
        cols    = item.get('columns', {})
        kingaku = cols.get('金額')
        if kingaku is None or not isinstance(kingaku, (int, float)):
            continue

        candidate = int(kingaku) + diff  # diff < 0 이므로 감소
        if candidate <= 0:
            continue

        qty    = cols.get('数量')
        mishuu = cols.get('未収条件')
        if qty is None or mishuu is None:
            continue

        try:
            arithmetic = round(float(qty) * float(mishuu))
        except (TypeError, ValueError):
            continue

        if arithmetic == candidate:
            corrected: list[dict] = []
            for j, it in enumerate(existing_items):
                if j == i:
                    new_it = dict(it)
                    new_it['columns'] = dict(cols)
                    new_it['columns']['金額'] = candidate
                    new_it['_ocr_misread_金額'] = int(kingaku)
                    corrected.append(new_it)
                else:
                    corrected.append(it)
            desc = (
                f"管理No {kanri_no}: {item.get('product', '?')} "
                f"金額 {int(kingaku):,} → {candidate:,} "
                f"(数量{qty}×{mishuu}={arithmetic})"
            )
            return corrected, desc

    return None


def _replace_kanri_items(
    items: list[dict], kanri_no: str, fixed_items: list[dict]
) -> list[dict]:
    """특정 kanri_no의 기존 items를 fixed_items로 교체."""
    result:      list[dict] = []
    inserted =   False
    for item in items:
        if str(item.get('kanri_no', '')) == str(kanri_no):
            if not inserted:
                result.extend(fixed_items)
                inserted = True
            # 기존 항목은 버림
        else:
            result.append(item)
    if not inserted:
        result.extend(fixed_items)
    return result


def _try_deterministic_recovery(
    block_text:     str,
    existing_items: list[dict],
    kanri_no:       str,
    expected_total: int,
    actual_total:   int,
    cell_map:       dict | None = None,
) -> list[dict] | None:
    """block_text에서 누락 항목을 결정적으로 추출 시도.

    셀 인덱스·조건 필드는 form_types.json `row_anchor.recovery_cell_map`에서 온다.
    cell_map이 없으면(=양식에 recovery_cell_map 미정의) None 반환 — Haiku 폴백.
    kanri_no가 없는 양식(form_01 등)은 호출 경로가 없으므로 안전.

    알고리즘:
      1. block_text의 각 테이블 행을 파싱해 제품 행 후보를 추출
      2. 이미 추출된 항목(existing_items) 제품명과 비교 — 없는 행만 수집
         (접두사 ※·▷는 비교 시 제거)
      3. 수집된 행의 金額 합산 == diff이면 항목 배열로 반환 (Haiku 불필요)
      4. 합산 불일치 시 None → 기존 Haiku 폴백

    왜 필요한가:
      - 入出荷支店 변경 직후 첫 번째 행은 Sonnet·Haiku 모두 연속 헤더 행으로 오인해 누락
      - 소액(수백円) 행은 두 모델 모두 LLM 바이어스로 생략
      - block_text는 이미 깔끔한 MD 표이므로 Python 파싱이 더 신뢰성 있음
    """
    diff = expected_total - actual_total
    if diff <= 0 or not existing_items:
        return None

    if not cell_map:
        # recovery_cell_map 미정의 양식 — 결정적 추출 불가, Haiku 폴백 (명시적 게이트)
        logger.info(
            "管理No %s — recovery_cell_map 미정의로 결정적 복구 건너뜀 → Haiku 폴백 "
            "(form_types.json row_anchor.recovery_cell_map 추가 시 활성화)", kanri_no,
        )
        return None

    product_idx = int(cell_map.get('product', 1))
    nyusuu_idx  = cell_map.get('nyusuu')
    qty_idx     = cell_map.get('qty')
    cond_idx    = cell_map.get('cond')
    biko_idx    = cell_map.get('biko')
    cond_field  = cell_map.get('cond_field', '未収条件')
    cond_scale  = float(cell_map.get('cond_scale', 1.0))
    skip_pat    = cell_map.get('skip_product_pattern', '管理No|入出荷|得意先|計上場所')

    # ※·▷ 접두사를 제거한 정규화 제품명 집합 (비교용)
    def _norm(p: str) -> str:
        return re.sub(r'^[※▷]+\s*', '', (p or '')).strip()

    existing_norm = {_norm(item.get('product', '')) for item in existing_items}
    template = existing_items[0]

    missing: list[dict] = []
    for line in block_text.splitlines():
        if '|' not in line:
            continue
        cells = [c.strip() for c in line.split('|')]
        if len(cells) < 4:
            continue
        product = cells[product_idx] if len(cells) > product_idx else ''
        if not product:
            continue
        # 헤더·집계 행 제외: 첫 번째 데이터 셀로 판별
        if re.search(skip_pat, product):
            continue
        if _RE_TOTAL_CELL.search(line):
            continue
        if re.match(r'^[*＊]+$', product):
            continue
        # 이미 추출된 제품 제외 (※ 정규화 비교)
        if _norm(product) in existing_norm:
            continue
        # 金額: 마지막 양수 정수 셀
        kingaku = None
        for c in reversed(cells):
            num = c.replace(',', '').replace(' ', '')
            if num.isdigit() and int(num) > 0:
                kingaku = int(num)
                break
        if kingaku is None:
            continue

        def _si(s: str) -> int | None:
            s2 = re.sub(r'[個件,. :]', '', s)
            return int(s2) if s2.isdigit() else None

        nyusuu = _si(cells[nyusuu_idx]) if nyusuu_idx is not None and len(cells) > nyusuu_idx else None
        qty    = _si(cells[qty_idx])    if qty_idx    is not None and len(cells) > qty_idx    else None
        cond_raw = cells[cond_idx].replace(',', '').strip() if cond_idx is not None and len(cells) > cond_idx else ''
        cond   = int(cond_raw) * cond_scale if cond_raw.isdigit() else None
        biko   = cells[biko_idx] if biko_idx is not None and len(cells) > biko_idx else ''

        missing.append({
            'product': product, '金額': kingaku,
            '入数': nyusuu, '数量': qty, cond_field: cond, '備考': biko,
        })

    if not missing:
        return None
    if sum(r['金額'] for r in missing) != diff:
        return None  # 합산 불일치 → Haiku 폴백

    result = []
    for row in missing:
        new_item = {k: v for k, v in template.items() if k not in ('product', 'columns')}
        new_item['product']  = row['product']
        new_item['kanri_no'] = kanri_no
        cols = dict(template.get('columns', {}))
        cols['金額'] = row['金額']
        if row['入数']    is not None: cols['入数']    = row['入数']
        if row['数量']    is not None: cols['数量']    = row['数量']
        if row.get(cond_field) is not None: cols[cond_field] = row[cond_field]
        if '備考' in cols:             cols['備考']    = row['備考']
        new_item['columns'] = cols
        result.append(new_item)
    return result


async def _retry_missing_items(
    doc_id:         str,
    kanri_no:       str,
    block_text:     str,
    existing_items: list[dict],
    expected_total: int,
    actual_total:   int,
    run_id:         str = "",
    cell_map:       dict | None = None,
) -> list[dict]:
    """특정 管理No 블록의 누락 행을 복구. Python 결정적 추출 → 실패 시 Haiku 폴백."""
    diff = expected_total - actual_total

    # ── 결정적 추출 먼저 시도 (Haiku 불필요) ─────────────────────────
    det = _try_deterministic_recovery(
        block_text, existing_items, kanri_no, expected_total, actual_total,
        cell_map=cell_map,
    )
    if det is not None:
        logger.info(
            "[%s] 管理No %s — 결정적 복구 %d행 (%s), Haiku 생략",
            doc_id, kanri_no, len(det),
            ', '.join(f"{r['columns'].get('金額')}円" for r in det),
        )
        return det

    # ── Haiku 폴백 ─────────────────────────────────────────────────────
    settings = get_settings()
    client   = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    existing_example = json.dumps(existing_items[0], ensure_ascii=False, indent=2) if existing_items else "{}"
    prompt = f"""\
管理No: {kanri_no} 블록에서 金額 추출 누락이 발생했습니다.

## 블록 원문
{block_text}

## 이미 추출된 items (金額 합산 = {actual_total:,})
{json.dumps(existing_items, ensure_ascii=False, indent=2)}

## 검증
- 문서의 管理No計: {expected_total:,}
- 현재 합산:       {actual_total:,}
- 누락 금액:       {diff:,}

## 추출 방법 (이 순서를 따른다)
1. "블록 원문"을 위에서 아래로 한 행씩 스캔한다.
2. 管理No 헤더 행, 入出荷支店·得意先·計上場所 헤더 행, *管理No 計: 집계 행은 건너뛴다.
3. **나머지 행은 모두 item 후보다. 블록 첫 번째 상품 행도 반드시 포함한다.**
4. 이미 추출된 items(위 목록)에 있는 제품명이면 제외한다.
5. 남은 행의 金額 합산이 누락 금액({diff:,}円)과 일치하는지 확인한다.

## 출력 규칙 (반드시 준수)
1. 누락된 {diff:,}円의 행만 JSON 배열로 출력. 이미 추출된 항목 포함 금지.
2. 각 항목의 필드 구조는 위 existing items와 **정확히 동일**하게 유지한다. 예:
{existing_example}
3. 수치 데이터는 반드시 `columns` 객체 안에 담는다. `columns` 키를 절대 생략하지 않는다.
4. 최상위에 일본어 컬럼명(管理No, 品名, 金額 등)이나 영어 필드명(product_name, amount, unit_price 등)을 직접 쓰는 것은 절대 금지.
5. ▷, ※ 등 접두사가 붙은 제품명도 `product` 필드에 그대로 보존하고 나머지 구조는 동일.
6. 未収条件: OCR값÷100 (소수점 표기). 数量: 정수.
7. **金額이 아무리 작아도(100円 미만이라도) 블록 원문에 있는 행이면 반드시 출력한다.**
8. JSON 배열만 출력. 설명 없이."""

    message = await client.messages.create(
        model=_VERIFY_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    await accumulate_token_usage(
        doc_id, "phase2_verify",
        message.usage.input_tokens, message.usage.output_tokens,
        _VERIFY_MODEL, run_id=run_id,
    )

    raw = (message.content[0].text if message.content else "").strip()

    # 코드 펜스 제거
    if "```" in raw:
        parts = raw.split("```")
        raw   = parts[1].strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()

    bracket = raw.find('[')
    if bracket > 0:
        raw = raw[bracket:]

    try:
        recovered = json.loads(raw)
        if isinstance(recovered, list):
            valid: list[dict] = []
            for item in recovered:
                cols = item.get('columns')
                if not isinstance(cols, dict):
                    logger.warning(
                        "[%s] 管理No %s — Haiku item에 columns 없음 (폐기): %r",
                        doc_id, kanri_no,
                        {k: v for k, v in item.items() if k != 'columns'},
                    )
                    continue
                kingaku = cols.get('金額')
                if kingaku is None or not isinstance(kingaku, (int, float)) or kingaku <= 0:
                    logger.warning(
                        "[%s] 管理No %s — Haiku item의 columns.金額 무효 (폐기): %r",
                        doc_id, kanri_no, cols,
                    )
                    continue
                valid.append(item)
            return valid
    except json.JSONDecodeError:
        logger.warning("[%s] 管理No %s 복구 JSON 파싱 실패: %r", doc_id, kanri_no, raw[:200])
    return []


def _dedup_after_recovery(items: list[dict]) -> list[dict]:
    """복구 후 content-hash 중복 제거. 유효 7자리 kanri_no 항목을 가비지 항목보다 우선시한다.

    시나리오: Phase 2가 kanri_no를 잘못 파싱한 항목(예: "1568")과
    올바른 항목("1565568")을 동시에 추출 → dedup이 잘못된 쪽을 남김 →
    verify가 올바른 쪽을 복구 추가 → 이중 집계.
    정렬 후 dedup하면 올바른 kanri_no 항목이 먼저 오므로 가비지가 제거된다.
    """
    def _is_valid_kanri(item: dict) -> bool:
        k = str(item.get('kanri_no', '') or '')
        return bool(re.match(r'^\d{7}$', k))

    # 유효 kanri_no 먼저, 나머지 뒤
    items_sorted = sorted(items, key=lambda x: (0 if _is_valid_kanri(x) else 1))

    seen: set[str] = set()
    result: list[dict] = []
    for item in items_sorted:
        inv = (item.get('invoice_no') or '').strip()
        if inv:
            key = inv
        else:
            key = hashlib.md5(
                json.dumps({
                    'kanri_no': item.get('kanri_no', ''),
                    'customer': item.get('customer', ''),
                    'product':  item.get('product', ''),
                    'columns':  item.get('columns', {}),
                }, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


async def run_phase2_verify(
    doc_id:        str,
    form_id:       str,
    output_dir:    Path,
    phase2_result: dict,
    run_id:        str = "",
) -> dict:
    """Phase 2 완료 후 2단계 역산 검증 + 누락 행 핀포인트 재요청.

    1차 검증 (row anchor, form_04 한정):
      phase2_row_anchors.json이 있으면 LLM 누락 row_id를 직접 탐지·복구.
      LLM이 not_item으로 표시한 행은 누락으로 간주하지 않는다.

    2차 검증 (管理No計, 전 양식):
      MD 合計와 items 금액 합산 비교. 미일치 시 결정적 복구 → Haiku 재요청.
      1차에서 복구된 항목이 반영된 후 실행되므로 미일치 건수가 줄어 있다.

    kanri_no 필드가 없는 양식(form_01 등)은 1차만 스킵, 2차는 진행.
    """
    items = phase2_result.get('items', [])

    # 결정적 복구용 셀 인덱스 — form_types.json row_anchor.recovery_cell_map (양식별 정의)
    recovery_cell_map = _load_recovery_cell_map(form_id)

    # ── 1차: row anchor 커버리지 검증 (form_04) ──────────────────────
    anchors = load_row_anchors(output_dir)
    if anchors:
        # ── 1-0: anchor에 없는 row_id를 가진 phantom item 제거 ──────
        # anchor는 Python이 MD에서 확정한 실제 제품 행 목록.
        # LLM이 row_id를 임의로 붙인 phantom item(존재하지 않는 행)을
        # 코드 레벨에서 확실히 제거한다.
        valid_row_ids = {a['row_id'] for a in anchors}
        phantom_removed = 0
        cleaned: list[dict] = []
        for item in items:
            rid = item.get('row_id')
            if rid and rid not in valid_row_ids:
                logger.warning(
                    "[%s] phantom item 제거 — row_id=%s kanri_no=%s product=%s 金額=%s",
                    doc_id, rid, item.get('kanri_no'), item.get('product'),
                    item.get('columns', {}).get('金額'),
                )
                phantom_removed += 1
            else:
                cleaned.append(item)
        if phantom_removed:
            logger.warning("[%s] phantom items %d개 제거", doc_id, phantom_removed)
            items = cleaned
            phase2_result['items'] = items
            # 파일을 즉시 기록 — 이후 anchor 복구가 없어도 phantom 제거가 반영되도록
            out_path = output_dir / "phase2_output.json"
            out_path.write_text(
                json.dumps(phase2_result, ensure_ascii=False, indent=2), encoding='utf-8'
            )

        # not_item 더미 행 제외한 실제 item만으로 kanri 집계
        real_items = [it for it in items if not it.get('not_item')]
        items_by_kanri_for_anchor: dict[str, list[dict]] = {}
        for item in real_items:
            k = item.get('kanri_no')
            if k:
                items_by_kanri_for_anchor.setdefault(k, []).append(item)

        anchor_recovered = _check_anchor_coverage(
            anchors, items, items_by_kanri_for_anchor, cell_map=recovery_cell_map,
        )
        if anchor_recovered:
            total_ar = sum(len(v) for v in anchor_recovered.values())
            logger.info(
                "[%s] row anchor 복구 — %d행 (%s)",
                doc_id, total_ar,
                ', '.join(f"管理No {k}: {len(v)}행" for k, v in anchor_recovered.items()),
            )
            result_items = list(items)
            for kanri_no, new_items in anchor_recovered.items():
                result_items = _insert_after_kanri(result_items, kanri_no, new_items)
            phase2_result['items'] = _dedup_after_recovery(result_items)
            out_path = output_dir / "phase2_output.json"
            out_path.write_text(
                json.dumps(phase2_result, ensure_ascii=False, indent=2), encoding='utf-8'
            )
        else:
            logger.info("[%s] row anchor 검증 — 전체 커버 (%d앵커)", doc_id, len(anchors))

    # not_item 더미를 items에서 제거 (2차 검증 이후 최종 결과물에서 제외)
    phase2_result['items'] = [it for it in phase2_result['items'] if not it.get('not_item')]
    items = phase2_result['items']

    # ── form_01용 2차 역산검증 (請求伝票番号 小計) ─────────────────────────────
    # kanri_no가 없고 invoice_no가 있는 양식(form_01)에서 실행
    if (not any('kanri_no' in it for it in items)
            and any('invoice_no' in it for it in items)):
        invoice_totals = _parse_invoice_totals(output_dir)
        if invoice_totals:
            # items를 請求伝票番号(invoice_no 첫 번째 토큰) 기준으로 집계
            items_by_invoice: dict[str, list[dict]] = {}
            for it in items:
                parts = (it.get('invoice_no') or '').split()
                if parts:
                    items_by_invoice.setdefault(parts[0], []).append(it)

            mismatches = [
                (inv_no, info['page'], info['total'],
                 sum((it.get('columns', {}).get('金額') or 0)
                     for it in items_by_invoice.get(inv_no, [])))
                for inv_no, info in invoice_totals.items()
                if sum((it.get('columns', {}).get('金額') or 0)
                       for it in items_by_invoice.get(inv_no, [])) != info['total']
            ]
            if mismatches:
                logger.warning(
                    "[%s] invoice_no 역산검증 — %d/%d 伝票 불일치: %s",
                    doc_id, len(mismatches), len(invoice_totals),
                    [(inv, exp - act) for inv, _, exp, act in mismatches],
                )
            else:
                logger.info(
                    "[%s] invoice_no 역산검증 — 전체 일치 (%d 伝票)", doc_id, len(invoice_totals)
                )

    # kanri_no 기반 양식이 아니면 管理No計 2차 스킵
    if not any('kanri_no' in item for item in items):
        return phase2_result

    kanri_totals = _parse_kanri_totals(output_dir)
    if not kanri_totals:
        return phase2_result

    # items → kanri_no 별 집계 (1차 복구 반영 후)
    items_by_kanri: dict[str, list[dict]] = {}
    for item in items:
        k = item.get('kanri_no')
        if k:
            items_by_kanri.setdefault(k, []).append(item)

    mismatches: list[tuple] = []
    for kanri_no, info in kanri_totals.items():
        expected  = info['total']
        extracted = sum(
            (i['columns'].get('金額') or 0)
            for i in items_by_kanri.get(kanri_no, [])
        )
        if extracted != expected:
            mismatches.append((kanri_no, info['page'], expected, extracted, info['block_text']))

    report: dict = {
        'doc_id':         doc_id,
        'total_blocks':   len(kanri_totals),
        'mismatch_count': len(mismatches),
        'blocks':         [],
    }

    if not mismatches:
        logger.info("[%s] Phase 2 역산검증 — 전체 일치 (%d 블록)", doc_id, len(kanri_totals))
        _save_report(output_dir, report)
        return phase2_result

    logger.warning(
        "[%s] Phase 2 역산검증 — %d/%d 블록 불일치: %s",
        doc_id, len(mismatches), len(kanri_totals),
        [(k, exp - act) for k, _, exp, act, _ in mismatches],
    )

    recovered_by_kanri:  dict[str, list[dict]]             = {}
    ocr_fixed_by_kanri:  dict[str, list[dict]]             = {}

    for kanri_no, page_no, expected, actual, block_text in mismatches:
        diff         = expected - actual
        block_report = {
            'kanri_no': kanri_no,
            'page':     page_no,
            'expected': expected,
            'actual':   actual,
            'diff':     diff,
            'type':     'undercounting' if diff > 0 else 'overcounting',
            'resolved': False,
            'resolution': None,
        }

        if diff < 0:
            # ── 오버카운팅: OCR 오독 탐지 ──────────────────────────────────
            result_fix = _detect_and_fix_ocr_misread(
                items_by_kanri.get(kanri_no, []), kanri_no, expected, actual
            )
            if result_fix is not None:
                fixed_items, desc = result_fix
                ocr_fixed_by_kanri[kanri_no] = fixed_items
                block_report['resolved']   = True
                block_report['resolution'] = f'ocr_misread_fix: {desc}'
                logger.warning("[%s] OCR 오독 수정 — %s", doc_id, desc)
            else:
                block_report['resolution'] = 'overcounting_unresolved'
                logger.warning(
                    "[%s] 管理No %s — 오버카운팅 원인 불명 "
                    "(actual=%d, expected=%d, diff=%d)",
                    doc_id, kanri_no, actual, expected, diff,
                )
        else:
            # ── 언더카운팅: 누락 행 복구 ────────────────────────────────────
            logger.info(
                "[%s] 管理No %s (p%d) 재요청 — 기대 %d, 추출 %d, 누락 %d",
                doc_id, kanri_no, page_no, expected, actual, diff,
            )
            recovered = await _retry_missing_items(
                doc_id, kanri_no, block_text,
                items_by_kanri.get(kanri_no, []),
                expected, actual, run_id,
                cell_map=recovery_cell_map,
            )
            if recovered:
                for item in recovered:
                    item.setdefault('source_pages', [page_no])
                    item.setdefault('kanri_no', kanri_no)
                logger.info("[%s] 管理No %s — %d행 복구", doc_id, kanri_no, len(recovered))
                recovered_by_kanri[kanri_no] = recovered
                block_report['resolved']   = True
                block_report['resolution'] = f'recovered {len(recovered)} items'
                block_report['recovered_金額'] = [
                    r.get('columns', {}).get('金額') for r in recovered
                ]
            else:
                block_report['resolution'] = 'recovery_failed'
                logger.warning(
                    "[%s] 管理No %s — 복구 실패 (누락 %d 잔존)",
                    doc_id, kanri_no, diff,
                )

        report['blocks'].append(block_report)

    if recovered_by_kanri or ocr_fixed_by_kanri:
        result_items = list(items)

        # OCR 오독: 기존 항목 金額 교체
        for kanri_no, fixed_items in ocr_fixed_by_kanri.items():
            result_items = _replace_kanri_items(result_items, kanri_no, fixed_items)

        # 언더카운팅: 누락 행 삽입
        for kanri_no, new_items in recovered_by_kanri.items():
            result_items = _insert_after_kanri(result_items, kanri_no, new_items)

        # 복구 후 재dedup
        phase2_result['items'] = _dedup_after_recovery(result_items)
        out_path = output_dir / "phase2_output.json"
        out_path.write_text(
            json.dumps(phase2_result, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        logger.info(
            "[%s] Phase 2 역산검증 완료 — 복구 %d행, OCR수정 %d블록",
            doc_id,
            sum(len(v) for v in recovered_by_kanri.values()),
            len(ocr_fixed_by_kanri),
        )

    _save_report(output_dir, report)
    return phase2_result


def _save_report(output_dir: Path, report: dict) -> None:
    """검증 리포트를 phase2_verify_report.json 으로 저장."""
    try:
        path = output_dir / "phase2_verify_report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        logger.info("검증 리포트 저장: %s", path)
    except Exception as exc:
        logger.warning("검증 리포트 저장 실패: %s", exc)
