"""
MinIO 资源 Redis 缓存（预签名 URL + 对象字节）。

环境变量::

    MINIO_REDIS_CACHE=1                      # 默认关闭；设为 1/true/on 启用
    MINIO_REDIS_CACHE_OBJECT_TTL=86400       # 对象字节缓存 TTL（秒）
    MINIO_REDIS_CACHE_OBJECT_MAX_BYTES=52428800  # 超过则不写入 Redis（默认 50MB）
    MINIO_REDIS_CACHE_PRESIGN_MARGIN=60      # 预签名 URL 在 Redis 中的 TTL = expires_in - margin

Redis 不可用时所有函数静默降级为直连 MinIO。
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

from minio.error import S3Error

from .minio_storage import get_bucket_name, get_minio_client, presigned_get_url
from .redis_factory import get_redis_binary_client, get_redis_client

logger = logging.getLogger(__name__)

_CACHE_VER = "v1"


def _bool_env(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def cache_enabled() -> bool:
    return _bool_env("MINIO_REDIS_CACHE", False)


def _digest(bucket: str, object_key: str) -> str:
    h = hashlib.sha256(f"{bucket}\0{object_key}".encode("utf-8")).hexdigest()
    return h


def _bytes_key(bucket: str, digest: str) -> str:
    return f"cubism:minio:bin:{_CACHE_VER}:{bucket}:{digest}"


def _presign_key(bucket: str, digest: str, expires_in: int) -> str:
    return f"cubism:minio:sgn:{_CACHE_VER}:{bucket}:{digest}:{expires_in}"


def _object_max_bytes() -> int:
    try:
        return max(1024, int(os.environ.get("MINIO_REDIS_CACHE_OBJECT_MAX_BYTES", str(50 * 1024 * 1024))))
    except ValueError:
        return 50 * 1024 * 1024


def _object_ttl_s() -> int:
    try:
        return max(60, int(os.environ.get("MINIO_REDIS_CACHE_OBJECT_TTL", "86400")))
    except ValueError:
        return 86400


def _presign_margin_s() -> int:
    try:
        return max(5, int(os.environ.get("MINIO_REDIS_CACHE_PRESIGN_MARGIN", "60")))
    except ValueError:
        return 60


def invalidate_object_cache(object_key: str, *, bucket: Optional[str] = None) -> None:
    """上传覆盖或删除对象后调用，清理字节缓存与匹配前缀的预签名条目。"""
    if not cache_enabled():
        return
    b = bucket or get_bucket_name()
    key_norm = object_key.lstrip("/")
    dig = _digest(b, key_norm)
    r_bin = get_redis_binary_client()
    r_txt = get_redis_client()
    if not r_bin and not r_txt:
        return
    bk = _bytes_key(b, dig)
    prefix = f"cubism:minio:sgn:{_CACHE_VER}:{b}:{dig}:"
    try:
        if r_bin:
            r_bin.delete(bk)
        cli_scan = r_txt or r_bin
        if cli_scan:
            for k in cli_scan.scan_iter(match=f"{prefix}*"):
                cli_scan.delete(k)
    except Exception:
        logger.debug("invalidate_object_cache 失败 key=%s", key_norm, exc_info=True)


def presigned_get_url_cached(
    object_name: str,
    *,
    bucket: Optional[str] = None,
    expires_in: int = 3600,
) -> str:
    """带 Redis 缓存的预签名 GET（缓存 TTL 短于签名有效期）。"""
    b = bucket or get_bucket_name()
    key_norm = object_name.lstrip("/")
    ttl = max(1, int(expires_in))

    if not cache_enabled():
        return presigned_get_url(key_norm, bucket=b, expires_in=ttl)

    r = get_redis_client()
    if not r:
        return presigned_get_url(key_norm, bucket=b, expires_in=ttl)

    dig = _digest(b, key_norm)
    rk = _presign_key(b, dig, ttl)
    try:
        cached = r.get(rk)
        if cached:
            return cached if isinstance(cached, str) else cached.decode("utf-8", errors="replace")
    except Exception:
        logger.debug("presign cache get miss/fail key=%s", key_norm, exc_info=True)

    url = presigned_get_url(key_norm, bucket=b, expires_in=ttl)
    margin = _presign_margin_s()
    redis_ttl = max(1, ttl - margin)
    try:
        r.setex(rk, redis_ttl, url)
    except Exception:
        logger.debug("presign cache set fail key=%s", key_norm, exc_info=True)
    return url


def get_object_bytes_cached(object_name: str, *, bucket: Optional[str] = None) -> bytes:
    """从 MinIO 读取对象字节；命中 Redis 则跳过 MinIO。"""
    b = bucket or get_bucket_name()
    key_norm = object_name.lstrip("/")

    if cache_enabled():
        r_bin = get_redis_binary_client()
        if r_bin:
            dig = _digest(b, key_norm)
            rk = _bytes_key(b, dig)
            try:
                hit = r_bin.get(rk)
                if hit:
                    return hit if isinstance(hit, (bytes, bytearray)) else bytes(hit)
            except Exception:
                logger.debug("minio bytes cache get fail key=%s", key_norm, exc_info=True)

    c = get_minio_client()
    try:
        resp = c.get_object(b, key_norm)
        try:
            data = resp.read()
        finally:
            resp.close()
            resp.release_conn()
    except S3Error:
        raise

    if cache_enabled() and data and len(data) <= _object_max_bytes():
        r_bin = get_redis_binary_client()
        if r_bin:
            try:
                dig = _digest(b, key_norm)
                r_bin.setex(_bytes_key(b, dig), _object_ttl_s(), data)
            except Exception:
                logger.debug("minio bytes cache set fail key=%s", key_norm, exc_info=True)

    return data
