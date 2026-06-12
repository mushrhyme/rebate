"""S3 JSON 저장소 유틸리티.

Key 규칙:
  config/users.json                          — 사용자 목록
  documents/{doc_id}/meta.json              — 문서 메타·상태·토큰 사용량
  documents/{doc_id}/mappings.json          — 소매처·제품·판매처 매핑 목록
  documents/{doc_id}/reviews.json           — 리뷰 목록
  documents/{doc_id}/original.pdf          — PDF 원본
  documents/{doc_id}/pages/page_NNN.*      — OCR 결과 (PNG + txt + json)
  documents/{doc_id}/extracted/*           — Phase 1~4 산출물 JSON
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

# ── doc_id별 asyncio Lock ─────────────────────────────────────────────────────
# 단일 프로세스(asyncio 이벤트 루프) 내에서 같은 doc_id의 meta/mappings/reviews.json에
# 대한 read-modify-write를 직렬화한다.
#
# 보호 대상: queries.py의 모든 write 함수
# 보호 범위: 단일 EC2 프로세스. 다중 프로세스 환경으로 확장 시
#            S3 conditional put (ETag + IfMatch) 추가 필요.
#
# asyncio 단일 스레드이므로 _doc_locks dict 자체에는 별도 동기화가 불필요하다.

_doc_locks: dict[str, asyncio.Lock] = {}


def get_doc_lock(doc_id: str) -> asyncio.Lock:
    """doc_id별 asyncio.Lock 반환 (없으면 생성)."""
    if doc_id not in _doc_locks:
        _doc_locks[doc_id] = asyncio.Lock()
    return _doc_locks[doc_id]

logger = logging.getLogger(__name__)

_s3_client = None


def _get_s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name="ap-northeast-2")
    return _s3_client


def get_bucket() -> str:
    from .config import get_settings
    return get_settings().aws_s3_bucket


# ── 저수준 read/write ──────────────────────────────────────────────────────────

def read_json(key: str) -> Any | None:
    """S3에서 JSON 읽기. 키 없으면 None 반환."""
    try:
        resp = _get_s3().get_object(Bucket=get_bucket(), Key=key)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return None
        raise


def read_text(key: str) -> str | None:
    """S3에서 텍스트 읽기. 키 없으면 None 반환."""
    try:
        resp = _get_s3().get_object(Bucket=get_bucket(), Key=key)
        return resp["Body"].read().decode("utf-8")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return None
        raise


def write_text(key: str, text: str) -> None:
    """S3에 텍스트 쓰기 (EC2 런타임 변경 파일 미러용)."""
    _get_s3().put_object(
        Bucket=get_bucket(),
        Key=key,
        Body=text.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )


def write_json(key: str, data: Any) -> None:
    """S3에 JSON 쓰기."""
    _get_s3().put_object(
        Bucket=get_bucket(),
        Key=key,
        Body=json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
        ContentType="application/json",
    )


def delete_key(key: str) -> None:
    try:
        _get_s3().delete_object(Bucket=get_bucket(), Key=key)
    except ClientError:
        pass


def list_keys(prefix: str) -> list[str]:
    """prefix 하위 모든 키 반환 (페이지네이션 처리)."""
    s3 = _get_s3()
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=get_bucket(), Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


# ── 파일 업로드 / 다운로드 ────────────────────────────────────────────────────────

def upload_file(local_path: Path, key: str) -> None:
    """로컬 파일을 S3에 업로드."""
    _get_s3().upload_file(str(local_path), get_bucket(), key)


def download_file(key: str, local_path: Path) -> None:
    """S3에서 로컬 경로로 다운로드. 부모 디렉토리를 자동 생성."""
    local_path = Path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    _get_s3().download_file(get_bucket(), key, str(local_path))


def file_exists(key: str) -> bool:
    """S3 키 존재 여부 확인."""
    try:
        _get_s3().head_object(Bucket=get_bucket(), Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def upload_dir(local_dir: Path, prefix: str) -> int:
    """local_dir 이하 모든 파일을 S3의 prefix 하위에 업로드. 파일 수 반환."""
    local_dir = Path(local_dir)
    count = 0
    for f in local_dir.rglob("*"):
        if f.is_file():
            rel = f.relative_to(local_dir)
            key = f"{prefix}/{rel}".replace("\\", "/")
            upload_file(f, key)
            count += 1
    return count


def download_dir(prefix: str, local_dir: Path) -> int:
    """S3 prefix 하위의 모든 파일을 local_dir에 복원. 파일 수 반환."""
    local_dir = Path(local_dir)
    keys = list_keys(prefix)
    count = 0
    for key in keys:
        rel = key[len(prefix):].lstrip("/")
        if rel:
            local_path = local_dir / rel
            try:
                download_file(key, local_path)
                count += 1
            except Exception:
                logger.warning("S3 다운로드 실패: %s", key)
    return count


def list_doc_ids() -> list[str]:
    """documents/ 하위의 doc_id 목록 반환. NFC 정규화 후 중복 제거."""
    import unicodedata
    prefix = "documents/"
    s3 = _get_s3()
    seen: set[str] = set()
    doc_ids: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=get_bucket(), Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            # "documents/<doc_id>/" → "<doc_id>"
            part = unicodedata.normalize("NFC", cp["Prefix"][len(prefix):].rstrip("/"))
            if part and part not in seen:
                seen.add(part)
                doc_ids.append(part)
    return doc_ids


# ── 도큐먼트 키 헬퍼 ───────────────────────────────────────────────────────────

def meta_key(doc_id: str) -> str:
    return f"documents/{doc_id}/meta.json"


def mappings_key(doc_id: str) -> str:
    return f"documents/{doc_id}/mappings.json"


def reviews_key(doc_id: str) -> str:
    return f"documents/{doc_id}/reviews.json"
