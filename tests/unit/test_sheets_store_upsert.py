"""test_sheets_store_upsert.py — upsert가 항목마다 시트를 다시 읽지 않음 + 429 backoff 검증.

배경: upsert_row가 매 호출마다 `values/{tab}!A1:ZZ`를 GET 하던 N+1 패턴이
4개 문서 동시 처리 시 Sheets 분당 read 쿼터(429)를 넘겼다.
이제 TTL 메모리 캐시에서 행을 찾고, 쓰기 1회 후 캐시를 in-memory로 갱신한다.

검증:
  1. 연속 upsert는 초기 1회만 fetch (항목마다 재read 없음)
  2. 같은 키 재upsert는 append가 아니라 update (캐시에서 행 발견)
  3. _request가 429를 만나면 backoff 후 재시도해 성공한다
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.sheets_store import SheetsStore  # noqa: E402


class _RecordingStore(SheetsStore):
    """네트워크 없이 fetch 횟수와 write 호출을 기록하는 더블."""

    def __init__(self):
        self._id = "test"
        self._ttl = 3600
        self._cache = {}
        self._fetch_errors = {}
        import threading
        self._write_lock = threading.Lock()
        self.fetch_calls = 0
        self.puts: list[tuple[str, list]] = []
        self.posts: list[tuple[str, list]] = []
        # 시트 초기 상태: 헤더 + 기존 1행
        self._sheet = [["ocr_name", "product_code", "product_name"],
                       ["기존", "P000", "기존상품"]]

    def _fetch_raw(self, tab):
        self.fetch_calls += 1
        return [list(r) for r in self._sheet]

    def _put(self, path, body, **params):
        self.puts.append((path, body["values"][0]))
        return {}

    def _post(self, path, body, **params):
        self.posts.append((path, body["values"][0]))
        return {}


def test_upsert_does_not_refetch_per_item():
    store = _RecordingStore()
    # 신규 3건 연속 확정
    store.upsert_row("ocr_product.csv", [0], ["신규1", "P001", "상품1"])
    store.upsert_row("ocr_product.csv", [0], ["신규2", "P002", "상품2"])
    store.upsert_row("ocr_product.csv", [0], ["신규3", "P003", "상품3"])
    # fetch는 최초 1회만 (캐시 재사용) — 핵심 회귀 방지
    assert store.fetch_calls == 1
    # 3건 모두 append
    assert len(store.posts) == 3
    assert store.puts == []


def test_upsert_existing_key_updates_not_appends():
    store = _RecordingStore()
    # 캐시 적재
    store.upsert_row("ocr_product.csv", [0], ["신규1", "P001", "상품1"])
    # 방금 추가한 키를 다시 upsert → append 아닌 update 여야 함 (burst 내 dedup)
    store.upsert_row("ocr_product.csv", [0], ["신규1", "P999", "수정상품"])
    assert len(store.posts) == 1          # 첫 append 1회
    assert len(store.puts) == 1           # 두 번째는 update
    assert store.fetch_calls == 1         # 여전히 재read 없음


def test_existing_sheet_key_is_updated():
    store = _RecordingStore()
    store.upsert_row("ocr_product.csv", [0], ["기존", "P111", "갱신"])
    assert len(store.puts) == 1
    assert store.posts == []


def test_identical_value_skips_write():
    store = _RecordingStore()
    # 시트에 이미 있는 행과 '완전히 동일한' 값으로 upsert → write 생략
    store.upsert_row("ocr_product.csv", [0], ["기존", "P000", "기존상품"])
    assert store.puts == []
    assert store.posts == []


def test_concurrent_same_new_key_writes_once_then_skips():
    store = _RecordingStore()
    # 동시 문서가 같은 신규 키를 각자 확정하는 상황 모사 (값 동일)
    store.upsert_row("ocr_product.csv", [0], ["신규", "P777", "신상품"])  # append
    store.upsert_row("ocr_product.csv", [0], ["신규", "P777", "신상품"])  # 동일 → skip
    assert len(store.posts) == 1   # 최초 1회만 기록
    assert store.puts == []         # 동일값이라 update도 안 함
    assert store.fetch_calls == 1


def test_request_retries_on_429():
    import threading

    class _Resp:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}
            self.content = b"{}"

        def json(self):
            return {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError(f"unexpected raise {self.status_code}")

    class _Session:
        def __init__(self, seq):
            self.seq = list(seq)
            self.calls = 0

        def request(self, method, url, params=None, json=None):
            self.calls += 1
            return _Resp(self.seq.pop(0))

    store = SheetsStore.__new__(SheetsStore)
    store._id = "test"
    store._session = _Session([429, 429, 200])

    # time.sleep을 무력화해 빠르게
    import backend.core.sheets_store as mod
    orig_sleep = mod.time.sleep
    mod.time.sleep = lambda s: None
    try:
        out = store._request("GET", "values/x!A1:ZZ")
    finally:
        mod.time.sleep = orig_sleep
    assert out == {}
    assert store._session.calls == 3  # 429 두 번 후 세 번째 성공
