"""CSV 마스터 검색 — 제품 / 소매처."""
import csv
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from ...core.auth import get_current_user
from ...core.config import get_settings

router = APIRouter(prefix="/api/v3/search", tags=["search"])
log = logging.getLogger(__name__)


# ── CSV 로더 — SheetsStore가 내부 캐시를 보유하므로 lru_cache 불필요 ─────

def _load_products(mappings_dir: Path) -> list[dict]:
    from ...core.sheets_store import get_sheets_store
    store = get_sheets_store()
    if store:
        try:
            return store.read_csv("unit_price.csv")
        except Exception as e:
            log.warning("Sheets unit_price.csv 읽기 실패 — 로컬 CSV fallback: %s", e)
    path = mappings_dir / "unit_price.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _load_retailers(mappings_dir: Path) -> list[dict]:
    from ...core.sheets_store import get_sheets_store
    store = get_sheets_store()
    if store:
        try:
            return store.read_csv("retail_user.csv")
        except Exception as e:
            log.warning("Sheets retail_user.csv 읽기 실패 — 로컬 CSV fallback: %s", e)
    path = mappings_dir / "retail_user.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ── 엔드포인트 ────────────────────────────────────────────────

@router.get("/product")
async def search_product(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, le=50),
    _: dict = Depends(get_current_user),
):
    """제품명 또는 JAN코드로 unit_price.csv 검색."""
    settings = get_settings()
    rows = _load_products(settings.mappings_dir)
    q_lower = q.lower()

    results = []
    for r in rows:
        if q_lower in r["제품명"].lower() or q in r.get("JANコード", ""):
            results.append({
                "code":     r["제품코드"],
                "name":     r["제품명"],
                "volume":   r["제품용량"],
                "spec":     r["규격"],
                "sikiri":   _num(r["시키리"]),
                "honbucho": _num(r["본부장"]),
                "jan":      r.get("JANコード", ""),
            })
            if len(results) >= limit:
                break

    return results


@router.get("/retailer")
async def search_retailer(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, le=50),
    _: dict = Depends(get_current_user),
):
    """소매처명으로 retail_user.csv 검색."""
    settings = get_settings()
    rows = _load_retailers(settings.mappings_dir)
    q_lower = q.lower()

    results = []
    seen: set[str] = set()
    for r in rows:
        if q_lower in r["소매처명"].lower():
            code = r["소매처코드"]
            if code in seen:
                continue
            seen.add(code)
            results.append({
                "code": code,
                "name": r["소매처명"],
            })
            if len(results) >= limit:
                break

    return results


@router.get("/retailer-dists")
async def search_retailer_dists(
    retailer_code: str = Query(...),
    _: dict = Depends(get_current_user),
):
    """특정 소매처코드의 판매처 후보를 retail_user.csv에서 조회."""
    settings = get_settings()
    rows = _load_retailers(settings.mappings_dir)
    seen: set[str] = set()
    results = []
    for r in rows:
        if r["소매처코드"] == retailer_code:
            code = r["판매처코드"]
            if code in seen:
                continue
            seen.add(code)
            results.append({"code": code, "name": r["판매처명"]})
    return results


@router.get("/dist")
async def search_dist(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, le=50),
    _: dict = Depends(get_current_user),
):
    """판매처명 또는 코드로 retail_user.csv 검색."""
    settings = get_settings()
    rows = _load_retailers(settings.mappings_dir)
    q_lower = q.lower()
    seen: set[str] = set()
    results = []
    for r in rows:
        code = r["판매처코드"]
        name = r["판매처명"]
        if q_lower in name.lower() or q in code:
            if code in seen:
                continue
            seen.add(code)
            results.append({"code": code, "name": name})
            if len(results) >= limit:
                break
    return results


def _num(v: str) -> float | None:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
