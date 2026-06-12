"""test_phase3_master_validation.py — legacy Phase 3 코드 검증·dist 캐시 로드 회귀

실행: pytest tests/unit/test_phase3_master_validation.py -v

배경 (2026-06-11 감사):
  1. legacy run_phase3()는 Claude가 confidence=high로 답한 코드를 마스터 존재
     여부 검증 없이 자동 확정하고 ocr_* 캐시에 영구 기록했다 (hallucination이
     캐시 오염으로 고착). _code_in_master()로 마스터 밖 코드를 pending으로 보낸다.
  2. _load_dist_cache()가 path.exists()로 선차단해, 로컬 CSV가 삭제된 Sheets
     운영 모드에서 ocr_dist 캐시를 영영 읽지 않았다 (Sheets에 있어도 무시).

검증 항목:
  1. 마스터에 있는 코드 → 통과
  2. 마스터에 없는 코드 → 거부 (pending행)
  3. 마스터 자체가 빈 경우(로드 실패) → 강제하지 않고 통과 (전건 pending 방지)
  4. 로컬 ocr_dist.csv가 없어도 _read_csv(Sheets) 결과로 dist 캐시가 구성됨
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import backend.pipeline.phase3 as phase3  # noqa: E402
import backend.pipeline.phase3_dist_resolver as dist_resolver  # noqa: E402


# ── _code_in_master ───────────────────────────────────────────────────────────

def test_code_in_master_accepts_known_code():
    assert phase3._code_in_master(
        "10101", {"10101", "20202"}, kind="retailer", ocr_name="テスト商店"
    ) is True


def test_code_in_master_rejects_unknown_code():
    assert phase3._code_in_master(
        "99999", {"10101", "20202"}, kind="retailer", ocr_name="テスト商店"
    ) is False


def test_code_in_master_skips_enforcement_when_master_empty():
    # 마스터 로드 실패(빈 집합) 시 전건 pending화가 더 위험 — 검증 생략하고 통과
    assert phase3._code_in_master(
        "99999", set(), kind="product", ocr_name="テスト商品"
    ) is True


# ── _load_dist_cache: 로컬 파일 부재 + Sheets 경유 _read_csv ──────────────────

_SHEETS_ROWS = [
    {"form_id": "form_01", "issuer_fingerprint": "fp-A", "retailer_code": "R1", "dist_code": "D1"},
    {"form_id": "form_04", "issuer_fingerprint": "fp-B", "retailer_code": "R2", "dist_code": "D2"},
]


def test_load_dist_cache_reads_without_local_file(monkeypatch, tmp_path):
    """path.exists()가 False여도 _read_csv(Sheets 우선) 결과를 캐시로 구성해야 한다."""
    monkeypatch.setattr(phase3, "_read_csv", lambda path: _SHEETS_ROWS)
    missing = tmp_path / "ocr_dist.csv"
    assert not missing.exists()

    cache = phase3._load_dist_cache(missing)

    assert cache[("form_01", "fp-A", "R1")] == "D1"
    assert cache[("form_04", "fp-B", "R2")] == "D2"


def test_load_dist_cache_empty_when_no_source(monkeypatch, tmp_path):
    monkeypatch.setattr(phase3, "_read_csv", lambda path: [])
    assert phase3._load_dist_cache(tmp_path / "ocr_dist.csv") == {}


def test_dist_resolver_cache_reads_without_local_file(monkeypatch, tmp_path):
    """resolve_dist_code_for_retailer도 로컬 ocr_dist.csv 부재 시 Sheets(_read_csv)를
    읽어야 한다 — exists() 선차단이 재도입되면 이 테스트가 깨진다."""
    calls: list[Path] = []

    def _fake_read_csv(path):
        calls.append(path)
        return _SHEETS_ROWS

    monkeypatch.setattr(dist_resolver, "_read_csv", _fake_read_csv)
    missing_dir = tmp_path  # ocr_dist.csv 없음

    resolution = dist_resolver.resolve_dist_code_for_retailer(
        "R1",
        mappings_dir=missing_dir,
        form_id="form_01",
        issuer_fingerprint="fp-A",
    )

    assert resolution.dist_code == "D1"
    assert resolution.basis == "cache"
    assert any(p.name == "ocr_dist.csv" for p in calls)
