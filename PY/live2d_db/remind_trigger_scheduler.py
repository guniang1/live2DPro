"""``remind_trigger`` 定时扫描：到期且用户在线时经 ``/ws/chat`` 推送 ``remind_trigger`` 帧。

离线用户仅在下次建立 ``/ws/chat`` 时由 ``flush_pending_reminders_for_connection`` 补发。

投递语义：**仅**在 WebSocket 已成功下发 ``remind_trigger`` JSON（含非空 ``delivery_message``）后，将 ``is_triggered`` 置为 **1**；
否则保持 **0** 待下次扫描或重连补发。单进程内按 ``user_id`` 互斥，见 ``remind_trigger_delivery``。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

from live2d_db.connection import connection_ctx
from live2d_db.db_config import DbConfig
from live2d_db.entities import RemindDeliverOutcome
from live2d_db.remind_trigger_delivery import (
    deliver_remind_trigger,
    normalize_legacy_delivering_sync,
)
from live2d_db.repositories import RemindTriggerRepository

logger = logging.getLogger(__name__)

_stop_event: Optional[asyncio.Event] = None
_background_task: Optional[asyncio.Task[None]] = None
_idle_chitchat_task: Optional[asyncio.Task[None]] = None

# 默认 300 秒（5 分钟）一轮；可用 REMIND_TRIGGER_SCAN_INTERVAL_SEC 覆盖（最小 5 秒，联调可设更小）
_SCAN_INTERVAL_SEC = max(5, int(os.getenv("REMIND_TRIGGER_SCAN_INTERVAL_SEC", "300")))
_BATCH_LIMIT = max(1, min(int(os.getenv("REMIND_TRIGGER_SCAN_BATCH", "200")), 2000))
# 空闲闲聊检测间隔（默认 60 秒一轮，与到期扫描解耦）
_IDLE_CHITCHAT_CHECK_SEC = max(
    15, int(os.getenv("IDLE_CHITCHAT_CHECK_INTERVAL_SEC", "60"))
)


def _now_naive() -> datetime:
    return datetime.now()


def _list_pending_sync(before: datetime):
    with connection_ctx(DbConfig.from_env()) as conn:
        return RemindTriggerRepository.list_pending_before(conn, before, limit=_BATCH_LIMIT)


async def _sleep_interval_or_until_stop() -> None:
    assert _stop_event is not None
    try:
        await asyncio.wait_for(_stop_event.wait(), timeout=_SCAN_INTERVAL_SEC)
    except asyncio.TimeoutError:
        pass


async def run_scan_tick() -> dict[str, int]:
    """立即执行一轮到期提醒扫描；返回统计便于排查「无推送」原因。"""
    logger.info("remind_trigger 手动触发扫描（scan-now）")
    stats = await _tick()
    stats["idle_chitchat_delivered"] = await _run_idle_chitchat_tick()
    logger.info(
        "remind_trigger 扫描结束 pending_fetched=%s claimed=%s delivered=%s "
        "released_no_ws=%s idle_chitchat_delivered=%s",
        stats["pending_fetched"],
        stats["claimed"],
        stats["delivered"],
        stats["released_no_ws"],
        stats["idle_chitchat_delivered"],
    )
    return stats


async def _run_idle_chitchat_tick() -> int:
    """在线且超过空闲阈值的用户推送随机话题闲聊。"""
    from live2d_db.remind_trigger_delivery import deliver_idle_chitchat_to_user
    from router import wschat
    from utils.idle_chitchat import (
        idle_chitchat_enabled,
        idle_chitchat_seconds,
        mark_idle_chitchat_sent,
        should_offer_idle_chitchat,
    )

    if not idle_chitchat_enabled():
        return 0

    delivered = 0
    for uid in wschat.list_online_chat_user_ids():
        if not should_offer_idle_chitchat(uid):
            continue
        outcome = await deliver_idle_chitchat_to_user(uid)
        if outcome == RemindDeliverOutcome.JSON_SENT:
            mark_idle_chitchat_sent(uid)
            delivered += 1
            logger.info(
                "空闲闲聊已推送 user_id=%s idle_sec=%s",
                uid,
                idle_chitchat_seconds(),
            )
        elif outcome != RemindDeliverOutcome.ALREADY_HANDLED:
            logger.info(
                "空闲闲聊未推送 user_id=%s outcome=%s",
                uid,
                outcome.name,
            )
    return delivered


async def _tick() -> dict[str, int]:
    stats: dict[str, int] = {
        "pending_fetched": 0,
        "stale_reclaimed": 0,
        "claimed": 0,
        "delivered": 0,
        "released_no_ws": 0,
    }

    before = _now_naive()
    rows = await asyncio.to_thread(_list_pending_sync, before)
    stats["pending_fetched"] = len(rows)
    if not rows:
        logger.info(
            "remind_trigger 扫描：无到期待投递记录（is_triggered=0 且 trigger_time<=当前时间）"
        )
        return stats

    for t in rows:
        tid = t.trigger_id
        if tid is None:
            continue
        stats["claimed"] += 1
        outcome = await deliver_remind_trigger(t)
        if outcome == RemindDeliverOutcome.JSON_SENT:
            stats["delivered"] += 1
            logger.info(
                "remind_trigger 已完成 trigger_id=%s user_id=%s type=%s outcome=%s",
                tid,
                t.user_id,
                t.trigger_type,
                outcome.name,
            )
        elif outcome == RemindDeliverOutcome.ALREADY_HANDLED:
            pass
        else:
            stats["released_no_ws"] += 1
            logger.info(
                "remind_trigger 未下发 JSON，保持待投递 trigger_id=%s user_id=%s outcome=%s",
                tid,
                t.user_id,
                outcome.name,
            )
    return stats


async def _idle_chitchat_background_loop() -> None:
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            n = await _run_idle_chitchat_tick()
            if n:
                logger.info("空闲闲聊本轮推送 %s 人", n)
        except Exception:
            logger.exception("空闲闲聊后台 tick 异常")
        if _stop_event.is_set():
            break
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=_IDLE_CHITCHAT_CHECK_SEC)
        except asyncio.TimeoutError:
            pass


async def _background_loop() -> None:
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            await _tick()
        except Exception:
            logger.exception("remind_trigger 后台扫描 tick 异常")
        if _stop_event.is_set():
            break
        await _sleep_interval_or_until_stop()


async def start_remind_trigger_scheduler() -> None:
    global _stop_event, _background_task, _idle_chitchat_task
    if _background_task is not None and not _background_task.done():
        return
    legacy = await asyncio.to_thread(normalize_legacy_delivering_sync)
    if legacy:
        logger.info(
            "remind_trigger 已将 %s 条历史 is_triggered=2 重置为待投递 0",
            legacy,
        )
    _stop_event = asyncio.Event()
    _background_task = asyncio.create_task(_background_loop(), name="remind_trigger_scheduler")
    from utils.idle_chitchat import idle_chitchat_enabled, idle_chitchat_seconds

    if idle_chitchat_enabled():
        _idle_chitchat_task = asyncio.create_task(
            _idle_chitchat_background_loop(), name="idle_chitchat_scheduler"
        )
    else:
        _idle_chitchat_task = None

    logger.info(
        "remind_trigger 定时扫描已启动 interval=%ss batch_limit=%s idle_chitchat=%s "
        "idle_sec=%s idle_check_interval=%ss",
        _SCAN_INTERVAL_SEC,
        _BATCH_LIMIT,
        idle_chitchat_enabled(),
        idle_chitchat_seconds(),
        _IDLE_CHITCHAT_CHECK_SEC,
    )


async def stop_remind_trigger_scheduler() -> None:
    global _stop_event, _background_task, _idle_chitchat_task
    if _stop_event is not None:
        _stop_event.set()
    for task in (_background_task, _idle_chitchat_task):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _background_task = None
    _idle_chitchat_task = None
    _stop_event = None
    logger.info("remind_trigger 定时扫描已停止")
