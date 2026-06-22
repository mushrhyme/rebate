"""dist 캐시 키 단일 출처(dist_cache_key) 계약 테스트.

세 경로(빌드·조회·쓰기)가 이 모듈을 공유하므로, 여기 스키마가 곧 시트 컬럼·
복합키 정의다. 키 일관성과 round-trip(쓰기→조회 키 일치)을 고정한다.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from backend.core import dist_cache_key as dck  # noqa: E402


def test_schema_shape():
    # 현재 운영 스키마 = context(2) + dimension(2) + dist_code/dist_name
    assert dck.CONTEXT_FIELDS == ("form_id", "issuer_fingerprint")
    assert dck.DIMENSION_FIELDS == ("retailer_code", "jisho")
    assert dck.CACHE_HEADERS == [
        "form_id", "issuer_fingerprint", "retailer_code", "jisho",
        "dist_code", "dist_name",
    ]
    # 키 인덱스는 키 필드 길이에서 자동 도출 (하드코딩 [0,1,2,3] 아님)
    assert dck.KEY_INDICES == list(range(len(dck.KEY_FIELDS)))


def test_key_from_mapping_missing_fields_default_empty():
    # jisho 없는 구 행도 안전하게 ""로 채워 4튜플
    k = dck.key_from_mapping({"form_id": "form_04", "retailer_code": "R001"})
    assert k == ("form_04", "", "R001", "")


def test_write_read_key_roundtrip():
    """쓰기 행에서 추출한 키 == 조회용 컨텍스트에서 만든 키 (드리프트 방지의 핵심)."""
    ctx = {"form_id": "form_04", "issuer_fingerprint": "fp1",
           "retailer_code": "R001", "jisho": "CVS営業部"}
    row = dck.row_from_mapping(ctx, "D001", "東日本")
    # CACHE_HEADERS 순서로 행 → dict 복원 후 키 추출
    row_dict = dict(zip(dck.CACHE_HEADERS, row))
    assert dck.key_from_mapping(row_dict) == dck.key_from_mapping(ctx)
    assert row_dict["dist_code"] == "D001"
    assert row_dict["dist_name"] == "東日本"


def test_same_retailer_different_jisho_distinct_keys():
    """같은 소매처라도 jisho가 다르면 다른 키 (판매처 분리 업무규칙)."""
    base = {"form_id": "f", "issuer_fingerprint": "fp", "retailer_code": "R001"}
    k_a = dck.key_from_mapping({**base, "jisho": "CVS営業部"})
    k_b = dck.key_from_mapping({**base, "jisho": "R営業東北"})
    assert k_a != k_b
