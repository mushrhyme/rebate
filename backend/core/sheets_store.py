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
        try:
            result = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self._id, range=f"{tab}!A1:ZZ")
                .execute()
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Sheets 탭 '%s' 읽기 실패 — 빈 결과 반환: %s", tab, e)
            return []
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

    def upsert_row(self, csv_filename: str, key_cols: list[int], values: list[str]) -> None:
        """키 컬럼 기준 upsert: 존재하면 해당 행 업데이트, 없으면 추가."""
        tab = self.tab_for(csv_filename)
        if tab is None:
            return
        result = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._id, range=f"{tab}!A1:ZZ")
            .execute()
        )
        raw = result.get("values", [])
        key_values = [values[i] for i in key_cols]
        found_sheet_row: int | None = None  # 1-indexed Sheets 행 번호
        if len(raw) > 1:
            headers = raw[0]
            for i, row in enumerate(raw[1:]):
                padded = row + [""] * (len(headers) - len(row))
                if [padded[k] for k in key_cols] == key_values:
                    found_sheet_row = i + 2  # +1 헤더, +1 1-indexed
                    break
        if found_sheet_row is not None:
            self._service.spreadsheets().values().update(
                spreadsheetId=self._id,
                range=f"{tab}!A{found_sheet_row}",
                valueInputOption="RAW",
                body={"values": [values]},
            ).execute()
        else:
            self._service.spreadsheets().values().append(
                spreadsheetId=self._id,
                range=f"{tab}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [values]},
            ).execute()
        self._cache.pop(tab, None)

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

    def append_to_tab(self, tab_name: str, values: list[str]) -> None:
        """TAB_MAP 등록 없이 임의 탭에 행 추가 (results 탭 등 전용)."""
        self._service.spreadsheets().values().append(
            spreadsheetId=self._id,
            range=f"{tab_name}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        ).execute()

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
_init_failed: bool = False


def get_sheets_store() -> Optional[SheetsStore]:
    """설정된 경우 SheetsStore 싱글턴 반환, 미설정이거나 초기화 실패 시 None."""
    global _instance, _init_failed
    if _instance is not None:
        return _instance
    if _init_failed:
        return None
    from .config import get_settings
    sid = get_settings().google_sheets_mappings_id
    if not sid:
        return None
    try:
        _instance = SheetsStore(sid)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "SheetsStore 초기화 실패 — 로컬 CSV로 fallback: %s", e
        )
        _init_failed = True
        return None
    return _instance
