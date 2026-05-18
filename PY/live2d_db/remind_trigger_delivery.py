"""到期关怀统一投递：``is_triggered`` 仅 **0** 待投递 / **1** 已成功下发。

单进程内按 ``user_id`` 使用 ``asyncio.Lock``，避免定时扫描与 WebSocket 补发并发重复推送。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

from fastapi import WebSocket

from live2d_db.connection import connection_ctx
from live2d_db.db_config import DbConfig
from live2d_db.entities import RemindDeliverOutcome, RemindTrigger
from live2d_db.repositories import RemindTriggerRepository

logger = logging.getLogger(__name__)

_user_locks: Dict[int, asyncio.Lock] = {}
_meta_lock = asyncio.Lock()


async def _lock_for_user(user_id: int) -> asyncio.Lock:
    async with _meta_lock:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _user_locks[user_id] = lock
        return lock


def normalize_legacy_delivering_sync() -> int:
    """将历史 ``is_triggered=2`` 重置为待投递 ``0``（升级后启动时调用一次即可）。"""
    with connection_ctx(DbConfig.from_env()) as conn:
        return RemindTriggerRepository.normalize_legacy_delivering(conn)


def _is_pending_sync(trigger_id: int) -> bool:
    with connection_ctx(DbConfig.from_env()) as conn:
        return RemindTriggerRepository.is_pending(conn, trigger_id)


def _mark_delivered_sync(trigger_id: int) -> bool:
    with connection_ctx(DbConfig.from_env()) as conn:
        return RemindTriggerRepository.mark_remind_delivered(conn, trigger_id)


async def deliver_remind_trigger(
    t: RemindTrigger,
    *,
    websocket: Optional[WebSocket] = None,
) -> RemindDeliverOutcome:
    """尝试投递一条到期提醒；成功下发 JSON 后将 ``is_triggered`` 置为 **1**，否则保持 **0**。

    ``websocket`` 非空时仅向该连接发送（补发）；否则向该用户所有在线 ``/ws/chat`` 广播。
    """
    from router import wschat

    tid = t.trigger_id
    if tid is None:
        return RemindDeliverOutcome.TRANSPORT_FAILED

    async with await _lock_for_user(t.user_id):
        if not await asyncio.to_thread(_is_pending_sync, tid):
            return RemindDeliverOutcome.ALREADY_HANDLED

        try:
            if websocket is not None:
                outcome, hist_text = await wschat._deliver_remind_trigger_on_websocket(
                    websocket, t.user_id, t
                )
                if outcome == RemindDeliverOutcome.JSON_SENT and hist_text:
                    await asyncio.to_thread(
                        wschat._persist_remind_delivery_to_chat_session,
                        t.user_id,
                        websocket,
                        t,
                        hist_text,
                    )
            else:
                outcome = await wschat.broadcast_remind_trigger_to_user(t.user_id, t)
        except Exception:
            logger.exception(
                "推送 remind_trigger 异常 trigger_id=%s user_id=%s",
                tid,
                t.user_id,
            )
            outcome = RemindDeliverOutcome.TRANSPORT_FAILED

        if outcome == RemindDeliverOutcome.JSON_SENT:
            marked = await asyncio.to_thread(_mark_delivered_sync, tid)
            if not marked:
                logger.warning(
                    "remind_trigger 已下发 JSON 但置 1 未命中（可能已被其他路径标记） trigger_id=%s",
                    tid,
                )
        return outcome


async def deliver_idle_chitchat_to_user(user_id: int) -> RemindDeliverOutcome:
    """空闲闲聊：无 ``remind_trigger`` 表行，成功下发 JSON 即可（不落库 is_triggered）。"""
    from router import wschat
    from utils.idle_chitchat import build_idle_chitchat_trigger

    if user_id < 1:
        return RemindDeliverOutcome.TRANSPORT_FAILED

    t = build_idle_chitchat_trigger(user_id)
    async with await _lock_for_user(user_id):
        try:
            return await wschat.broadcast_remind_trigger_to_user(user_id, t)
        except Exception:
            logger.exception("空闲闲聊推送异常 user_id=%s", user_id)
            return RemindDeliverOutcome.TRANSPORT_FAILED
