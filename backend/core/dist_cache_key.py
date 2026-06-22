"""dist 캐시(ocr_dist) 키 스키마 — 단일 출처(single source of truth).

ocr_dist 캐시는 **전 양식이 공유하는 하나의 시트**다(form_id는 별도 시트가 아니라
키의 한 값). 키는 두 부류로 구성된다:

  - CONTEXT_FIELDS:   문서·발행처 식별 (form_id, issuer_fingerprint)
  - DIMENSION_FIELDS: 같은 소매처를 서로 다른 판매처로 가르는 차원
                      (retailer_code, jisho) — "같은 소매처라도 入出荷支店이 다르면
                      판매처가 갈린다"는 업무규칙을 키로 표현한 부분

배경: 캐시 빌드·조회·쓰기 **세 군데**가 이 키를 각자 하드코딩하던 탓에
`(form_id, issuer_fp, retailer_code)` 3튜플과 `(…, jisho)` 4튜플이 경로마다
갈리는 드리프트가 있었다(jisho 추가가 일부 경로에만 반영). 이 모듈이 키 스키마의
유일한 정의처이며, 세 경로 모두 여기서 키·헤더를 받아 쓴다.

새 차원 추가(예: channel):
  1. DIMENSION_FIELDS에 필드명 추가              ← 여기 한 줄
  2. ocr_dist 시트에 동명 컬럼 추가              ← 데이터 작업(현업)
  3. 그 차원 값을 items에 채우고 순회하는 plumbing ← 그 차원 고유 코드(불가피한 T3)
3번이 차원별 본질 비용이다. 1·2로 키 스키마는 자동 일관 적용된다.

설계: docs/registry-driven-primitives.md (축 B)
"""

# 키 컬럼 — 순서가 곧 ocr_dist 시트의 컬럼 순서다. 변경 시 시트 마이그레이션 필요.
CONTEXT_FIELDS:   tuple[str, ...] = ("form_id", "issuer_fingerprint")
DIMENSION_FIELDS: tuple[str, ...] = ("retailer_code", "jisho")

KEY_FIELDS:    tuple[str, ...] = CONTEXT_FIELDS + DIMENSION_FIELDS
CACHE_HEADERS: list[str]       = list(KEY_FIELDS) + ["dist_code", "dist_name"]
# Sheets upsert_row 의 키 인덱스 (복합키 컬럼 위치) — KEY_FIELDS 길이에서 자동 도출.
KEY_INDICES:   list[int]       = list(range(len(KEY_FIELDS)))


def key_from_mapping(d: dict) -> tuple:
    """행/컨텍스트 dict → 복합키 튜플. 누락 필드는 ""(구 행 호환)."""
    return tuple(d.get(f, "") for f in KEY_FIELDS)


def row_from_mapping(d: dict, dist_code: str, dist_name: str = "") -> list:
    """CACHE_HEADERS 순서의 행 리스트(키 값 + dist_code + dist_name)."""
    return [d.get(f, "") for f in KEY_FIELDS] + [dist_code, dist_name]
