"""Google Sheets 기반 매핑 CSV 저장소."""
import io
import csv as _csv
import logging
import os
import pickle
import random
import threading
import time
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import AuthorizedSession, Request

_log = logging.getLogger(__name__)

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

# 429/503 재시도 설정 — Sheets 분당 쿼터 초과 시 backoff (Retry-After 우선)
_MAX_RETRIES = int(os.getenv("SHEETS_MAX_RETRIES", "4"))
_RETRY_BASE_DELAY = float(os.getenv("SHEETS_RETRY_BASE_DELAY", "1.0"))
_RETRY_MAX_DELAY = float(os.getenv("SHEETS_RETRY_MAX_DELAY", "30.0"))
_RETRYABLE_STATUS = {429, 503}

# 운영상 절대 비어 있으면 안 되는 마스터 탭 — 빈 결과 = 토큰/네트워크 장애로 간주
MASTER_CSV_FILES = ("unit_price.csv", "retail_user.csv", "domae_retail_1.csv")


def _rows_to_dicts(raw: list[list]) -> list[dict]:
    """시트 원본 행(헤더 포함)을 list[dict]로 변환. 짧은 행은 빈 문자열로 패딩."""
    if not raw:
        return []
    headers = raw[0]
    out = []
    for row in raw[1:]:
        padded = list(row) + [""] * (len(headers) - len(row))
        out.append(dict(zip(headers, padded)))
    return out


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
        # tab → (cached_at_monotonic, raw_values)  raw_values: 시트 원본 행(헤더 포함) list[list[str]]
        self._cache: dict[str, tuple[float, list[list]]] = {}
        # tab → (실패 시각(monotonic), 메시지) — fetch 실패를 조용히 삼키지 않고 노출
        self._fetch_errors: dict[str, tuple[float, str]] = {}
        # upsert/append 직렬화 + 캐시 mutation 보호 (asyncio.to_thread로 병렬 진입 가능)
        self._write_lock = threading.Lock()
        self._session = self._build_session()

    def _build_session(self) -> AuthorizedSession:
        with open(_TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return AuthorizedSession(creds)

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None) -> dict:
        """Sheets API 호출 + 429/503 backoff 재시도 (Retry-After 우선).

        쿼터 초과(429)·일시 장애(503)는 exponential backoff로 재시도한다.
        그 외 4xx/5xx는 즉시 raise_for_status로 던진다."""
        url = f"{_SHEETS_BASE}/{self._id}/{path}"
        delay = _RETRY_BASE_DELAY
        for attempt in range(_MAX_RETRIES + 1):
            resp = self._session.request(method, url, params=params, json=body)
            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = delay
                else:
                    wait = delay
                wait = min(wait, _RETRY_MAX_DELAY) + random.uniform(0, 0.5)
                _log.warning(
                    "Sheets %s %s → %d, %.1fs 후 재시도 (%d/%d)",
                    method, path, resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(wait)
                delay = min(delay * 2, _RETRY_MAX_DELAY)
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        # 도달 불가 (루프 마지막에서 항상 raise/return) — 방어적
        resp.raise_for_status()
        return {}

    def _get(self, path: str, **params) -> dict:
        return self._request("GET", path, params=params)

    def _put(self, path: str, body: dict, **params) -> dict:
        return self._request("PUT", path, params=params, body=body)

    def _post(self, path: str, body: dict, **params) -> dict:
        return self._request("POST", path, params=params, body=body)

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
        raw = self._cached_raw(tab)
        rows = _rows_to_dicts(raw)
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

    def _cached_raw(self, tab: str) -> list[list]:
        """탭의 원본 행(헤더 포함)을 TTL 메모리 캐시로 반환. 만료 시에만 재조회."""
        entry = self._cache.get(tab)
        now = time.monotonic()
        if entry is None or (now - entry[0]) >= self._ttl:
            self._cache[tab] = (now, self._fetch_raw(tab))
        return self._cache[tab][1]

    def _fetch_raw(self, tab: str) -> list[list]:
        """탭 원본 값(values) GET. 실패 시 _fetch_errors에 기록하고 빈 리스트 반환."""
        try:
            result = self._get("values/" + tab + "!A1:ZZ")
            self._fetch_errors.pop(tab, None)
        except Exception as e:
            self._fetch_errors[tab] = (time.monotonic(), f"{type(e).__name__}: {e}")
            _log.warning("Sheets 탭 '%s' 읽기 실패 — 빈 결과 반환: %s", tab, e)
            return []
        return result.get("values", [])

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
        """키 컬럼 기준 upsert: 존재하면 해당 행 업데이트, 없으면 추가.

        행 위치 탐색은 항목마다 시트를 다시 읽지 않고 TTL 메모리 캐시(`_cached_raw`)에서
        수행한다 — 문서당 N개 매핑 확정 시 발생하던 'N회 전체-read'를 제거해
        Sheets 분당 쿼터 초과(429)를 막는다. 쓰기 1회 후 캐시를 in-memory로 갱신해
        같은 배치 내 후속 upsert가 재조회 없이 방금 쓴 행을 본다.
        """
        tab = self.tab_for(csv_filename)
        if tab is None:
            return
        values = list(values)
        key_values = [values[i] for i in key_cols]
        with self._write_lock:
            raw = self._cached_raw(tab)
            if not raw and tab in self._fetch_errors:
                # 빈 캐시가 'fetch 실패' 때문이면 맹목 추가(중복 양산) 금지 — 명시적 실패
                _, msg = self._fetch_errors[tab]
                raise SheetsUnavailableError(
                    f"Sheets 탭 '{tab}' 읽기 실패로 upsert 중단: {msg}"
                )
            found_sheet_row: int | None = None
            existing_row: list | None = None
            if len(raw) > 1:
                headers = raw[0]
                for i, row in enumerate(raw[1:]):
                    padded = row + [""] * (len(headers) - len(row))
                    if [padded[k] for k in key_cols] == key_values:
                        found_sheet_row = i + 2
                        existing_row = padded[: len(values)]
                        break
            new_raw = [list(r) for r in raw]
            if found_sheet_row is not None:
                # 값이 이미 동일하면 write 생략 — 동시 문서가 같은 신규 키를
                # 각자 확정할 때 발생하는 중복 PUT을 제거(쓰기 쿼터 절약)
                if existing_row == list(values):
                    return
                self._put(
                    f"values/{tab}!A{found_sheet_row}",
                    body={"values": [values]},
                    valueInputOption="RAW",
                )
                new_raw[found_sheet_row - 1] = values
            else:
                self._post(
                    f"values/{tab}!A1:append",
                    body={"values": [values]},
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                )
                new_raw.append(values)
            # TTL 시각은 유지 — 외부(현업 직접 편집)는 만료 후 재조회로 반영
            ts = self._cache[tab][0]
            self._cache[tab] = (ts, new_raw)

    def append_row(self, csv_filename: str, values: list[str]) -> None:
        """캐시 탭에 행 1개 추가 후 메모리 캐시도 동일하게 갱신."""
        tab = self.tab_for(csv_filename)
        if tab is None:
            return
        values = list(values)
        with self._write_lock:
            self._post(
                f"values/{tab}!A1:append",
                body={"values": [values]},
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
            )
            entry = self._cache.get(tab)
            if entry is not None:
                self._cache[tab] = (entry[0], [list(r) for r in entry[1]] + [values])

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
        with self._write_lock:
            self._post(f"values/{tab}!A1:ZZ:clear", body={})
            self._put(
                f"values/{tab}!A1",
                body={"values": values},
                valueInputOption="RAW",
            )
            # 방금 쓴 내용을 캐시에 반영 (불필요한 즉시 재조회 방지)
            self._cache[tab] = (time.monotonic(), [list(r) for r in values])

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
