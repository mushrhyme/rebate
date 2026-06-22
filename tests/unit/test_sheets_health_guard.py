"""#3 token.pickle 조기 경보 — SheetsStore 장애 감지·마스터 가드 검증.

배경: token.pickle refresh_token 철회/네트워크 장애 시 _fetch가 예외를 삼키고
빈 결과를 반환 → 마스터(unit_price)가 빈 채로 분석이 진행돼 모든 매핑이
not_found·NET이 None이 되는 '조용한 오답'이 가능했다. 이를 명시적 신호로 바꾼다.

검증:
  1. read_csv(required=True)는 fetch 실패로 빈 결과면 SheetsUnavailableError
  2. read_csv(required=True)는 fetch 성공·빈 탭이면 그냥 빈 리스트 (정상)
  3. probe() 성공/실패
  4. get_sheets_health(deep=False): 미설정 → disabled
  5. orchestrator._check_master_availability: 빈 마스터 → 사유 반환
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.sheets_store import SheetsStore, SheetsUnavailableError  # noqa: E402


class _FailingFetchStore(SheetsStore):
    """fetch가 항상 실패하는 더블 (토큰 만료·네트워크 장애 모사)."""

    def __init__(self):
        self._id = "test"
        self._ttl = 3600
        self._cache = {}
        self._fetch_errors = {}

    def _fetch_raw(self, tab: str) -> list[list]:
        # 실제 _fetch_raw와 동일하게 에러를 기록하고 빈 결과 반환
        self._fetch_errors[tab] = (time.monotonic(), "ConnectionError: boom")
        return []


class _EmptyTabStore(SheetsStore):
    """fetch는 성공하지만 탭이 실제로 비어 있는 더블 (정상 케이스)."""

    def __init__(self):
        self._id = "test"
        self._ttl = 3600
        self._cache = {}
        self._fetch_errors = {}

    def _fetch_raw(self, tab: str) -> list[list]:
        return []  # 성공, 빈 탭 — 에러 기록 없음


def test_required_raises_on_fetch_failure():
    store = _FailingFetchStore()
    with pytest.raises(SheetsUnavailableError):
        store.read_csv("unit_price.csv", required=True)


def test_required_returns_empty_on_genuinely_empty_tab():
    store = _EmptyTabStore()
    # fetch 성공 + 빈 탭 → 예외 없이 빈 리스트 (장애 아님)
    assert store.read_csv("unit_price.csv", required=True) == []


def test_non_required_never_raises():
    store = _FailingFetchStore()
    assert store.read_csv("unit_price.csv") == []  # required=False 기본


def test_last_fetch_error_exposed():
    store = _FailingFetchStore()
    store.read_csv("unit_price.csv")
    err = store.last_fetch_error
    assert err is not None and "ConnectionError" in err[1]


def test_health_disabled_when_unconfigured(monkeypatch):
    from backend.core import sheets_store
    from backend.core.config import get_settings

    monkeypatch.setattr(get_settings(), "google_sheets_mappings_id", "")
    h = sheets_store.get_sheets_health(deep=False)
    assert h["configured"] is False
    assert h["status"] == "disabled"


def test_check_master_availability_local_mode_is_skipped(monkeypatch):
    """Sheets 미설정(로컬 CSV 모드)이면 가드 비대상 → None."""
    from backend.pipeline import orchestrator
    from backend.core import sheets_store
    from backend.core.config import get_settings

    monkeypatch.setattr(sheets_store, "get_sheets_store", lambda: None)
    monkeypatch.setattr(get_settings(), "google_sheets_mappings_id", "")
    assert orchestrator._check_master_availability() is None


def test_check_master_availability_empty_master_blocks(monkeypatch):
    """Sheets 모드 + 빈 unit_price → 사유 문자열 반환 (분석 차단)."""
    from backend.pipeline import orchestrator
    from backend.core import sheets_store

    store = _FailingFetchStore()
    monkeypatch.setattr(sheets_store, "get_sheets_store", lambda: store)
    monkeypatch.setattr(store, "probe", lambda: (True, "ok"))
    reason = orchestrator._check_master_availability()
    assert reason is not None
    assert "unit_price" in reason or "읽기 실패" in reason


def test_check_master_availability_probe_fail_blocks(monkeypatch):
    """probe 실패(토큰 만료 등) → 사유 반환."""
    from backend.pipeline import orchestrator
    from backend.core import sheets_store

    store = _EmptyTabStore()
    monkeypatch.setattr(sheets_store, "get_sheets_store", lambda: store)
    monkeypatch.setattr(store, "probe", lambda: (False, "RefreshError: token revoked"))
    reason = orchestrator._check_master_availability()
    assert reason is not None and "probe" in reason
