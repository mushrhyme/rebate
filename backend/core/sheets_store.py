"""Google Sheets 기반 매핑 CSV 저장소."""
import io
import csv as _csv
import os
import pickle
import time
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import AuthorizedSession, Request

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


_SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

# 운영상 절대 비어 있으면 안 되는 마스터 탭 — 빈 결과 = 토큰/네트워크 장애로 간주
MASTER_CSV_FILES = ("unit_price.csv", "retail_user.csv", "domae_retail_1.csv")


class SheetsUnavailableError(RuntimeError):
    """Sheets 마스터를 읽을 수 없음 (토큰 만료·네트워크 장애 등).

    빈 마스터로 분석을 '조용히' 진행하지 않기 위해 명시적으로 던진다."""


class SheetsStore:
    """Google Sheets 기반 매핑 데이터 저장소.

    - 읽기: 탭별로 프로세스 내 메모리 캐시 (TTL 만료 시 재조회 —
      현업이 Sheets를 직접 수정해도 백엔드 재시작 없이 반영되도록)
    - 쓰기(캐시): Sheets에 행 추가 후 메모리 캐시 무효화
    Transport: requests (urllib3) — httplib2 C SSL heap corruption 회피
    """

    def __init__(self, spreadsheet_id: str, ttl_seconds: float | None = None):
        self._id = spreadsheet_id
        if ttl_seconds is None:
            ttl_seconds = float(os.getenv("SHEETS_CACHE_TTL", "300"))
        self._ttl = ttl_seconds
        # tab → (cached_at_monotonic, rows)
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        # tab → (실패 시각(monotonic), 메시지) — fetch 실패를 조용히 삼키지 않고 노출
        self._fetch_errors: dict[str, tuple[float, str]] = {}
        self._session = self._build_session()

    def _build_session(self) -> AuthorizedSession:
        with open(_TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return AuthorizedSession(creds)

    def _get(self, path: str, **params) -> dict:
        url = f"{_SHEETS_BASE}/{self._id}/{path}"
        resp = self._session.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, body: dict, **params) -> dict:
        url = f"{_SHEETS_BASE}/{self._id}/{path}"
        resp = self._session.put(url, json=body, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict, **params) -> dict:
        url = f"{_SHEETS_BASE}/{self._id}/{path}"
        resp = self._session.post(url, json=body, params=params)
        resp.raise_for_status()
        return resp.json()

    def tab_for(self, csv_filename: str) -> Optional[str]:
        return TAB_MAP.get(csv_filename)

    def read_csv(self, csv_filename: str, required: bool = False) -> list[dict]:
        """CSV 파일명에 대응하는 Sheets 탭을 list[dict]로 반환 (TTL 메모리 캐시).

        required=True: 결과가 비고 직전 fetch가 실패했으면 SheetsUnavailableError를 던진다.
        마스터(unit_price 등)를 '빈 결과로 조용히' 쓰지 않기 위한 가드.
        """
        tab = self.tab_for(csv_filename)
        if tab is None:
            return []
        entry = self._cache.get(tab)
        now = time.monotonic()
        if entry is None or (now - entry[0]) >= self._ttl:
            self._cache[tab] = (now, self._fetch(tab))
        rows = self._cache[tab][1]
        if required and not rows and tab in self._fetch_errors:
            _, msg = self._fetch_errors[tab]
            raise SheetsUnavailableError(f"Sheets 탭 '{tab}' 읽기 실패: {msg}")
        return rows

    def probe(self) -> tuple[bool, str]:
        """토큰·연결 상태를 가볍게 검증 (spreadsheet 메타데이터 1회 GET).

        반환: (정상 여부, 사유). 모니터링·/health/sheets용."""
        try:
            url = f"{_SHEETS_BASE}/{self._id}"
            resp = self._session.get(url, params={"fields": "spreadsheetId"})
            resp.raise_for_status()
            return True, "ok"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    @property
    def last_fetch_error(self) -> Optional[tuple[float, str]]:
        """가장 최근 fetch 실패 (시각 monotonic, 메시지) — 없으면 None."""
        if not self._fetch_errors:
            return None
        return max(self._fetch_errors.values(), key=lambda v: v[0])

    def _fetch(self, tab: str) -> list[dict]:
        try:
            result = self._get("values/" + tab + "!A1:ZZ")
            self._fetch_errors.pop(tab, None)
        except Exception as e:
            import logging
            self._fetch_errors[tab] = (time.monotonic(), f"{type(e).__name__}: {e}")
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
        result = self._get(f"values/{tab}!A1:ZZ")
        raw = result.get("values", [])
        key_values = [values[i] for i in key_cols]
        found_sheet_row: int | None = None
        if len(raw) > 1:
            headers = raw[0]
            for i, row in enumerate(raw[1:]):
                padded = row + [""] * (len(headers) - len(row))
                if [padded[k] for k in key_cols] == key_values:
                    found_sheet_row = i + 2
                    break
        if found_sheet_row is not None:
            self._put(
                f"values/{tab}!A{found_sheet_row}",
                body={"values": [values]},
                valueInputOption="RAW",
            )
        else:
            self._post(
                f"values/{tab}!A1:append",
                body={"values": [values]},
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
            )
        self._cache.pop(tab, None)

    def append_row(self, csv_filename: str, values: list[str]) -> None:
        """캐시 탭에 행 1개 추가 후 메모리 캐시 무효화."""
        tab = self.tab_for(csv_filename)
        if tab is None:
            return
        self._post(
            f"values/{tab}!A1:append",
            body={"values": [values]},
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
        )
        self._cache.pop(tab, None)

    def append_to_tab(self, tab_name: str, values: list[str]) -> None:
        """TAB_MAP 등록 없이 임의 탭에 행 추가 (results 탭 등 전용)."""
        self._post(
            f"values/{tab_name}!A1:append",
            body={"values": [values]},
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
        )

    def write_all(self, csv_filename: str, rows: list[dict], fieldnames: list[str]) -> None:
        """시트 전체를 덮어씀 (헤더 + 모든 행)."""
        tab = self.tab_for(csv_filename)
        if tab is None:
            return
        values = [fieldnames] + [[row.get(f, "") for f in fieldnames] for row in rows]
        self._post(f"values/{tab}!A1:ZZ:clear", body={})
        self._put(
            f"values/{tab}!A1",
            body={"values": values},
            valueInputOption="RAW",
        )
        self._cache.pop(tab, None)

    def invalidate(self, csv_filename: str) -> None:
        tab = self.tab_for(csv_filename)
        if tab:
            self._cache.pop(tab, None)


_instance: Optional[SheetsStore] = None
_init_failed_at: Optional[float] = None
_init_error_msg: Optional[str] = None
_INIT_RETRY_COOLDOWN_SECONDS = 60.0


def get_sheets_store() -> Optional[SheetsStore]:
    """설정된 경우 SheetsStore 싱글턴 반환, 미설정이거나 초기화 실패 시 None.

    초기화 실패는 영구 고착하지 않는다 — 쿨다운 후 재시도해서
    token 갱신 일시 장애가 '프로세스 수명 내내 빈 마스터'로 이어지지 않게 한다."""
    global _instance, _init_failed_at, _init_error_msg
    if _instance is not None:
        return _instance
    if (_init_failed_at is not None
            and (time.monotonic() - _init_failed_at) < _INIT_RETRY_COOLDOWN_SECONDS):
        return None
    from .config import get_settings
    sid = get_settings().google_sheets_mappings_id
    if not sid:
        return None
    try:
        _instance = SheetsStore(sid)
        _init_failed_at = None
        _init_error_msg = None
    except Exception as e:
        import logging
        _init_error_msg = f"{type(e).__name__}: {e}"
        logging.getLogger(__name__).warning(
            "SheetsStore 초기화 실패 — %d초 후 재시도, 그동안 로컬 CSV fallback: %s",
            int(_INIT_RETRY_COOLDOWN_SECONDS), e,
        )
        _init_failed_at = time.monotonic()
        return None
    return _instance


def get_sheets_health(deep: bool = False) -> dict:
    """Sheets 연결 상태 요약 — /health 및 모니터링용.

    deep=False: API 호출 없이 캐시된 상태(설정 여부·인스턴스 여부·최근 에러)만 본다.
    deep=True : probe()로 실제 토큰·연결을 검증한다 (운영자/모니터 폴링용).
    """
    from .config import get_settings
    sid = get_settings().google_sheets_mappings_id
    if not sid:
        return {"configured": False, "status": "disabled"}

    info: dict = {"configured": True}
    if _instance is None:
        info["status"] = "uninitialized"
        if _init_error_msg:
            info["init_error"] = _init_error_msg
        if deep:
            # 초기화를 한 번 시도해 본다 (쿨다운 무시하지 않음 — get_sheets_store 경유)
            store = get_sheets_store()
            if store is None:
                info["status"] = "error"
                if _init_error_msg:
                    info["init_error"] = _init_error_msg
                return info
            ok, reason = store.probe()
            info["status"] = "ok" if ok else "error"
            if not ok:
                info["probe_error"] = reason
        return info

    last_err = _instance.last_fetch_error
    if deep:
        ok, reason = _instance.probe()
        info["status"] = "ok" if ok else "error"
        if not ok:
            info["probe_error"] = reason
    else:
        info["status"] = "degraded" if last_err else "ok"
    if last_err:
        info["last_fetch_error"] = last_err[1]
    return info
