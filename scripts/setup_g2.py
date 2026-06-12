#!/usr/bin/env python3
"""G2 초기 설정: Drive 폴더 구조 생성 + Sheets results 탭 생성.

실행: python scripts/setup_g2.py [--parent-id FOLDER_ID]

--parent-id 미지정 시: rebate-inbox / rebate-archive 를 My Drive 루트에 직접 생성.
지정 시: 해당 폴더 아래에 생성.

결과: DRIVE_INBOX_FOLDER_ID / DRIVE_ARCHIVE_FOLDER_ID 값 출력
  → backend/.env 에 추가 후 EC2 재배포
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

WORKSPACE = Path(__file__).parents[1]
RESULTS_TAB = "results"
RESULTS_HEADERS = ["doc_id", "発行処", "発行月", "소매처코드", "소매처명", "NET金額", "処理日時"]


def _run(cmd: list[str], check: bool = True) -> dict:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        print(f"  오류: {err}")
        if check:
            sys.exit(1)
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"_raw": result.stdout.strip()}


def _create_drive_folder(name: str, parent_id: str = "") -> str:
    """Drive 폴더 생성 (이미 있으면 기존 ID 반환).
    parent_id 빈 값 → My Drive 루트에 생성.
    """
    # 존재 확인
    q_parts = [f"name = '{name}'", "mimeType = 'application/vnd.google-apps.folder'", "trashed = false"]
    if parent_id:
        q_parts.append(f"'{parent_id}' in parents")
    res = _run(["google-cli", "drive", "list", "-q", " and ".join(q_parts), "-l", "5", "-f", "json"])
    files = res.get("data", [])
    if files:
        fid = files[0]["id"]
        print(f"  (기존) {name}/ → {fid}")
        return fid

    # 생성
    cmd = ["google-cli", "drive", "mkdir", name, "-f", "json"]
    if parent_id:
        cmd += ["-p", parent_id]
    res = _run(cmd)
    data = res.get("data", res)
    fid = data.get("id", "") if isinstance(data, dict) else ""
    if not fid:
        print(f"  ERROR: {name}/ 생성 후 ID 확인 불가. 출력: {res}")
        sys.exit(1)
    print(f"  (신규) {name}/ → {fid}")
    return fid


def _ensure_results_tab(sheets_id: str) -> None:
    """results 탭이 없으면 생성하고 헤더를 삽입한다."""
    res = _run(["google-cli", "sheets", "info", sheets_id, "-f", "json"])
    sheets_data = res.get("data", {})
    existing_tabs = {s["properties"]["title"] for s in sheets_data.get("sheets", [])}

    if RESULTS_TAB not in existing_tabs:
        batch_body = json.dumps({
            "requests": [{"addSheet": {"properties": {"title": RESULTS_TAB}}}]
        })
        _run(["google-cli", "sheets", "api", "spreadsheets.batchUpdate",
              "-p", f"spreadsheetId={sheets_id}", "--body", batch_body])
        print(f"  (신규) Sheets 탭 '{RESULTS_TAB}' 생성")

        header_json = json.dumps([RESULTS_HEADERS])
        _run(["google-cli", "sheets", "write", sheets_id, f"{RESULTS_TAB}!A1:G1",
              "-v", header_json])
        print(f"  헤더 삽입: {RESULTS_HEADERS}")
    else:
        res2 = _run(["google-cli", "sheets", "read", sheets_id, f"{RESULTS_TAB}!A1:G1", "-f", "json"])
        rows = res2.get("data", [])
        first = rows[0] if rows else []
        if first == RESULTS_HEADERS:
            print(f"  (기존) Sheets 탭 '{RESULTS_TAB}' — 헤더 일치")
        elif first:
            print(f"  경고: 기존 헤더 {first}")
            print(f"  예상 헤더: {RESULTS_HEADERS}")
        else:
            print(f"  (기존) Sheets 탭 '{RESULTS_TAB}' 확인됨 (헤더 행 없음)")


def _read_env(key: str) -> str:
    env_file = WORKSPACE / "backend" / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="G2 Drive/Sheets 초기 설정")
    parser.add_argument(
        "--parent-id",
        default="",
        help="Drive 상위 폴더 ID (미지정 시 My Drive 루트에 생성)",
    )
    parser.add_argument(
        "--sheets-id",
        default=_read_env("GOOGLE_SHEETS_MAPPINGS_ID"),
        help="스프레드시트 ID (기본: .env GOOGLE_SHEETS_MAPPINGS_ID)",
    )
    args = parser.parse_args()

    if not args.sheets_id:
        print("ERROR: --sheets-id 또는 .env GOOGLE_SHEETS_MAPPINGS_ID 필요")
        sys.exit(1)

    parent_label = args.parent_id if args.parent_id else "My Drive 루트"
    print("=" * 52)
    print("  G2 초기 설정")
    print(f"  상위 폴더  : {parent_label}")
    print(f"  Sheets ID  : {args.sheets_id}")
    print("=" * 52)

    print("\n[1/2] Drive 폴더 생성...")
    inbox_id = _create_drive_folder("rebate-inbox", args.parent_id)
    archive_id = _create_drive_folder("rebate-archive", args.parent_id)

    print("\n[2/2] Sheets results 탭 설정...")
    _ensure_results_tab(args.sheets_id)

    print("\n" + "=" * 52)
    print("  ✅  G2 설정 완료!")
    print("=" * 52)
    print("\nbackend/.env 에 아래 항목을 추가(주석 해제)하세요:\n")
    if args.parent_id:
        print(f"  DRIVE_ROOT_FOLDER_ID={args.parent_id}")
    print(f"  DRIVE_INBOX_FOLDER_ID={inbox_id}")
    print(f"  DRIVE_ARCHIVE_FOLDER_ID={archive_id}")
    print()
    print("설정 후 EC2 재배포하면 G2 활성화됩니다.")
    print("rebate-inbox/ 에 PDF 업로드 → 300초 이내 파이프라인 자동 트리거됩니다.\n")


if __name__ == "__main__":
    main()
