"""mapping.py — 소매처·제품·판매처 매핑 Tool Layer

업무 능력(capability) 단위 추상화.
Python Workflow에서 직접 호출되며, 향후 Claude tool_use로도 호출 가능.

공개 Tool API:
  lookup_retailer()  — OCR 거래처명 → 소매처코드 조회
  search_product()   — OCR 제품명 → 제품코드 조회
  confirm_mapping()  — 매핑 확정 결과를 캐시 CSV에 기록

공통 Contract:
  - confidence는 항상 [0.0, 1.0] 범위
  - candidates는 similarity 내림차순 정렬, code 기준 dedup
  - basis 값은 각 Result dataclass에 정의된 Literal 범위 안
  - CSV 파일이 없어도 예외 없이 not_found / 빈 후보 반환
  - invalid mapping_type은 ValueError
  - dist 필수 context 키 누락은 ValueError
"""
import asyncio
import csv
import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict

from .metrics import (
    _record_confirm_mapping_failure,
    _record_confirm_mapping_success,
    _record_lookup_retailer,
    _record_search_product,
)


# ── 내부 유틸리티 ─────────────────────────────────────────────────────────────

# NFKC 정규화 후의 법인격 표기 목록
# （株）→(株)、㈱→(株) 등은 NFKC로 먼저 변환되므로 반각 형태만 열거
_LEGAL_MARKERS = [
    "株式会社", "有限会社", "合同会社",
    "(株)", "(有)", "(合)",
]

# 제품 OCR명에 붙는 제조사 prefix — 유사도 계산 전 쿼리 측에서만 제거
# "農心ジャパン"을 "農心"보다 먼저 시도해야 "農心"만 제거하고 "ジャパン"이 남는 오류를 방지한다
_MANUFACTURER_PREFIXES = ["農心ジャパン", "農心", "Nongshim", "NONGSHIM"]


def _strip_manufacturer_prefix(name: str) -> str:
    """제조사명 prefix 제거 (유사도 계산용 — unit_price.csv 검색 시 쿼리에만 적용)."""
    for prefix in _MANUFACTURER_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):].lstrip()
    return name


def normalize_ocr_name(name: str) -> str:
    """OCR 명칭 정규화: 전각→반각(NFKC) + 법인격 제거 + 공백 압축.

    캐시 조회 키로만 사용. 원본 OCR명은 캐시 파일에 그대로 저장한다.
    """
    name = unicodedata.normalize("NFKC", name)
    for marker in _LEGAL_MARKERS:
        name = name.replace(marker, "")
    name = " ".join(name.split())
    # OCR 아티팩트: "6 8 g" → "68g" (숫자 사이·숫자-단위 사이 공백 제거)
    while True:
        collapsed = re.sub(r"(\d) (\d)", r"\1\2", name)
        if collapsed == name:
            break
        name = collapsed
    name = re.sub(r"(\d) ([gGmMlLkK][gGlL]?\b)", r"\1\2", name)
    return name


def _read_csv(path: Path) -> list[dict]:
    from ..core.sheets_store import get_sheets_store, TAB_MAP
    store = get_sheets_store()
    if store and path.name in TAB_MAP:
        return store.read_csv(path.name)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _parse_bracket_code_csv(form_md: str) -> str:
    """form_XX.md의 データソース 섹션에서 bracket_code_csv 지시어 추출.
    없으면 빈 문자열 반환.
    """
    for line in form_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("bracket_code_csv:"):
            return stripped[len("bracket_code_csv:"):].strip()
    return ""


def parse_retailer_csv_sources(form_md: str) -> list[str]:
    """form_XX.md의 ## データソース 섹션에서 소매처 매핑용 CSV 목록 추출.
    섹션이 없으면 기본값 ["retail_user.csv"] 반환.
    """
    lines = form_md.splitlines()
    in_section = False
    csvs: list[str] = []
    for line in lines:
        if line.strip().startswith("## データソース"):
            in_section = True
            continue
        if in_section:
            if line.startswith("##"):
                break
            stripped = line.strip()
            if stripped.startswith("- ") and stripped.endswith(".csv"):
                csvs.append(stripped[2:].strip())
    return csvs or ["retail_user.csv"]


# ── 후보 항목 타입 정의 (TypedDict) ──────────────────────────────────────────

class RetailerCandidate(TypedDict):
    """lookup_retailer() candidates 리스트의 항목 구조.

    similarity: difflib.SequenceMatcher 기반, 항상 (0.3, 1.0] 범위.
    candidates는 similarity 내림차순 정렬, retailer_code 기준 dedup.
    """
    retailer_code: str
    retailer_name: str
    source: str        # CSV 파일명 (예: "retail_user.csv")
    similarity: float  # (0.3, 1.0], 소수점 3자리


class ProductCandidate(TypedDict):
    """search_product() candidates 리스트의 항목 구조.

    프런트엔드 MappingCandidate 인터페이스와 필드명을 일치시켜 직접 저장 가능.
    score: difflib.SequenceMatcher 기반, 항상 (0.3, 1.0] 범위.
    candidates는 score 내림차순 정렬, code 기준 dedup.
    volume: unit_price.csv 제품용량 (g 단위 문자열). 없으면 빈 문자열.
    case_qty: 규격 컬럼 값 (예: "12×2"). 없으면 빈 문자열.
    """
    code: str
    name: str
    score: float       # (0.3, 1.0], 소수점 3자리
    volume: str        # 제품용량 (Claude 용량 대조용, UI 표시용)
    case_qty: str      # 규격 (UI 표시용 — 개입수 식별)
    shikiri: float     # 시키리 단가
    honbucho: float    # 본부장 단가


# ── 공개 Result 타입 ──────────────────────────────────────────────────────────

@dataclass
class LookupRetailerResult:
    """lookup_retailer() 반환값.

    Guarantees:
      - confidence ∈ [0.0, 1.0]
      - basis ∈ {"cache", "bracket_code", "exact_match", "candidate", "not_found"}
      - basis="cache"|"bracket_code"|"exact_match" → retailer_code is not None,
                                                     confidence=1.0, candidates=[]
      - basis="candidate" → retailer_code is None, len(candidates) >= 1,
                            confidence = candidates[0]["similarity"]
      - basis="not_found" → retailer_code is None, candidates=[], confidence=0.0
      - candidates는 similarity 내림차순 정렬, retailer_code 기준 dedup
    """
    retailer_code: str | None
    basis: Literal["cache", "bracket_code", "exact_match", "candidate", "not_found"]
    confidence: float
    candidates: list[RetailerCandidate] = field(default_factory=list)


@dataclass
class SearchProductResult:
    """search_product() 반환값.

    Guarantees:
      - confidence ∈ [0.0, 1.0]
      - basis ∈ {"cache", "candidate", "not_found"}
      - basis="cache" → product_code is not None, confidence=1.0, candidates=[]
      - basis="candidate" → product_code is None, len(candidates) >= 1,
                            confidence = candidates[0]["score"]
      - basis="not_found" → product_code is None, candidates=[], confidence=0.0
      - candidates는 score 내림차순 정렬, code 기준 dedup
    """
    product_code: str | None
    basis: Literal["cache", "candidate", "not_found"]
    confidence: float
    candidates: list[ProductCandidate] = field(default_factory=list)


# ── 캐시 upsert (내부 구현) ───────────────────────────────────────────────────

def _upsert_cache_row(
    path: Path, key_col: str, headers: list[str], key: str, new_row: list[str],
) -> None:
    """캐시 파일에 key 기준 upsert. 헤더 컬럼 확장 시 기존 행에 빈 값을 채운다."""
    rows: list[list[str]] = []
    if path.exists() and path.stat().st_size > 0:
        try:
            with path.open(encoding="utf-8-sig") as f:
                rows = [[r.get(h, "") for h in headers] for r in csv.DictReader(f)]
        except Exception:
            pass
    key_idx = headers.index(key_col)
    updated = False
    for i, row in enumerate(rows):
        if len(row) > key_idx and row[key_idx] == key:
            rows[i] = new_row
            updated = True
            break
    if not updated:
        rows.append(new_row)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


def _upsert_dist_cache_row(
    path: Path, form_id: str, issuer_fingerprint: str,
    retailer_code: str, dist_code: str, dist_name: str = "",
) -> None:
    """ocr_dist.csv 복합키(form_id, issuer_fingerprint, retailer_code) upsert."""
    headers = ["form_id", "issuer_fingerprint", "retailer_code", "dist_code", "dist_name"]
    new_row = [form_id, issuer_fingerprint, retailer_code, dist_code, dist_name]
    rows: list[list[str]] = []
    if path.exists() and path.stat().st_size > 0:
        try:
            with path.open(encoding="utf-8-sig") as f:
                rows = [[r.get(h, "") for h in headers] for r in csv.DictReader(f)]
        except Exception:
            pass
    updated = False
    for i, row in enumerate(rows):
        if len(row) >= 3 and row[0] == form_id and row[1] == issuer_fingerprint and row[2] == retailer_code:
            rows[i] = new_row
            updated = True
            break
    if not updated:
        rows.append(new_row)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


# ── CSV 파일별 write lock ──────────────────────────────────────────────────────

# path.resolve() 기준 per-file asyncio.Lock registry.
# 단일 asyncio 이벤트 루프 안에서 같은 파일에 대한 read-modify-write를 직렬화한다.
# 서로 다른 파일은 독립 lock으로 병렬 실행 가능.
# 주의: single-process 안전성만 보장.
#       multi-worker uvicorn/gunicorn에서는 lock이 각 프로세스에 독립적이므로
#       프로세스 간 경쟁 조건이 발생한다. fcntl.flock 또는 DB 전환이 필요하다.
_CSV_LOCKS: dict[str, asyncio.Lock] = {}


def _get_csv_lock(path: Path) -> asyncio.Lock:
    """path.resolve() 기준으로 per-file asyncio.Lock을 반환한다.

    동일 파일(resolve 후 canonical path가 같으면)은 항상 동일 Lock 객체.
    Python 3.10+ 에서는 asyncio.Lock()을 이벤트 루프 외부에서도 생성 가능.
    dict 접근은 asyncio 단일 스레드 모델에서 원자적이다 (await 없음).
    """
    key = str(path.resolve())
    if key not in _CSV_LOCKS:
        _CSV_LOCKS[key] = asyncio.Lock()
    return _CSV_LOCKS[key]


# ── 공개 Tool API ─────────────────────────────────────────────────────────────

async def confirm_mapping(
    mapping_type: Literal["retailer", "product", "dist"],
    ocr_name: str,
    confirmed_code: str,
    context: dict,
    mappings_dir: Path,
) -> None:
    """매핑 확정 결과를 캐시 CSV에 기록한다.

    mapping_type별 저장 대상:
      "retailer" → mappings/ocr_retailer.csv  (키: ocr_name)
      "product"  → mappings/ocr_product.csv   (키: ocr_name)
      "dist"     → mappings/ocr_dist.csv      (키: form_id + issuer_fingerprint + retailer_code)

    Args:
        mapping_type:   저장 대상 종류
        ocr_name:       OCR 원문 명칭 (retailer·product 의 CSV 키; dist는 참고용)
        confirmed_code: 확정된 코드 (retailer_code / product_code / dist_code)
        context:        타입별 추가 정보
            retailer → {"retailer_name": str}  (선택)
            product  → {"product_name": str}   (선택)
            dist     → 필수: "form_id", "issuer_fingerprint", "retailer_code"
                       선택: "dist_name"
        mappings_dir:   mappings/ 디렉토리 경로

    Returns:
        None

    Raises:
        ValueError: 알 수 없는 mapping_type
        ValueError: dist mapping_type에 필수 context 키 누락
                    ("form_id", "issuer_fingerprint", "retailer_code")

    동시성 안전:
        같은 CSV 파일에 대한 동시 호출은 _get_csv_lock()으로 직렬화된다.
        read-modify-write 전체가 lock 범위 안에서 asyncio.to_thread()로 실행되므로
        이벤트 루프를 블로킹하지 않으면서 write 유실이 발생하지 않는다.
    """
    try:
        from ..core.sheets_store import get_sheets_store
        store = get_sheets_store()

        if mapping_type == "retailer":
            row = [ocr_name, confirmed_code, context.get("retailer_name", "")]
            if store:
                await asyncio.to_thread(store.upsert_row, "ocr_retailer.csv", [0], row)
            else:
                cache_path = mappings_dir / "ocr_retailer.csv"
                async with _get_csv_lock(cache_path):
                    await asyncio.to_thread(
                        _upsert_cache_row, cache_path, "ocr_name",
                        ["ocr_name", "retailer_code", "retailer_name"], ocr_name, row,
                    )
        elif mapping_type == "product":
            row = [ocr_name, confirmed_code, context.get("product_name", "")]
            if store:
                await asyncio.to_thread(store.upsert_row, "ocr_product.csv", [0], row)
            else:
                cache_path = mappings_dir / "ocr_product.csv"
                async with _get_csv_lock(cache_path):
                    await asyncio.to_thread(
                        _upsert_cache_row, cache_path, "ocr_name",
                        ["ocr_name", "product_code", "product_name"], ocr_name, row,
                    )
        elif mapping_type == "dist":
            _required = {"form_id", "issuer_fingerprint", "retailer_code"}
            _missing = _required - set(context)
            if _missing:
                raise ValueError(
                    f"confirm_mapping(dist)에 필요한 context 키가 없음: {sorted(_missing)}"
                )
            row = [
                context["form_id"], context["issuer_fingerprint"],
                context["retailer_code"], confirmed_code, context.get("dist_name", ""),
            ]
            if store:
                await asyncio.to_thread(store.upsert_row, "ocr_dist.csv", [0, 1, 2], row)
            else:
                cache_path = mappings_dir / "ocr_dist.csv"
                async with _get_csv_lock(cache_path):
                    await asyncio.to_thread(
                        _upsert_dist_cache_row, cache_path,
                        context["form_id"], context["issuer_fingerprint"],
                        context["retailer_code"], confirmed_code, context.get("dist_name", ""),
                    )
        else:
            raise ValueError(f"알 수 없는 mapping_type: {mapping_type!r}")
        _record_confirm_mapping_success()
    except Exception:
        _record_confirm_mapping_failure()
        raise


async def search_product(
    ocr_name: str,
    mappings_dir: Path,
    top_k: int = 15,
) -> SearchProductResult:
    """OCR 제품명 → 제품코드 조회.

    처리 순서:
      ① 캐시 조회 (ocr_product.csv) — 정규화 매칭
      ② unit_price.csv 유사도 검색  — similarity > 0.3인 후보 반환

    Args:
        ocr_name:     OCR에서 추출한 제품명 원문
        mappings_dir: mappings/ 디렉토리 경로
        top_k:        유사도 후보 최대 수 (기본값 5, 1 이상)

    Returns:
        SearchProductResult
          basis="cache"     → product_code 확정 (confidence=1.0)
          basis="candidate" → 후보 목록, Claude 판단 필요
          basis="not_found" → 조회 불가 (CSV 없음 포함)

    Raises:
        없음 — CSV 파일이 없거나 컬럼이 누락되어도 not_found로 반환
    """
    # ── ① 캐시 조회 ────────────────────────────────────────────────────────────
    # cache_path.exists() 조건 없이 항상 시도 — Sheets 모드에서는 로컬 파일 없어도 읽음
    cache_path = mappings_dir / "ocr_product.csv"
    norm_query = normalize_ocr_name(ocr_name)
    for row in _read_csv(cache_path):
        if normalize_ocr_name(row.get("ocr_name", "")) == norm_query:
            code = row.get("product_code", "")
            if code:  # 컬럼 누락 행은 캐시 미스로 처리
                _record_search_product("cache")
                return SearchProductResult(
                    product_code=code,
                    basis="cache",
                    confidence=1.0,
                )

    # ── ② 유사도 검색 ───────────────────────────────────────────────────────────
    candidates = _search_product_candidates(ocr_name, mappings_dir, top_k)
    if candidates:
        _record_search_product("candidate")
        return SearchProductResult(
            product_code=None,
            basis="candidate",
            confidence=candidates[0]["score"],
            candidates=candidates,
        )

    _record_search_product("not_found")
    return SearchProductResult(
        product_code=None,
        basis="not_found",
        confidence=0.0,
    )


async def lookup_retailer(
    ocr_name: str,
    form_id: str,
    mappings_dir: Path,
    form_definitions_dir: Path | None = None,
    top_k: int = 5,
) -> LookupRetailerResult:
    """OCR 거래처명 → 소매처코드 조회.

    처리 순서:
      ① 캐시 조회 (ocr_retailer.csv) — 정규화 매칭
      ② 괄호 코드 추출 → domae_retail CSV 직접 매칭 (bracket_code_csv 지정 양식)
      ③ retail_user.csv + 양식별 CSV 유사도 검색 — similarity > 0.3인 후보 반환

    Args:
        ocr_name:             OCR에서 추출한 거래처명 원문
        form_id:              양식 ID (예: "form_01")
        mappings_dir:         mappings/ 디렉토리 경로
        form_definitions_dir: form_definitions/ 경로. None이면 get_settings()에서 로드.
                              테스트 시에는 명시적으로 전달할 것.
        top_k:                유사도 후보 최대 수 (기본값 5, 1 이상)

    Returns:
        LookupRetailerResult
          basis="cache"        → retailer_code 확정 (confidence=1.0)
          basis="bracket_code" → retailer_code 확정 (confidence=1.0)
          basis="exact_match"  → 정규화 완전 일치로 자동 확정 (confidence=1.0)
          basis="candidate"    → 후보 목록, Claude 판단 필요
          basis="not_found"    → 조회 불가 (CSV·MD 없음 포함)

    Raises:
        없음 — CSV·MD 파일이 없거나 컬럼이 누락되어도 not_found로 반환
    """
    if form_definitions_dir is None:
        from ..core.config import get_settings
        form_definitions_dir = get_settings().form_definitions_dir

    # ── ① 캐시 조회 ────────────────────────────────────────────────────────────
    # cache_path.exists() 조건 없이 항상 시도 — Sheets 모드에서는 로컬 파일 없어도 읽음
    cache_path = mappings_dir / "ocr_retailer.csv"
    norm_query = normalize_ocr_name(ocr_name)
    for row in _read_csv(cache_path):
        if normalize_ocr_name(row.get("ocr_name", "")) == norm_query:
            code = row.get("retailer_code", "")
            if code:  # 컬럼 누락 행은 캐시 미스로 처리
                _record_lookup_retailer("cache")
                return LookupRetailerResult(
                    retailer_code=code,
                    basis="cache",
                    confidence=1.0,
                )

    # ── form_XX.md 로드 ─────────────────────────────────────────────────────────
    form_path = form_definitions_dir / f"{form_id}.md"
    form_md = form_path.read_text(encoding="utf-8") if form_path.exists() else ""

    # ── ② 괄호 코드 직접 매칭 ──────────────────────────────────────────────────
    if form_md:
        bracket_csv_name = _parse_bracket_code_csv(form_md)
        if bracket_csv_name:
            bracket_path = mappings_dir / bracket_csv_name
            m = re.search(r'\((\d+)\)', ocr_name)
            if m:
                bracket_code = m.group(1)
                domae_map: dict[str, str] = {}
                for r in _read_csv(bracket_path):
                    keys = list(r.keys())
                    if len(keys) >= 2:
                        domae_map[r[keys[0]]] = r[keys[1]]
                retailer_code = domae_map.get(bracket_code, "")
                if retailer_code:
                    _record_lookup_retailer("bracket_code")
                    return LookupRetailerResult(
                        retailer_code=retailer_code,
                        basis="bracket_code",
                        confidence=1.0,
                    )

    # ── ③ 유사도 검색 ───────────────────────────────────────────────────────────
    candidates = _search_retailer_candidates(
        ocr_name=ocr_name,
        form_md=form_md,
        mappings_dir=mappings_dir,
        top_k=top_k,
    )
    if candidates:
        # 정규화 완전 일치(similarity=1.0)는 Claude 판단 없이 자동 확정
        if candidates[0]["similarity"] == 1.0:
            _record_lookup_retailer("exact_match")
            return LookupRetailerResult(
                retailer_code=candidates[0]["retailer_code"],
                basis="exact_match",
                confidence=1.0,
            )
        _record_lookup_retailer("candidate")
        return LookupRetailerResult(
            retailer_code=None,
            basis="candidate",
            confidence=candidates[0]["similarity"],
            candidates=candidates,
        )

    _record_lookup_retailer("not_found")
    return LookupRetailerResult(
        retailer_code=None,
        basis="not_found",
        confidence=0.0,
    )


# ── 내부 검색 헬퍼 ────────────────────────────────────────────────────────────

def _extract_volume_g(text: str) -> float | None:
    """텍스트에서 용량(g 단위)을 추출. 없으면 None."""
    m = re.search(r'(\d+(?:\.\d+)?)\s*g\b', text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _tokenize(text: str) -> set[str]:
    """문자 종류별 토큰 분리 (히라가나/가타카나/한자/알파벳/숫자 연속 구간).

    단어 순서가 뒤바뀐 경우에도 Jaccard 유사도가 높게 나오도록 한다.
    """
    return set(re.findall(r'[ぁ-ん]+|[ァ-ヶー]+|[一-龯々]+|[a-zA-Z]+|\d+', text))


# 용량 보정 파라미터
_VOL_BOOST = 0.15       # OCR 용량과 DB 용량이 일치할 때 가산
_VOL_PENALTY = 0.20     # OCR 용량이 있는데 DB 용량이 다를 때 감산
# 용량 일치 허용 오차(g). 제품용량은 정수라 사실상 정확 일치를 요구한다.
# 과거 ±5% 비율 톨러런스(0.95)는 103↔105·113↔114처럼 인접하지만 다른 제품을
# "같은 용량"으로 오판해 틀린 후보에 가산까지 줬다. 0.5g 미만 = 같은 정수로 좁힌다.
_VOL_MATCH_TOLERANCE = 0.5

# 점수 혼합 비율
_SEQ_WEIGHT = 0.6   # SequenceMatcher 비중 (문자열 연속 일치)
_JAC_WEIGHT = 0.4   # Jaccard 비중 (단어 집합 일치, 순서 무관)


def _search_product_candidates(
    ocr_name: str,
    mappings_dir: Path,
    top_k: int,
) -> list[ProductCandidate]:
    """unit_price.csv에서 제품명 유사도 기반 후보 검색.

    점수 = SequenceMatcher * 0.6 + Jaccard(토큰) * 0.4 + 용량 보정
    용량 보정: OCR에 g 정보가 있을 때만 적용.
      - DB 용량과 정확 일치(±0.5g) → +_VOL_BOOST
      - DB 용량이 다름            → -_VOL_PENALTY

    용량 우선 정렬: OCR에 용량이 있으면 "용량 일치" 후보를 점수와 무관하게 위로 올리고
    (1차 정렬키), 컷오프(0.3)도 면제한다. 이름이 더 비슷한 다른 용량 제품(예: 103 OCR에
    이름이 똑같은 105 제품)이 정답을 밀어내거나 top_k에서 잘라버리는 것을 막는다.

    동일 product_code가 여러 행에 있으면 최고 점수 1건만 유지.
    컬럼이 없거나 파일이 없으면 빈 리스트 반환.
    """
    norm_query = _strip_manufacturer_prefix(normalize_ocr_name(ocr_name))
    query_vol = _extract_volume_g(norm_query)
    query_tokens = _tokenize(norm_query)
    # (volume_match, ProductCandidate) — volume_match: True=일치, False=불일치, None=비교불가
    scored: list[tuple[bool | None, ProductCandidate]] = []
    seen_codes: set[str] = set()

    p = mappings_dir / "unit_price.csv"
    rows = _read_csv(p)
    if not rows:
        return []

    name_col, code_col = "제품명", "제품코드"
    vol_col = "제품용량"
    spec_col = "규격"
    if name_col not in rows[0] or code_col not in rows[0]:
        return []

    def _num(v: str) -> float:
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    for row in rows:
        name_val = row.get(name_col, "")
        code_val = row.get(code_col, "")
        if not name_val or not code_val:
            continue
        norm_name = normalize_ocr_name(name_val)
        if not norm_name:
            continue

        seq_score = difflib.SequenceMatcher(None, norm_query, norm_name).ratio()
        name_tokens = _tokenize(norm_name)
        union = query_tokens | name_tokens
        jac_score = len(query_tokens & name_tokens) / len(union) if union else 0.0
        score = _SEQ_WEIGHT * seq_score + _JAC_WEIGHT * jac_score

        vol_match: bool | None = None  # OCR/DB 용량 둘 다 있을 때만 True/False
        if query_vol is not None:
            db_vol = _num(row.get(vol_col, ""))
            if db_vol > 0:
                vol_match = abs(query_vol - db_vol) < _VOL_MATCH_TOLERANCE
                score += _VOL_BOOST if vol_match else -_VOL_PENALTY

        # 용량 일치 후보는 컷오프 면제 — 이름이 달라도 Claude가 보게 한다
        if score > 0.3 or vol_match is True:
            scored.append((vol_match, ProductCandidate(
                code=code_val,
                name=name_val.strip(),
                score=round(score, 3),
                volume=row.get(vol_col, ""),
                case_qty=row.get(spec_col, ""),
                shikiri=_num(row.get("시키리", "")),
                honbucho=_num(row.get("본부장", "")),
            )))

    # 정렬: OCR에 용량이 있으면 "용량 일치" 우선(1차), 그 다음 점수(2차).
    # vol_match: True=일치(맨 위), None=비교불가(중립), False=불일치(맨 아래).
    def _rank(item: tuple[bool | None, ProductCandidate]) -> tuple[int, float]:
        vm, cand = item
        tier = 1 if vm is True else (0 if vm is None else -1)
        return (tier, cand["score"])

    scored.sort(key=_rank, reverse=True)
    deduped: list[ProductCandidate] = []
    for _vm, item in scored:
        code = item["code"]
        if code not in seen_codes:
            seen_codes.add(code)
            deduped.append(item)

    return deduped[:top_k]


def _search_retailer_candidates(
    ocr_name: str,
    form_md: str,
    mappings_dir: Path,
    top_k: int,
) -> list[RetailerCandidate]:
    """retail_user.csv + 양식별 CSV 유사도 기반 후보 검색.

    - retail_user.csv     : 소매처명 열로 검색
    - domae_retail_1.csv  : 코드→코드 매핑이므로 이름 검색 대상 아님 (스킵)

    동일 소매처코드가 여러 행에 등장하면 최고 점수 1건만 유지.
    컬럼이 없거나 파일이 없으면 해당 소스를 건너뜀.
    """
    norm_query = normalize_ocr_name(ocr_name)
    scored: list[RetailerCandidate] = []
    seen_codes: set[str] = set()

    csv_sources = parse_retailer_csv_sources(form_md) if form_md else ["retail_user.csv"]

    for csv_name in csv_sources:
        p = mappings_dir / csv_name
        try:
            rows = _read_csv(p)
        except Exception:
            continue
        if not rows:
            continue

        first = rows[0]

        if csv_name == "retail_user.csv":
            # 알려진 스키마 사용
            name_col, code_col = "소매처명", "소매처코드"
            if name_col not in first or code_col not in first:
                continue
        else:
            # 기타 CSV: 첫 열 = 이름, 두 번째 열 = 코드
            # domae_retail_1.csv 는 코드→코드 매핑 — 첫 열이 숫자(도매코드)이므로
            # 유사도 검색 대상이 아님. normalize 후 숫자 문자열만 남으면 스킵.
            keys = list(first.keys())
            if len(keys) < 2:
                continue
            name_col, code_col = keys[0], keys[1]
            sample_name = normalize_ocr_name(first.get(name_col, ""))
            if sample_name.isdigit():
                continue  # 코드→코드 매핑 파일 스킵

        for row in rows:
            name_val = row.get(name_col, "")
            code_val = row.get(code_col, "")
            if not name_val or not code_val:
                continue
            norm_name = normalize_ocr_name(name_val)
            if not norm_name:
                continue
            score = difflib.SequenceMatcher(None, norm_query, norm_name).ratio()
            if score > 0.3:
                scored.append(RetailerCandidate(
                    retailer_code=code_val,
                    retailer_name=name_val,
                    source=csv_name,
                    similarity=round(score, 3),
                ))

    # 점수 내림차순 → 소매처코드 중복 제거 (최고 점수 유지)
    scored.sort(key=lambda x: x["similarity"], reverse=True)
    deduped: list[RetailerCandidate] = []
    for item in scored:
        code = item["retailer_code"]
        if code not in seen_codes:
            seen_codes.add(code)
            deduped.append(item)

    return deduped[:top_k]
