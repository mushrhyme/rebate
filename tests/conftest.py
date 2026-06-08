"""conftest.py — pytest 공통 설정

backend 패키지를 import할 수 있도록 프로젝트 루트를 sys.path에 추가한다.
실행: pytest tests/ -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
