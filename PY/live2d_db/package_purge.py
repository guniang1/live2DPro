"""删除用户某模型包时，级联清理 MySQL、Redis 与 MinIO 关联数据。"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

import pymysql

from .package_key_util import normalize_package_key
from .repositories import (
    ChatSessionRepository,
    Live2dModelAssetRepository,
    Live2dTtsReferRepository,
    LongMemoryRepository,
    PersonaRepository,
    RemindTriggerRepository,
)

logger = logging.getLogger(__name__)


def collect_package_key_aliases(
    conn: pymysql.connections.Connection, user_id: int, package_key: str
) -> list[str]:
    """收集同一逻辑包在库中可能出现的 ``package_key`` 写法（含规范化键）。"""
    pkg_norm = normalize_package_key(package_key)
    keys: set[str] = set()
    raw = (package_key or "").strip()
    if raw:
        keys.add(raw)
    keys.add(pkg_norm)

    for k in ChatSessionRepository.distinct_package_keys_for_user(conn, user_id):
        if normalize_package_key(k) == pkg_norm:
            keys.add(k)

    for row in LongMemoryRepository.list_by_user(conn, user_id, limit=500, offset=0):
        pk = (row.package_key or "").strip()
        if pk and normalize_package_key(pk) == pkg_norm:
            keys.add(pk)

    for asset in Live2dModelAssetRepository.list_by_user(conn, user_id, limit=2000, offset=0):
        pk = (asset.package_key or "").strip()
        if pk and normalize_package_key(pk) == pkg_norm:
            keys.add(pk)

    for refer in Live2dTtsReferRepository.list_by_user(conn, user_id):
        pk = (refer.package_key or "").strip()
        if pk and normalize_package_key(pk) == pkg_norm:
            keys.add(pk)

    for p in PersonaRepository.list_package_personas_for_user(conn, user_id):
        pk = (p.package_key or "").strip()
        if pk and normalize_package_key(pk) == pkg_norm:
            keys.add(pk)

    return sorted(keys)


def purge_user_package_data(
    conn: pymysql.connections.Connection,
    user_id: int,
    package_key: str,
    *,
    redis_cli: Any = None,
) -> dict[str, int]:
    """
    删除该用户该模型包下的关联数据（资源索引、人设、音色、记忆、对话、提醒等）。
    返回各步骤受影响行数，键名见返回值。
    """
    aliases = collect_package_key_aliases(conn, user_id, package_key)
    pkg_norm = normalize_package_key(package_key)

    tts_object_keys: list[str] = []
    for pk in aliases:
        refer = Live2dTtsReferRepository.get_by_user_and_package(conn, user_id, pk)
        if refer and refer.audio_object_key:
            tts_object_keys.append(str(refer.audio_object_key).strip())

    asset_object_keys: list[str] = []
    for pk in aliases:
        for asset in Live2dModelAssetRepository.list_by_package(conn, user_id, pk, limit=5000):
            if asset.object_key:
                asset_object_keys.append(str(asset.object_key).strip())

    n_remind = RemindTriggerRepository.delete_by_user_package_keys(conn, user_id, aliases)
    n_chat = ChatSessionRepository.delete_by_user_package_keys(conn, user_id, aliases)
    n_memory = LongMemoryRepository.delete_by_user_package_keys(conn, user_id, aliases)

    n_tts = 0
    for pk in aliases:
        n_tts += Live2dTtsReferRepository.delete_by_user_and_package(conn, user_id, pk)

    n_persona = 0
    resolved = PersonaRepository.resolve_persona_for_package(conn, user_id, package_key)
    if resolved and resolved.persona_id is not None:
        n_persona += PersonaRepository.delete_by_id(conn, int(resolved.persona_id))
    for pk in aliases:
        n_persona += PersonaRepository.delete_by_user_and_package(conn, user_id, pk)

    n_assets = 0
    for pk in aliases:
        n_assets += Live2dModelAssetRepository.delete_by_package_key(conn, pk, user_id)

    if redis_cli is not None:
        try:
            from . import memory_layers as _mem

            _mem.delete_memory_keys(redis_cli, user_id, pkg_norm)
            _mem.delete_mimo_director_persona_cached(redis_cli, user_id, pkg_norm)
        except Exception:
            logger.exception("清理 Redis 记忆/人设缓存失败 user_id=%s pkg=%s", user_id, pkg_norm)

    _best_effort_delete_minio_objects(tts_object_keys + asset_object_keys)

    return {
        "model_assets": n_assets,
        "personas": n_persona,
        "tts_refers": n_tts,
        "long_memories": n_memory,
        "chat_sessions": n_chat,
        "remind_triggers": n_remind,
    }


def _best_effort_delete_minio_objects(object_keys: Sequence[str]) -> None:
    seen: set[str] = set()
    keys = [k for k in object_keys if k and k not in seen and not seen.add(k)]
    if not keys:
        return
    try:
        from .minio_storage import delete_object
    except ImportError:
        return
    for key in keys:
        try:
            delete_object(key)
        except Exception:
            logger.warning("MinIO 删除对象失败 key=%s", key, exc_info=True)
