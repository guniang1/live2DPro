"""
本地 / 自建 MinIO（S3 兼容 API）上传与公开 URL 拼接。

依赖: pip install minio
环境变量（可与 PY/.env 一起由 dotenv 加载）::

    MINIO_ENDPOINT=localhost:9000
    MINIO_ACCESS_KEY=admin
    MINIO_SECRET_KEY=password
    MINIO_SECURE=false
    MINIO_BUCKET=live2d-assets
    # 浏览器/Live2D 加载用的基址（须与 MinIO 对外地址一致，换机器时改此项）
    MINIO_PUBLIC_BASE=http://localhost:9000
    # 显式 region 可避免 presign 等操作在未启动 MinIO 时仍去 GetBucketLocation（连不上会报错）
    MINIO_REGION=us-east-1

开发时若浏览器跨域失败，在 MinIO 控制台为该 Bucket 配置 CORS（允许你的前端 Origin）。

公开读：控制台 → Bucket → Access Policy，或对前缀设为只读（生产勿整桶公开）。
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Optional, Tuple

from minio import Minio
from minio.error import S3Error


def _bool_env(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def get_minio_client() -> Minio:
    endpoint = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
    access = os.environ.get("MINIO_ACCESS_KEY", "admin")
    secret = os.environ.get("MINIO_SECRET_KEY", "password")
    secure = _bool_env("MINIO_SECURE", False)
    region = (os.environ.get("MINIO_REGION") or "us-east-1").strip() or "us-east-1"
    return Minio(
        endpoint,
        access_key=access,
        secret_key=secret,
        secure=secure,
        region=region,
    )


def get_bucket_name() -> str:
    return os.environ.get("MINIO_BUCKET", "live2d-assets")


def get_public_base() -> str:
    return os.environ.get("MINIO_PUBLIC_BASE", "http://localhost:9000").rstrip("/")


def ensure_bucket(client: Optional[Minio] = None, bucket: Optional[str] = None) -> str:
    c = client or get_minio_client()
    b = bucket or get_bucket_name()
    if not c.bucket_exists(b):
        c.make_bucket(b)
    return b


def object_public_url(bucket: str, object_name: str) -> str:
    """Path-style URL: ``{base}/{bucket}/{key}``（与 MinIO 默认一致）。"""
    key = object_name.lstrip("/")
    return f"{get_public_base()}/{bucket}/{key}"


def upload_file(
    local_path: Path,
    object_name: str,
    *,
    bucket: Optional[str] = None,
    content_type: Optional[str] = None,
) -> Tuple[str, str]:
    """
    上传本地文件，返回 ``(object_name, public_url)``。
    ``object_name`` 建议用正斜杠路径，如 ``users/1/Xiaozi/motions/a.motion3.json``。
    """
    c = get_minio_client()
    b = ensure_bucket(c, bucket)
    path = Path(local_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    length = path.stat().st_size
    with path.open("rb") as f:
        c.put_object(
            b,
            object_name.lstrip("/"),
            f,
            length=length,
            content_type=content_type or "application/octet-stream",
        )
    oname = object_name.lstrip("/")
    try:
        from .minio_redis_cache import invalidate_object_cache

        invalidate_object_cache(oname, bucket=b)
    except Exception:
        pass
    return oname, object_public_url(b, object_name)


def upload_bytes(
    data: bytes,
    object_name: str,
    *,
    bucket: Optional[str] = None,
    content_type: str = "application/octet-stream",
) -> Tuple[str, str]:
    from io import BytesIO

    c = get_minio_client()
    b = ensure_bucket(c, bucket)
    bio = BytesIO(data)
    c.put_object(b, object_name.lstrip("/"), bio, length=len(data), content_type=content_type)
    oname = object_name.lstrip("/")
    try:
        from .minio_redis_cache import invalidate_object_cache

        invalidate_object_cache(oname, bucket=b)
    except Exception:
        pass
    return oname, object_public_url(b, object_name)


def delete_object(object_name: str, *, bucket: Optional[str] = None) -> None:
    """删除单个对象；不存在时忽略。"""
    c = get_minio_client()
    b = bucket or get_bucket_name()
    oname = object_name.lstrip("/")
    try:
        c.remove_object(b, oname)
    except S3Error as exc:
        if getattr(exc, "code", "") not in ("NoSuchKey", "NoSuchObject"):
            raise
    try:
        from .minio_redis_cache import invalidate_object_cache

        invalidate_object_cache(oname, bucket=b)
    except Exception:
        pass


def presigned_get_url(
    object_name: str,
    *,
    bucket: Optional[str] = None,
    expires_in: int = 3600,
) -> str:
    """
    生成下载临时链接（秒）。
    注意：MinIO/S3 对过期时长有上限，过大值会报错。
    """
    c = get_minio_client()
    b = bucket or get_bucket_name()
    sec = max(1, int(expires_in))
    return c.presigned_get_object(b, object_name.lstrip("/"), expires=timedelta(seconds=sec))
