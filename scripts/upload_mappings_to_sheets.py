"""CSV 마스터 파일을 Google Sheets에 업로드."""
import csv
import pickle
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SPREADSHEET_ID = "1UVoETT84o5LDiLEu9Xff4pAUulmXaajz6jvfgYDJenc"
TOKEN_PATH = Path.home() / ".google-cli" / "token.pickle"
MAPPINGS_DIR = Path(__file__).parent.parent / "mappings"

SHEETS = [
    ("ocr_retailer",   "ocr_retailer.csv"),
    ("ocr_product",    "ocr_product.csv"),
    ("ocr_dist",       "ocr_dist.csv"),
    ("retail_user",    "retail_user.csv"),
    ("unit_price",     "unit_price.csv"),
    ("domae_retail_1", "domae_retail_1.csv"),
]


def load_creds():
    with open(TOKEN_PATH, "rb") as f:
        creds = pickle.load(f)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def read_csv(path: Path) -> list[list[str]]:
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.reader(f))


def get_existing_sheets(service) -> dict[str, int]:
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}


def add_sheets(service, names: list[str]):
    requests = [
        {"addSheet": {"properties": {"title": name}}}
        for name in names
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": requests},
    ).execute()
    print(f"  탭 생성: {', '.join(names)}")


def upload_sheet(service, tab_name: str, rows: list[list[str]]):
    range_ = f"{tab_name}!A1"
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=range_,
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()
    print(f"  {tab_name}: {len(rows)}행 업로드 완료")


def main():
    creds = load_creds()
    service = build("sheets", "v4", credentials=creds)

    existing = get_existing_sheets(service)
    print(f"기존 탭: {list(existing.keys())}")

    # 없는 탭만 생성
    to_create = [name for name, _ in SHEETS if name not in existing]
    if to_create:
        add_sheets(service, to_create)

    # 각 CSV 업로드
    for tab_name, csv_file in SHEETS:
        path = MAPPINGS_DIR / csv_file
        if not path.exists():
            print(f"  {csv_file} 없음 — 건너뜀")
            continue
        rows = read_csv(path)
        upload_sheet(service, tab_name, rows)

    print("\n완료. 스프레드시트 확인:")
    print(f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit")


if __name__ == "__main__":
    main()
