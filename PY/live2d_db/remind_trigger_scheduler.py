"""``remind_trigger`` 定时扫描：到期且用户在线时经 ``/ws/chat`` 推送 ``remind_trigger`` 帧。

离线用户仅在下次建立 ``/ws/chat`` 时由 ``flush_pending_reminders_for_connection`` 补发。
认领语义见 :meth:`RemindTriggerRepository.claim_pending_trigger`。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

from live2d_db.connection import connection_ctx
from live2d_db.db_config import DbConfig
from live2d_db.repositories import RemindTriggerRepository

logger = logging.getLogger(__name__)

_stop_event: Optional[asyncio.Event] = None
_background_task: Optional[asyncio.Task[None]] = None

# 默认 5 分钟；可用 REMIND_TRIGGER_SCAN_INTERVAL_SEC 覆盖（最小 5 秒）
_SCAN_INTERVAL_SEC = max(5, int(os.getenv("REMIND_TRIGGER_SCAN_INTERVAL_SEC", "300")))
_BATCH_LIMIT = max(1, min(int(os.getenv("REMIND_TRIGGER_SCAN_BATCH", "200")), 2000))


def _now_naive() -> datetime:
    return datetime.now()


def _list_pending_sync(before: datetime):
    with connection_ctx(DbConfig.from_env()) as conn:
        return RemindTriggerRepository.list_pending_before(conn, before, limit=_BATCH_LIMIT)


def _claim_sync(trigger_id: int) -> bool:
    with connection_ctx(DbConfig.from_env()) as conn:
        return RemindTriggerRepository.claim_pending_trigger(conn, trigger_id)


def _release_sync(trigger_id: int) -> None:
    with connection_ctx(DbConfig.from_env()) as conn:
        RemindTriggerRepository.release_trigger_claim(conn, trigger_id)


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
        "remind_trigger 扫描结束 pending_fetched=%s claimed=%s delivered=%s released_no_ws=%s",
        stats["pending_fetched"],
        stats["claimed"],
        stats["delivered"],
        stats["released_no_ws"],
    )
    return stats


async def _tick() -> dict[str, int]:
    from router import wschat

    stats: dict[str, int] = {
        "pending_fetched": 0,
        "claimed": 0,
        "delivered": 0,
        "released_no_ws": 0,
    }
    before = _now_naive()
    rows = await asyncio.to_thread(_list_pending_sync, before)
    stats["pending_fetched"] = len(rows)
    if not rows:
        logger.info(
            "remind_trigger 扫描：无到期未触发记录（需满足 is_triggered=0 且 trigger_time<=当前时间）"
        )
        return stats
    for t in rows:
        tid = t.trigger_id
        if tid is None:
            continue
        claimed = await asyncio.to_thread(_claim_sync, tid)
        if not claimed:
            continue
        stats["claimed"] += 1
        try:
            delivered = await wschat.broadcast_remind_trigger_to_user(t.user_id, t)
        except Exception:
            logger.exception("推送 remind_trigger 异常 trigger_id=%s user_id=%s", tid, t.user_id)
            delivered = False
        if not delivered:
            await asyncio.to_thread(_release_sync, tid)
            stats["released_no_ws"] += 1
            logger.info(
                "remind_trigger 未投递已释放认领 trigger_id=%s user_id=%s（该用户无在线 /ws/chat 或连接已断开）",
                tid,
                t.user_id,
            )
        else:
            stats["delivered"] += 1
            logger.info(
                "remind_trigger 已推送 trigger_id=%s user_id=%s type=%s",
                tid,
                t.user_id,
                t.trigger_type,
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
        "remind_trigger 定时扫描已启动 interval=%ss batch_limit=%s",
        _SCAN_INTERVAL_SEC,
        _BATCH_LIMIT,
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
