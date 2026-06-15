"""회귀 테스트 입력 박제 생성기 — Sheets/extracted 의존을 끊기 위한 self-contained 번들 작성.

phase4 회귀 테스트는 NET 계산이 코드 변경으로 바뀌는지만 잡아야 한다.
그런데 run()이 마스터(unit_price/retail_user)를 살아있는 Sheets에서 읽고,
입력(phase3_output.json)을 로컬 extracted/에서 읽어, 네트워크·Sheets 변경·로컬 상태에
테스트가 흔들렸다. 이 스크립트는 한 시점의 입력+참조 마스터 행을
tests/fixtures/regression/{case}/ 아래로 박제한다.

사용:
  python scripts/gen_regression_fixture.py <case_id> <doc_id>
  예) python scripts/gen_regression_fixture.py form_01 4월伊藤忠食品株式会社登録番号T2120001077362

생성물:
  tests/fixtures/regression/<case>/extracted/<doc_id>/phase3_output.json  (NET 입력)
  tests/fixtures/regression/<case>/extracted/<doc_id>/phase2_output.json  (cover/summary totals → xv)
  tests/fixtures/regression/<case>/mappings/unit_price.csv   (참조 제품코드 행만)
  tests/fixtures/regression/<case>/mappings/retail_user.csv  (참조 소매처/판매처코드 행만)

phase2_output.json도 박제해야 교차검증(xv)이 실제로 계산·비교된다.
누락하면 cover_totals/summary가 비어 xv가 0이 되고, 테스트가 '조용히 통과'한다.
"""
import csv
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    case_id, doc_id = sys.argv[1], sys.argv[2]

    src_p3 = BASE / "extracted" / doc_id / "phase3_output.json"
    if not src_p3.exists():
        sys.exit(f"[오류] {src_p3} 없음 — 로컬 분석 산출물이 있어야 박제 가능")
    p3 = json.loads(src_p3.read_text(encoding="utf-8"))

    items = p3.get("items", [])
    prod_codes = {i.get("product_code") for i in items if i.get("product_code")}
    ret_codes  = {i.get("retailer_code") for i in items if i.get("retailer_code")}
    dist_codes = {i.get("dist_code") for i in items if i.get("dist_code")}

    import scripts.phase4_calc as pc
    if pc._sheets_store is None:
        print("[경고] Sheets 비활성 — 로컬 mappings/ CSV에서 마스터를 읽습니다.")

    up = pc.load_csv_dict("unit_price.csv", "제품코드")
    ru_full = pc.load_csv_dict("retail_user.csv", "소매처코드")
    # retail_user는 소매처코드/판매처코드 양쪽으로 조회되므로 둘 다 커버하도록 원본 행에서 필터
    ru_rows = (pc._sheets_store.read_csv("retail_user.csv") if pc._sheets_store
               else _read_local_csv(BASE / "mappings" / "retail_user.csv"))
    up_rows = (pc._sheets_store.read_csv("unit_price.csv") if pc._sheets_store
               else _read_local_csv(BASE / "mappings" / "unit_price.csv"))

    up_keep = [r for r in up_rows if r.get("제품코드") in prod_codes]
    ru_keep = [r for r in ru_rows
               if r.get("소매처코드") in ret_codes or r.get("판매처코드") in dist_codes]

    missing_prod = prod_codes - {r.get("제품코드") for r in up_keep}
    if missing_prod:
        print(f"[경고] unit_price에 없는 제품코드 {len(missing_prod)}건: {sorted(missing_prod)[:5]}")

    bundle = BASE / "tests" / "fixtures" / "regression" / case_id
    (bundle / "extracted" / doc_id).mkdir(parents=True, exist_ok=True)
    (bundle / "mappings").mkdir(parents=True, exist_ok=True)

    (bundle / "extracted" / doc_id / "phase3_output.json").write_text(
        json.dumps(p3, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # phase2_output.json: cover/summary totals → 교차검증(xv) 입력. 있으면 함께 박제.
    src_p2 = BASE / "extracted" / doc_id / "phase2_output.json"
    if src_p2.exists():
        (bundle / "extracted" / doc_id / "phase2_output.json").write_text(
            src_p2.read_text(encoding="utf-8"), encoding="utf-8"
        )
    else:
        print("[경고] phase2_output.json 없음 — xv가 비어 회귀에서 검증되지 않습니다.")
    _write_csv(bundle / "mappings" / "unit_price.csv", up_rows[0:0] or up_keep, up_keep)
    _write_csv(bundle / "mappings" / "retail_user.csv", ru_keep, ru_keep)

    print(f"✅ 박제 완료: {bundle}")
    print(f"   phase3 items={len(items)}  unit_price={len(up_keep)}행  retail_user={len(ru_keep)}행")


def _read_local_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, _sample: list[dict], rows: list[dict]) -> None:
    if not rows:
        sys.exit(f"[오류] {path.name} 박제할 행이 0개 — 참조 코드 매칭 실패")
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
