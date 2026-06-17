"""phase3_dist_resolver.py — retailer_code → dist_code 조회

legacy run_phase3()에서 인라인으로 수행하던 dist_code 결정 로직을
재사용 가능한 함수로 분리한다.

Tool Use path와 adapter에서 dist_code를 채우기 위해 사용한다.

## Legacy와의 동작 일치 보장

처리 순서 (legacy phase3와 동일):
  ① ocr_dist.csv 캐시 조회 (form_id + issuer_fingerprint + retailer_code)
  ② retail_user.csv에서 소매처코드 == retailer_code인 행 수집
     1건 → 자동 확정 (basis="auto_1_to_1")
     N건 → 후보 반환 (basis="needs_confirmation"), Claude/사용자 결정 필요
     0건 → not_found (basis="not_found"), pending

## Side-effect

파일 읽기만 수행. 쓰기 없음.
confirm_mapping 호출 없음.
"""
import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "DistResolution",
    "resolve_dist_code_for_retailer",
    "build_dist_resolution_from_cache",
]

# ── basis 상수 ────────────────────────────────────────────────────────────────

_BASIS_CACHE        = "cache"        # ocr_dist.csv 캐시 히트
_BASIS_AUTO_1TO1    = "auto_1_to_1"  # retail_user.csv 1:1 자동 확정
_BASIS_CONFIRMATION = "needs_confirmation"  # 1:N, 사용자/Claude 결정 필요
_BASIS_NOT_FOUND    = "not_found"    # retail_user.csv에 해당 소매처코드 없음


# ── 결과 타입 ─────────────────────────────────────────────────────────────────

@dataclass
class DistResolution:
    """resolve_dist_code_for_retailer() 반환값.

    basis 값:
      "cache"             — ocr_dist.csv 캐시 히트 (form_id+issuer_fp+retailer_code)
      "auto_1_to_1"       — retail_user.csv에서 1건만 매칭 → 자동 확정
      "needs_confirmation" — 1:N 후보, Claude/사용자 결정 필요
      "not_found"         — retail_user.csv에 해당 소매처코드 없음
    """
    dist_code:          str | None     # 확정된 판매처코드 (None이면 미확정)
    basis:              str
    candidates:         list[dict] = field(default_factory=list)
    # [{"dist_code": str, "dist_name": str}]
    needs_confirmation: bool = False
    reason:             str | None = None


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────────────

def _read_csv(path: Path) -> list[dict]:
    from ..core.sheets_store import get_sheets_store, TAB_MAP
    store = get_sheets_store()
    if store and path.name in TAB_MAP:
        return store.read_csv(path.name)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ── 공개 함수 ─────────────────────────────────────────────────────────────────

def resolve_dist_code_for_retailer(
    retailer_code: str,
    *,
    mappings_dir: Path,
    form_id: str = "",
    issuer_fingerprint: str = "",
) -> DistResolution:
    """retailer_code → dist_code 조회.

    legacy phase3.py와 동일한 순서로 dist_code를 결정한다:

    ① ocr_dist.csv 캐시 (form_id + issuer_fingerprint + retailer_code)
    ② retail_user.csv 1:1 자동 확정
    ③ retail_user.csv 1:N → needs_confirmation 반환
    ④ retail_user.csv 0건 → not_found 반환

    파일 I/O:
      ocr_dist.csv, retail_user.csv 읽기. 쓰기 없음.
      confirm_mapping 호출 없음.

    Args:
        retailer_code:      확정된 소매처코드
        mappings_dir:       mappings/ 디렉토리 경로
        form_id:            양식 ID (ocr_dist.csv 캐시 키 일부)
        issuer_fingerprint: 발행처 지문 (ocr_dist.csv 캐시 키 일부)

    Returns:
        DistResolution
    """
    if not retailer_code:
        return DistResolution(
            dist_code=None,
            basis=_BASIS_NOT_FOUND,
            needs_confirmation=False,
            reason="retailer_code가 비어 있음",
        )

    # ── ① ocr_dist.csv 캐시 조회 ─────────────────────────────────────────────
    # path.exists() 선차단 금지 — _read_csv가 Sheets 우선 조회 (로컬 파일 없는 운영 모드 지원)
    dist_cache_path = mappings_dir / "ocr_dist.csv"
    for row in _read_csv(dist_cache_path):
        if (row.get("form_id", "") == form_id
                and row.get("issuer_fingerprint", "") == issuer_fingerprint
                and row.get("retailer_code", "") == retailer_code):
            return DistResolution(
                dist_code=row.get("dist_code", ""),
                basis=_BASIS_CACHE,
                needs_confirmation=False,
            )

    # ── ② retail_user.csv에서 소매처코드 기준 후보 수집 ──────────────────────
    retail_path = mappings_dir / "retail_user.csv"
    candidates: list[dict] = [
        {"dist_code": r.get("판매처코드", ""), "dist_name": r.get("판매처명", "")}
        for r in _read_csv(retail_path)
        if r.get("소매처코드") == retailer_code
    ]

    if len(candidates) == 1:
        # 1:1 — 자동 확정
        return DistResolution(
            dist_code=candidates[0]["dist_code"],
            basis=_BASIS_AUTO_1TO1,
            candidates=candidates,
            needs_confirmation=False,
        )

    if len(candidates) > 1:
        # 1:N — Claude/사용자 결정 필요
        return DistResolution(
            dist_code=None,
            basis=_BASIS_CONFIRMATION,
            candidates=candidates,
            needs_confirmation=True,
            reason=f"판매처 후보 {len(candidates)}건 — 사용자 또는 Claude 확인 필요",
        )

    # 0건 — retail_user.csv에 해당 소매처코드 없음
    log.warning(
        "[dist_resolver] NOT_FOUND — 소매처코드 '%s'에 대한 판매처 후보 없음 (form_id=%s)",
        retailer_code, form_id,
    )
    return DistResolution(
        dist_code=None,
        basis=_BASIS_NOT_FOUND,
        candidates=[],
        needs_confirmation=False,
        reason=f"retail_user.csv에 소매처코드 '{retailer_code}'에 해당하는 행 없음",
    )


def build_dist_resolution_from_cache(
    retailer_code: str,
    cached_dist: dict[tuple, str],
    retail_user_rows: list[dict],
    *,
    form_id: str = "",
    issuer_fingerprint: str = "",
    jisho: str = "",
) -> DistResolution:
    """미리 로드된 캐시·CSV 데이터로 dist 결정 (파일 I/O 없음 버전).

    ocr_dist.csv와 retail_user.csv가 이미 로드된 경우에 사용.
    Tool Use runtime에서 동일한 파일을 반복 읽지 않도록 한다.

    Args:
        retailer_code:      확정된 소매처코드
        cached_dist:        ocr_dist.csv → {(form_id, issuer_fp, retailer_code, jisho): dist_code}
        retail_user_rows:   retail_user.csv 전체 행 목록
        form_id:            양식 ID
        issuer_fingerprint: 발행처 지문

    Returns:
        DistResolution (파일 I/O 없음)
    """
    if not retailer_code:
        return DistResolution(
            dist_code=None,
            basis=_BASIS_NOT_FOUND,
            needs_confirmation=False,
            reason="retailer_code가 비어 있음",
        )

    # ① 캐시 조회 — (form_id, issuer_fingerprint, retailer_code, jisho) 4튜플 키.
    # 같은 소매처라도 jisho(入出荷支店 등)가 다르면 판매처가 갈리므로 jisho 포함.
    cache_key = (form_id, issuer_fingerprint, retailer_code, jisho)
    if cache_key in cached_dist:
        return DistResolution(
            dist_code=cached_dist[cache_key],
            basis=_BASIS_CACHE,
            needs_confirmation=False,
        )

    # ② retail_user.csv 후보 수집
    candidates = [
        {"dist_code": r.get("판매처코드", ""), "dist_name": r.get("판매처명", "")}
        for r in retail_user_rows
        if r.get("소매처코드") == retailer_code
    ]

    if len(candidates) == 1:
        return DistResolution(
            dist_code=candidates[0]["dist_code"],
            basis=_BASIS_AUTO_1TO1,
            candidates=candidates,
            needs_confirmation=False,
        )

    if len(candidates) > 1:
        return DistResolution(
            dist_code=None,
            basis=_BASIS_CONFIRMATION,
            candidates=candidates,
            needs_confirmation=True,
            reason=f"판매처 후보 {len(candidates)}건 — 사용자 또는 Claude 확인 필요",
        )

    return DistResolution(
        dist_code=None,
        basis=_BASIS_NOT_FOUND,
        candidates=[],
        needs_confirmation=False,
        reason=f"retail_user.csv에 소매처코드 '{retailer_code}'에 해당하는 행 없음",
    )
