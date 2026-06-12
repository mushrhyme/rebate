"""test_sheets_store_ttl.py — SheetsStore 읽기 캐시 TTL 검증

실행: pytest tests/unit/test_sheets_store_ttl.py -v

배경: 기존에는 탭당 1회 fetch 후 프로세스 수명 내내 재사용(무TTL)이라,
현업이 Google Sheets 마스터를 수정해도 백엔드 재시작 전까지 반영되지 않았다.
TTL 만료 시 재조회하도록 변경 — 이 동작을 회귀 방지한다.

검증 항목:
  1. TTL 이내 반복 읽기는 fetch 1회 (캐시 사용)
  2. TTL 만료 후 읽기는 재fetch
  3. invalidate() 호출 시 즉시 재fetch
  4. TAB_MAP에 없는 파일명은 빈 리스트 (fetch 없음)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.sheets_store import SheetsStore  # noqa: E402


class _FakeSheetsStore(SheetsStore):
    """네트워크·token.pickle 없이 캐시 로직만 검증하기 위한 더블."""

    def __init__(self, ttl_seconds: float):
        # SheetsStore.__init__은 token.pickle을 읽으므로 우회하고 필드만 구성
        self._id = "test-spreadsheet"
        self._ttl = ttl_seconds
        self._cache = {}
        self.fetch_calls = 0

    def _fetch(self, tab: str) -> list[dict]:
        self.fetch_calls += 1
        return [{"call": str(self.fetch_calls)}]


def test_read_csv_uses_cache_within_ttl():
    store = _FakeSheetsStore(ttl_seconds=3600)
    first = store.read_csv("retail_user.csv")
    second = store.read_csv("retail_user.csv")
    assert store.fetch_calls == 1
    assert first == second == [{"call": "1"}]


def test_read_csv_refetches_after_ttl_expiry():
    store = _FakeSheetsStore(ttl_seconds=0.0)  # 즉시 만료
    store.read_csv("retail_user.csv")
    rows = store.read_csv("retail_user.csv")
    assert store.fetch_calls == 2
    assert rows == [{"call": "2"}]


def test_invalidate_forces_refetch():
    store = _FakeSheetsStore(ttl_seconds=3600)
    store.read_csv("retail_user.csv")
    store.invalidate("retail_user.csv")
    store.read_csv("retail_user.csv")
    assert store.fetch_calls == 2


def test_unknown_filename_returns_empty_without_fetch():
    store = _FakeSheetsStore(ttl_seconds=3600)
    assert store.read_csv("unknown.csv") == []
    assert store.fetch_calls == 0
