"""懒加载 Redis 客户端（wschat / http_api 共用）。

支持：
- ``REDIS_URL`` 与 ``REDIS_PASSWORD`` 分拆配置：URL 里无认证信息时自动注入密码；若 URL 已含 ``@`` 但仍需在环境变量中单独维护密码，则通过构造函数 ``password=`` 显式传入（覆盖 URL 解析结果）。
- 当系统环境里 ``REDIS_PASSWORD`` 为空字符串导致 ``load_dotenv`` 无法写入时，回退读取 ``PY/.env`` 中的 ``REDIS_PASSWORD``。
- 仅在服务端明确提示「未配置密码却收到 AUTH」时降级为无密码重试。
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse, urlunparse

logger = logging.getLogger(__name__)

_redis_lib: Any = None
_redis_client: Any = None
_redis_client_init_failed: bool = False
_redis_client_lock = threading.Lock()

_redis_binary_client: Any = None
_redis_binary_client_init_failed: bool = False
_redis_binary_client_lock = threading.Lock()

try:
    _redis_lib = importlib.import_module("redis")
except Exception:  # pragma: no cover
    _redis_lib = None


def _redis_url_inject_password(url: str, password: str) -> str:
    """若 URL 的 netloc 不含 ``@``（无 userinfo），则插入 ``:password@host``。"""
    u = urlparse(url.strip())
    netloc = u.netloc or ""
    if not password.strip():
        return url
    if "@" in netloc:
        return url
    quoted = quote(password, safe="")
    new_netloc = f":{quoted}@{netloc}"
    path = u.path if u.path else "/"
    return urlunparse((u.scheme or "redis", new_netloc, path, u.params, u.query, u.fragment))


def _effective_redis_password() -> Optional[str]:
    """优先 ``os.environ``；若为空则读 ``PY/.env``（避免系统环境里空 REDIS_PASSWORD 挡住 dotenv）。"""
    raw = (os.getenv("REDIS_PASSWORD") or "").strip()
    if raw:
        return raw
    try:
        from dotenv import dotenv_values

        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.is_file():
            raw2 = (dotenv_values(env_path).get("REDIS_PASSWORD") or "").strip()
            if raw2:
                return raw2
    except Exception:
        pass
    return None


def _server_rejects_auth_because_no_requirepass(exc: BaseException) -> bool:
    """判断是否「客户端发了 AUTH，但服务端未设置 requirepass」类错误。"""
    msg = str(exc).lower()
    needles = (
        "no password is set",
        "without any password configured",
        "called without any password configured",
        "does not require authentication",
    )
    return any(n in msg for n in needles)


def get_redis_client(log: Optional[logging.Logger] = None) -> Any:
    """返回可用 Redis 客户端，失败则返回 None（调用方可降级）。"""
    global _redis_client, _redis_client_init_failed
    lg = log or logger

    if _redis_client_init_failed:
        return None
    if _redis_client is not None:
        return _redis_client

    with _redis_client_lock:
        if _redis_client_init_failed:
            return None
        if _redis_client is not None:
            return _redis_client

        if _redis_lib is None:
            _redis_client_init_failed = True
            lg.warning("redis 依赖不可用，相关缓存/短期记忆降级为关闭")
            return None

        redis_url = (os.getenv("REDIS_URL") or "").strip()
        pwd_raw = _effective_redis_password() or ""
        pwd = pwd_raw if pwd_raw else None
        redis_kw: dict[str, Any] = {"decode_responses": True}
        if pwd is not None:
            redis_kw["password"] = pwd

        try:
            if redis_url:
                u0 = urlparse(redis_url)
                if pwd and "@" not in (u0.netloc or ""):
                    redis_url = _redis_url_inject_password(redis_url, pwd_raw)
                cli = _redis_lib.Redis.from_url(redis_url, **redis_kw)
            else:
                host = os.getenv("REDIS_HOST", "127.0.0.1")
                port = int(os.getenv("REDIS_PORT", "6379"))
                db = int(os.getenv("REDIS_DB", "0"))
                cli = _redis_lib.Redis(
                    host=host,
                    port=port,
                    db=db,
                    **redis_kw,
                )

            try:
                cli.ping()
            except Exception as e:
                # 仅当「服务端未启用 requirepass，却配置了 REDIS_PASSWORD」时降级无密码
                if redis_url or not pwd or not _server_rejects_auth_because_no_requirepass(e):
                    raise
                lg.warning(
                    "Redis 服务端当前未启用 requirepass，但 .env 中 REDIS_PASSWORD 非空，"
                    "AUTH 被拒绝（%s），已改用无密码重试。"
                    "若需要密码认证，请在 redis.conf 执行 requirepass <密码>（与 REDIS_PASSWORD 一致，例如 123456）并重启 Redis。",
                    e,
                )
                if redis_url:
                    u_plain = (os.getenv("REDIS_URL") or "").strip()
                    cli = _redis_lib.Redis.from_url(
                        u_plain, decode_responses=True, password=None
                    )
                else:
                    host = os.getenv("REDIS_HOST", "127.0.0.1")
                    port = int(os.getenv("REDIS_PORT", "6379"))
                    db = int(os.getenv("REDIS_DB", "0"))
                    cli = _redis_lib.Redis(
                        host=host,
                        port=port,
                        db=db,
                        password=None,
                        decode_responses=True,
                    )
                cli.ping()

            _redis_client = cli
            return _redis_client
        except Exception:
            _redis_client_init_failed = True
            lg.exception("Redis 连接失败，相关缓存/短期记忆降级为关闭")
            return None


def get_redis_binary_client(log: Optional[logging.Logger] = None) -> Any:
    """decode_responses=False，用于 MinIO 对象字节等二进制 VALUE（KEY 仍为 UTF-8 字符串）。"""
    global _redis_binary_client, _redis_binary_client_init_failed
    lg = log or logger

    if _redis_binary_client_init_failed:
        return None
    if _redis_binary_client is not None:
        return _redis_binary_client

    with _redis_binary_client_lock:
        if _redis_binary_client_init_failed:
            return None
        if _redis_binary_client is not None:
            return _redis_binary_client

        if _redis_lib is None:
            _redis_binary_client_init_failed = True
            return None

        redis_url = (os.getenv("REDIS_URL") or "").strip()
        pwd_raw = _effective_redis_password() or ""
        pwd = pwd_raw if pwd_raw else None
        redis_kw: dict[str, Any] = {"decode_responses": False}
        if pwd is not None:
            redis_kw["password"] = pwd

        try:
            if redis_url:
                u0 = urlparse(redis_url)
                if pwd and "@" not in (u0.netloc or ""):
                    redis_url = _redis_url_inject_password(redis_url, pwd_raw)
                cli = _redis_lib.Redis.from_url(redis_url, **redis_kw)
            else:
                host = os.getenv("REDIS_HOST", "127.0.0.1")
                port = int(os.getenv("REDIS_PORT", "6379"))
                db = int(os.getenv("REDIS_DB", "0"))
                cli = _redis_lib.Redis(
                    host=host,
                    port=port,
                    db=db,
                    **redis_kw,
                )

            try:
                cli.ping()
            except Exception as e:
                if redis_url or not pwd or not _server_rejects_auth_because_no_requirepass(e):
                    raise
                lg.warning(
                    "Redis（二进制客户端）无密码重试：%s",
                    e,
                )
                if redis_url:
                    u_plain = (os.getenv("REDIS_URL") or "").strip()
                    cli = _redis_lib.Redis.from_url(
                        u_plain, decode_responses=False, password=None
                    )
                else:
                    host = os.getenv("REDIS_HOST", "127.0.0.1")
                    port = int(os.getenv("REDIS_PORT", "6379"))
                    db = int(os.getenv("REDIS_DB", "0"))
                    cli = _redis_lib.Redis(
                        host=host,
                        port=port,
                        db=db,
                        password=None,
                        decode_responses=False,
                    )
                cli.ping()

            _redis_binary_client = cli
            return _redis_binary_client
        except Exception:
            _redis_binary_client_init_failed = True
            lg.exception("Redis 二进制客户端连接失败，MinIO 字节缓存降级为关闭")
            return None
