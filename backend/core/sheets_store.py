"""Google Sheets 기반 매핑 CSV 저장소."""
import io
import csv as _csv
import pickle
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from googleapiclient.discovery import build

_TOKEN_PATH = Path.home() / ".google-cli" / "token.pickle"
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# csv 파일명 → Sheets 탭 이름
TAB_MAP: dict[str, str] = {
    "ocr_retailer.csv":   "ocr_retailer",
    "ocr_product.csv":    "ocr_product",
    "ocr_dist.csv":       "ocr_dist",
    "retail_user.csv":    "retail_user",
    "unit_price.csv":     "unit_price",
    "domae_retail_1.csv": "domae_retail_1",
}


class SheetsStore:
    """Google Sheets 기반 매핑 데이터 저장소.

    - 읽기: 탭별로 프로세스 내 메모리 캐시 (1회 fetch 후 재사용)
    - 쓰기(캐시): Sheets에 행 추가 후 메모리 캐시 무효화
    """

    def __init__(self, spreadsheet_id: str):
        self._id = spreadsheet_id
        self._cache: dict[str, list[dict]] = {}
        self._service = self._build_service()

    def _build_service(self):
        from .config import get_settings
        sa_path = Path(get_settings().workspace_root) / "service_account.json"
        if sa_path.exists():
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(
                str(sa_path), scopes=_SCOPES
            )
        else:
            with open(_TOKEN_PATH, "rb") as f:
                creds = pickle.load(f)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
        return build("sheets", "v4", credentials=creds)

    def tab_for(self, csv_filename: str) -> Optional[str]:
        return TAB_MAP.get(csv_filename)

    def read_csv(self, csv_filename: str) -> list[dict]:
        """CSV 파일명에 대응하는 Sheets 탭을 list[dict]로 반환 (메모리 캐시)."""
        tab = self.tab_for(csv_filename)
        if tab is None:
            return []
        if tab not in self._cache:
            self._cache[tab] = self._fetch(tab)
        return self._cache[tab]

    def _fetch(self, tab: str) -> list[dict]:
        result = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._id, range=f"{tab}!A1:ZZ")
            .execute()
        )
        raw = result.get("values", [])
        if not raw:
            return []
        headers = raw[0]
        rows = []
        for row in raw[1:]:
            padded = row + [""] * (len(headers) - len(row))
            rows.append(dict(zip(headers, padded)))
        return rows

    def to_csv_text(self, csv_filename: str) -> str:
        """Sheets 탭 데이터를 CSV 텍스트로 변환 (Claude 프롬프트 삽입용)."""
        rows = self.read_csv(csv_filename)
        if not rows:
            return ""
        buf = io.StringIO()
        w = _csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue()

    def append_row(self, csv_filename: str, values: list[str]) -> None:
        """캐시 탭에 행 1개 추가 후 메모리 캐시 무효화."""
        tab = self.tab_for(csv_filename)
        if tab is None:
            return
        self._service.spreadsheets().values().append(
            spreadsheetId=self._id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        ).execute()
        self._cache.pop(tab, None)

    def write_all(self, csv_filename: str, rows: list[dict], fieldnames: list[str]) -> None:
        """시트 전체를 덮어씀 (헤더 + 모든 행)."""
        tab = self.tab_for(csv_filename)
        if tab is None:
            return
        values = [fieldnames] + [[row.get(f, "") for f in fieldnames] for row in rows]
        self._service.spreadsheets().values().clear(
            spreadsheetId=self._id,
            range=f"{tab}!A1:ZZ",
        ).execute()
        self._service.spreadsheets().values().update(
            spreadsheetId=self._id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()
        self._cache.pop(tab, None)

    def invalidate(self, csv_filename: str) -> None:
        tab = self.tab_for(csv_filename)
        if tab:
            self._cache.pop(tab, None)


_instance: Optional[SheetsStore] = None


def get_sheets_store() -> Optional[SheetsStore]:
    """설정된 경우 SheetsStore 싱글턴 반환, 미설정이면 None."""
    global _instance
    if _instance is not None:
        return _instance
    from .config import get_settings
    sid = get_settings().google_sheets_mappings_id
    if not sid:
        return None
    _instance = SheetsStore(sid)
    return _instance
