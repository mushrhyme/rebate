"""
rebate_db_v2 업로드 스크립트
phase3_output.json + phase4_output.json → documents / pages / items 삽입

사용법:
  python scripts/upload_to_db.py --doc 日本アクセスＣＶＳ [--dry-run]
"""
import argparse, json, re
from pathlib import Path
from collections import defaultdict

import psycopg2
import psycopg2.extras

BASE     = Path(__file__).parent.parent
DB_DSN   = "host=localhost dbname=rebate_db_v2 user=postgres"

_REMAP_PATH = BASE / "mappings" / "form_columns.json"
_COLUMN_REMAP: dict[str, dict[str, str]] = json.loads(_REMAP_PATH.read_text(encoding="utf-8"))


def build_columns(p3_cols: dict, form_id: str) -> dict:
    remap = _COLUMN_REMAP.get(form_id, {})
    if not remap:
        return p3_cols
    return {remap.get(k, k): v for k, v in p3_cols.items()}


def build_net_result(p4_row: dict) -> dict:
    return {
        "受注先":         p4_row.get("受注先"),
        "受注先コード":   p4_row.get("受注先コード"),
        "スーパー":       p4_row.get("スーパー"),
        "商品名":         p4_row.get("商品名"),
        "仕切":           p4_row.get("仕切"),
        "仕切売上":       p4_row.get("仕切売上"),
        "本部長":         p4_row.get("本部長"),
        "NET":            p4_row.get("NET"),
        "net_lt_honbu":   p4_row.get("net_lt_honbu", False),
        "タイプ":         p4_row.get("タイプ"),
        "条件1（ケース）": p4_row.get("条件1（ケース）"),
        "条件2（ケース）": p4_row.get("条件2（ケース）"),
    }


def parse_cover_md(md: str) -> dict | None:
    """커버 페이지 MD에서 請求書ヘッダー + 入出荷支店別集計 추출."""
    def to_int(s):
        if not s: return None
        cleaned = re.sub(r"[^\d]", "", s)
        return int(cleaned) if cleaned else None

    m = re.search(r"請求書\s*No\.\s*[:：]\s*(\S+)", md)
    invoice_no = m.group(1) if m else None

    honbai = zeizei = gokei = None

    # Format A: 리스트형 "- 本体合計金額: X円"
    m = re.search(r"-\s*本体合計金額\s*[:：]\s*([\d,]+)", md)
    if m: honbai = to_int(m.group(1))
    m = re.search(r"-\s*消費税金額[^:：\n]*[:：]\s*([\d,]+)", md)
    if m: zeizei = to_int(m.group(1))
    m = re.search(r"-\s*合計ご請求金額\s*[:：]\s*([\d,]+)", md)
    if m: gokei = to_int(m.group(1))

    # Format B & C: 테이블형
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("|"): continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cols or not cols[0]: continue

        if cols[0] == "本体合計金額":
            if len(cols) >= 2 and "消費税" in cols[1]:
                # Format B: 헤더행 → 다음 데이터행
                for j in range(i + 1, min(i + 4, len(lines))):
                    if "---" in lines[j]: continue
                    dcols = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                    nums = [to_int(c) for c in dcols if re.search(r"\d", c) and "%" not in c]
                    nums = [n for n in nums if n is not None]
                    if nums:
                        if honbai is None: honbai = nums[0]
                        if zeizei is None and len(nums) >= 2: zeizei = nums[1]
                        if gokei is None and len(nums) >= 3: gokei = nums[-1]
                    break
            else:
                # Format C: 행 키 형식 "| 本体合計金額 | X | |"
                nums = [to_int(c) for c in cols[1:] if re.match(r"[\d,]+$", c)]
                if nums and honbai is None: honbai = nums[0]
        elif cols[0] == "消費税金額" and zeizei is None:
            nums = [to_int(c) for c in cols[1:] if re.match(r"[\d,]+$", c)]
            if nums: zeizei = nums[0]
        elif cols[0] == "合計ご請求金額" and gokei is None:
            nums = [to_int(c) for c in cols[1:] if re.match(r"[\d,]+$", c)]
            if nums: gokei = nums[0]

    # 入出荷支店別 테이블
    branches, in_table, header_passed = [], False, False
    for line in lines:
        if re.search(r"入出荷支店.*(本体金額|集計|明細)", line):
            in_table = True; header_passed = False; continue
        if in_table:
            if "---" in line: header_passed = True; continue
            if line.startswith("|") and header_passed:
                cols = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cols) >= 4 and cols[0] and cols[0] != "入出荷支店":
                    amt = to_int(cols[3])
                    if amt is not None:
                        branches.append({"name": cols[0], "amount": amt})
            elif header_passed and line.strip() and not line.startswith("|"):
                break

    if honbai is None and not branches:
        return None

    return {"invoice_no": invoice_no, "honbai": honbai, "zeizei": zeizei,
            "gokei": gokei, "branches": branches}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc",     required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    doc_dir  = BASE / "extracted" / args.doc
    p3_path  = doc_dir / "phase3_output.json"
    p4_path  = doc_dir / "phase4_output.json"

    for p in (p3_path, p4_path):
        if not p.exists():
            raise SystemExit(f"[오류] {p} 없음")

    with open(p3_path, encoding="utf-8") as f:
        p3 = json.load(f)
    with open(p4_path, encoding="utf-8") as f:
        p4 = json.load(f)

    form_id      = p3["form_id"]
    hatsu_month  = p3["hatsu_month"]
    items_p3     = p3["items"]
    rows_p4      = p4["rows"]
    pdf_filename = args.doc

    year, month = map(int, hatsu_month.split("."))

    p2_path = doc_dir / "phase2_output.json"
    pages_meta: list[dict] = []
    if p2_path.exists():
        with open(p2_path, encoding="utf-8") as f:
            p2 = json.load(f)
        pages_meta = p2.get("pages", [])

    page_md_contents: dict[int, str] = {}
    for fn in sorted(doc_dir.iterdir()):
        if fn.name.startswith("page_") and fn.suffix == ".md":
            pg_num = int(fn.stem.replace("page_", ""))
            page_md_contents[pg_num] = fn.read_text(encoding="utf-8")

    total_pages = max(page_md_contents.keys()) if page_md_contents else 0

    if len(items_p3) != len(rows_p4):
        raise SystemExit(
            f"[오류] items_p3({len(items_p3)}) ≠ rows_p4({len(rows_p4)}) — 순서 불일치"
        )

    print(f"  문서: {pdf_filename}  ({form_id}, {hatsu_month})")
    print(f"  총 페이지: {total_pages}  아이템: {len(items_p3)}")

    if args.dry_run:
        print("  [DRY-RUN] DB 쓰기 없음. 종료.")
        return

    conn = psycopg2.connect(DB_DSN, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn:
            with conn.cursor() as cur:

                # ── 1. documents ──────────────────────────────────────────────
                cur.execute("""
                    INSERT INTO documents (pdf_filename, form_type, data_year, data_month,
                                          total_pages, analyzed_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (pdf_filename) DO UPDATE
                        SET form_type   = EXCLUDED.form_type,
                            data_year   = EXCLUDED.data_year,
                            data_month  = EXCLUDED.data_month,
                            total_pages = EXCLUDED.total_pages,
                            analyzed_at = NOW()
                """, (pdf_filename, form_id, year, month, total_pages))
                print(f"  ✅ documents 삽입/갱신")

                # ── 2. pages ──────────────────────────────────────────────────
                role_map = {pm["page"]: pm.get("role", "detail") for pm in pages_meta}
                cover_count = 0

                for pg_num, md_text in sorted(page_md_contents.items()):
                    role = role_map.get(pg_num, "detail")
                    cover_data = None
                    if role == "cover":
                        cover_data = parse_cover_md(md_text)
                        if cover_data:
                            cover_count += 1

                    cur.execute("""
                        INSERT INTO pages (pdf_filename, page_number, page_role, page_md, cover_data)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (pdf_filename, page_number) DO UPDATE
                            SET page_role  = EXCLUDED.page_role,
                                page_md    = EXCLUDED.page_md,
                                cover_data = EXCLUDED.cover_data
                    """, (pdf_filename, pg_num, role, md_text,
                          json.dumps(cover_data, ensure_ascii=False) if cover_data else None))

                print(f"  ✅ pages 삽입/갱신 ({len(page_md_contents)}페이지, 커버 {cover_count}건)")

                # ── 3. items ──────────────────────────────────────────────────
                cur.execute("DELETE FROM items WHERE pdf_filename = %s", (pdf_filename,))

                order_counter: dict[int, int] = defaultdict(int)

                for p3_item, p4_row in zip(items_p3, rows_p4):
                    src_pages  = p3_item.get("source_pages", [])
                    pg_num     = src_pages[0] if src_pages else 1
                    order_counter[pg_num] += 1
                    item_order = order_counter[pg_num]

                    cols_json  = build_columns(p3_item.get("columns", {}), form_id)
                    net_json   = build_net_result(p4_row)

                    cur.execute("""
                        INSERT INTO items (
                            pdf_filename, page_number, item_order,
                            invoice_no, customer_ocr, product_ocr,
                            retailer_code, dist_code, product_code,
                            item_type, unconfirmed,
                            columns, net_result
                        ) VALUES (%s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s, %s,%s)
                    """, (
                        pdf_filename, pg_num, item_order,
                        p3_item.get("invoice_no"),
                        p3_item.get("customer_ocr"),
                        p3_item.get("product_ocr"),
                        p3_item.get("retailer_code"),
                        p3_item.get("dist_code"),
                        p3_item.get("product_code"),
                        p4_row.get("タイプ"),
                        p3_item.get("unconfirmed", False),
                        json.dumps(cols_json, ensure_ascii=False),
                        json.dumps(net_json, ensure_ascii=False),
                    ))

                print(f"  ✅ items 삽입 ({len(items_p3)}건)")

        print(f"\n  → rebate_db_v2 업로드 완료: {pdf_filename}")

    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
