"""``remind_trigger`` 定时扫描：到期且用户在线时经 ``/ws/chat`` 推送 ``remind_trigger`` 帧。

离线用户仅在下次建立 ``/ws/chat`` 时由 ``flush_pending_reminders_for_connection`` 补发。

投递语义：**仅**在 WebSocket 已成功下发 ``remind_trigger`` JSON（含非空 ``delivery_message``）后，将 ``is_triggered`` 置为 **1**。
未下发 JSON（无连接、发送失败、正文为空等）须 ``2→0`` 释放占用以便重试，**不得**标为已触发。
占用投递时用 **2**（投递中）防止并发重复下发；发送失败则 **2→0** 以便重试。
超时仍卡在 **2** 的记录由每轮扫描起始的回收逻辑重置为 **0**（见 ``REMIND_DELIVERY_STALE_SECONDS``）。
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
from live2d_db.repositories import RemindTriggerRepository

logger = logging.getLogger(__name__)

_stop_event: Optional[asyncio.Event] = None
_background_task: Optional[asyncio.Task[None]] = None

# 默认 300 秒（5 分钟）一轮；可用 REMIND_TRIGGER_SCAN_INTERVAL_SEC 覆盖（最小 5 秒，联调可设更小）
_SCAN_INTERVAL_SEC = max(5, int(os.getenv("REMIND_TRIGGER_SCAN_INTERVAL_SEC", "300")))
_BATCH_LIMIT = max(1, min(int(os.getenv("REMIND_TRIGGER_SCAN_BATCH", "200")), 2000))
_STALE_SEC = max(60, int(os.getenv("REMIND_DELIVERY_STALE_SECONDS", "900")))


def _now_naive() -> datetime:
    return datetime.now()


def _list_pending_sync(before: datetime):
    with connection_ctx(DbConfig.from_env()) as conn:
        return RemindTriggerRepository.list_pending_before(conn, before, limit=_BATCH_LIMIT)


def _reclaim_stale_sync() -> int:
    with connection_ctx(DbConfig.from_env()) as conn:
        return RemindTriggerRepository.reclaim_stale_delivering(conn, stale_seconds=_STALE_SEC)


def _begin_sync(trigger_id: int) -> bool:
    with connection_ctx(DbConfig.from_env()) as conn:
        return RemindTriggerRepository.begin_remind_delivery(conn, trigger_id)


def _finish_sync(trigger_id: int) -> None:
    with connection_ctx(DbConfig.from_env()) as conn:
        RemindTriggerRepository.finish_remind_delivery(conn, trigger_id)


def _release_sync(trigger_id: int) -> None:
    with connection_ctx(DbConfig.from_env()) as conn:
        RemindTriggerRepository.release_remind_delivery(conn, trigger_id)


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
    logger.info(
        "remind_trigger 扫描结束 pending_fetched=%s stale_reclaimed=%s claimed=%s delivered=%s released_no_ws=%s",
        stats["pending_fetched"],
        stats["stale_reclaimed"],
        stats["claimed"],
        stats["delivered"],
        stats["released_no_ws"],
    )
    return stats


async def _tick() -> dict[str, int]:
    from router import wschat

    stats: dict[str, int] = {
        "pending_fetched": 0,
        "stale_reclaimed": 0,
        "claimed": 0,
        "delivered": 0,
        "released_no_ws": 0,
    }
    reclaimed = await asyncio.to_thread(_reclaim_stale_sync)
    stats["stale_reclaimed"] = reclaimed
    if reclaimed:
        logger.info(
            "remind_trigger 回收超时投递中记录 %s 条（is_triggered=2，>%ss 视为卡住）",
            reclaimed,
            _STALE_SEC,
        )

    before = _now_naive()
    rows = await asyncio.to_thread(_list_pending_sync, before)
    stats["pending_fetched"] = len(rows)
    if not rows:
        logger.info(
            "remind_trigger 扫描：无到期待投递记录（is_triggered=0 且 trigger_time<=当前时间；"
            "投递中 is_triggered=2 不计入本列表）"
        )
        return stats
    for t in rows:
        tid = t.trigger_id
        if tid is None:
            continue
        begun = await asyncio.to_thread(_begin_sync, tid)
        if not begun:
            continue
        stats["claimed"] += 1
        try:
            outcome = await wschat.broadcast_remind_trigger_to_user(t.user_id, t)
        except Exception:
            logger.exception("推送 remind_trigger 异常 trigger_id=%s user_id=%s", tid, t.user_id)
            outcome = RemindDeliverOutcome.TRANSPORT_FAILED
        if outcome == RemindDeliverOutcome.JSON_SENT:
            await asyncio.to_thread(_finish_sync, tid)
            stats["delivered"] += 1
            logger.info(
                "remind_trigger 已完成 trigger_id=%s user_id=%s type=%s outcome=%s",
                tid,
                t.user_id,
                t.trigger_type,
                outcome.name,
            )
        else:
            await asyncio.to_thread(_release_sync, tid)
            stats["released_no_ws"] += 1
            logger.info(
                "remind_trigger 未下发 JSON，已释放占用 trigger_id=%s user_id=%s outcome=%s",
                tid,
                t.user_id,
                outcome.name,
            )
    return stats


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
    global _stop_event, _background_task
    if _background_task is not None and not _background_task.done():
        return
    _stop_event = asyncio.Event()
    _background_task = asyncio.create_task(_background_loop(), name="remind_trigger_scheduler")
    logger.info(
        "remind_trigger 定时扫描已启动 interval=%ss batch_limit=%s stale_reclaim=%ss",
        _SCAN_INTERVAL_SEC,
        _BATCH_LIMIT,
        _STALE_SEC,
    )


async def stop_remind_trigger_scheduler() -> None:
    global _stop_event, _background_task
    if _stop_event is not None:
        _stop_event.set()
    if _background_task is not None:
        _background_task.cancel()
        try:
            await _background_task
        except asyncio.CancelledError:
            pass
        _background_task = None
    _stop_event = None
    logger.info("remind_trigger 定时扫描已停止")
