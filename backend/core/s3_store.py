"""S3 JSON 저장소 유틸리티.

Key 규칙:
  config/users.json                      — 사용자 목록
  documents/{doc_id}/meta.json           — 문서 메타·상태·토큰 사용량
  documents/{doc_id}/mappings.json       — 소매처·제품·판매처 매핑 목록
  documents/{doc_id}/reviews.json        — 리뷰 목록
"""
from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

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


def list_doc_ids() -> list[str]:
    """documents/ 하위의 doc_id 목록 반환."""
    prefix = "documents/"
    s3 = _get_s3()
    doc_ids: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=get_bucket(), Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            # "documents/<doc_id>/" → "<doc_id>"
            part = cp["Prefix"][len(prefix):].rstrip("/")
            if part:
                doc_ids.append(part)
    return doc_ids


# ── 도큐먼트 키 헬퍼 ───────────────────────────────────────────────────────────

def meta_key(doc_id: str) -> str:
    return f"documents/{doc_id}/meta.json"


def mappings_key(doc_id: str) -> str:
    return f"documents/{doc_id}/mappings.json"


def reviews_key(doc_id: str) -> str:
    return f"documents/{doc_id}/reviews.json"
