"""conftest.py — pytest 공통 설정

backend 패키지를 import할 수 있도록 프로젝트 루트를 sys.path에 추가한다.
실행: pytest tests/ -v
"""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def block_sheets_writes(monkeypatch):
    """테스트 중 운영 Sheets에 쓰지 못하도록 get_sheets_store를 None으로 차단."""
    monkeypatch.setattr(
        "backend.core.sheets_store.get_sheets_store",
        lambda: None,
    )
