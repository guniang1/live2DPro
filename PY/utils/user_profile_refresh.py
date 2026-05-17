"""登录用户画像：近 24h 有对话且距上次更新满 24h 时，由后台从 ``chat_session`` 增量取材经 LLM 合并写入 ``user_profile``。

- 后台轮询：``start_user_profile_consolidator``（``main.py`` lifespan）。
- 主对话 system 可拼接画像块（``format_profile_for_chat_system``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Optional

import ollama

from live2d_db.connection import connection_ctx
from live2d_db.db_config import DbConfig
from live2d_db.entities import ChatSession, UserProfile
from live2d_db.repositories import ChatSessionRepository, UserProfileRepository

logger = logging.getLogger(__name__)

_CHAT_SESSION_MAX = 30000
_DISPLAY_NAME_MAX = 64
_USER_TAGS_MAX = 255
_EMOTION_MAX = 30
_PREFS_MAX = 8000
_TROUBLE_MAX = 8000
_AMBIGUOUS_NAMES = frozenset({"暂不明确", "未知", "无", "（无）", "不明确"})

_stop_event: Optional[asyncio.Event] = None
_background_task: Optional[asyncio.Task[None]] = None


def _poll_interval_sec() -> int:
    try:
        n = int((os.getenv("USER_PROFILE_REFRESH_INTERVAL_SEC") or "300").strip() or "300")
    except ValueError:
        return 300
    return max(60, min(86400, n))


def _min_gap_sec() -> int:
    try:
        n = int((os.getenv("USER_PROFILE_MIN_GAP_SEC") or "86400").strip() or "86400")
    except ValueError:
        return 86400
    return max(3600, min(86400 * 14, n))


def _source_window_sec() -> int:
    try:
        n = int((os.getenv("USER_PROFILE_SOURCE_WINDOW_SEC") or "86400").strip() or "86400")
    except ValueError:
        return 86400
    return max(3600, min(86400 * 14, n))


def refresh_enabled() -> bool:
    return os.getenv("USER_PROFILE_REFRESH_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def chat_inject_enabled() -> bool:
    return os.getenv("USER_PROFILE_IN_CHAT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def chat_session_block_max_chars() -> int:
    try:
        n = int((os.getenv("USER_PROFILE_REFRESH_CHAT_MAX_CHARS") or "30000").strip() or "30000")
    except ValueError:
        return _CHAT_SESSION_MAX
    return max(2000, min(50000, n))


def _ollama_client() -> ollama.Client:
    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    return ollama.Client(host=host)


def model_name() -> str:
    return (
        os.getenv("USER_PROFILE_REFRESH_MODEL") or os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    ).strip()


def _ollama_message_content(resp: object) -> str:
    if resp is None:
        return ""
    if isinstance(resp, dict):
        m = resp.get("message")
        if isinstance(m, dict):
            return str(m.get("content") or "").strip()
        if m is not None:
            c = getattr(m, "content", None)
            return str(c).strip() if c is not None else ""
        return ""
    m = getattr(resp, "message", None)
    if m is None:
        return ""
    if isinstance(m, dict):
        return str(m.get("content") or "").strip()
    c = getattr(m, "content", None)
    return str(c).strip() if c is not None else ""


def _extract_json_blob(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    if "```" in text:
        parts = text.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                text = p
                break
    lo = text.find("{")
    hi = text.rfind("}")
    blob = text[lo : hi + 1] if lo >= 0 and hi > lo else ""
    if not blob:
        return None
    try:
        parsed = json.loads(blob)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _truncate(s: str, lim: int) -> str:
    s = (s or "").strip()
    if len(s) <= lim:
        return s
    return s[: max(1, lim - 1)].rstrip() + "…"


def format_chat_sessions_block(rows: list[ChatSession]) -> str:
    """将 ``chat_session`` 行格式化为 LLM 阅读的对话正文（时间正序）。"""
    if not rows:
        return ""
    lines: list[str] = []
    for i, row in enumerate(rows, start=1):
        pkg = (row.package_key or "").strip() or "default"
        u = (row.user_input or "").strip()
        a = (row.ai_reply or "").strip()
        if u:
            lines.append(f"第{i}轮[{pkg}] 用户：{u}")
        if a:
            lines.append(f"第{i}轮[{pkg}] 助手：{a}")
    blob = "\n".join(lines).strip()
    return _truncate(blob, chat_session_block_max_chars())


def format_stored_profile_block(p: Optional[UserProfile]) -> str:
    """供 LLM 阅读的「旧用户画像」正文。"""
    if p is None:
        return (
            "（尚无已存画像，请仅依据近期对话归纳；"
            "字段仍须填满合理默认值如「暂不明确」「无明显困扰」等简短用语）"
        )
    parts: list[str] = []
    name = (p.display_name or "").strip()
    tags = (p.user_tags or "").strip()
    emo = (p.emotion_state or "").strip()
    pref = (p.preferences or "").strip()
    trouble = (p.trouble_events or "").strip()
    parts.append(f"称呼：{name or '（空）'}")
    parts.append(f"用户标签：{tags or '（空）'}")
    parts.append(f"情感状态：{emo or '（空）'}")
    parts.append(f"偏好与习惯：{pref or '（空）'}")
    parts.append(f"困扰与压力事件：{trouble or '（空）'}")
    return "\n".join(parts)


def format_profile_for_chat_system(p: Optional[UserProfile]) -> str:
    """拼入主对话 system 的画像块；若不存在或非空字段都没有则返回空串。"""
    if p is None:
        return ""
    name = (p.display_name or "").strip()
    tags = (p.user_tags or "").strip()
    emo = (p.emotion_state or "").strip()
    pref = (p.preferences or "").strip()
    trouble = (p.trouble_events or "").strip()
    if not any((name, tags, emo, pref, trouble)):
        return ""
    lines = [
        "【用户画像】",
        "（以下描述对话对象「用户」，不是【角色人设】中的角色本人）",
    ]
    if name and name not in _AMBIGUOUS_NAMES:
        lines.append(f"- 称呼：{name}")
    if tags:
        lines.append(f"- 标签：{tags}")
    if emo:
        lines.append(f"- 当前情感概况：{emo}")
    if pref:
        lines.append(f"- 偏好与习惯：{pref}")
    if trouble:
        lines.append(f"- 困扰与压力：{trouble}")
    return "\n".join(lines)


def _call_profile_llm(old_block: str, dialogue_block: str) -> dict[str, Any] | None:
    system = (
        "你是用户画像维护模块。输入包含【旧用户画像】与【近期对话】（自上次更新以来的增量，"
        "可能跨多个模型包）。\n"
        "任务：综合二者输出**更新后的完整画像**，继承旧画像中仍成立的信息，用新对话修正或补充；"
        "不要编造对话未出现的具体姓名、日期、成绩等。\n"
        "**字段分工**：用户自称/常用称呼**只**写入 display_name，且只写一次；"
        "user_tags 写身份/状态类短标签（如考研党、晚间活跃），**禁止**在 tags 中重复称呼或写「称呼:XX」；"
        "preferences / trouble_events 勿与 tags 复述同一事实；"
        "**勿**写入近期话题流水账（系统另有长期记忆模块）。\n\n"
        "输出严格为一个 JSON 对象，键名必须为：\n"
        "- display_name：字符串，用户在对白中亲口使用的自称或希望被称呼的名字；"
        "无则写「暂不明确」；禁止写入角色（助手）的名字；\n"
        "- user_tags：字符串，若干短标签用中文顿号或逗号分隔，总长度不超过 240 字；\n"
        "- emotion_state：字符串，概括用户当前情绪基调，不超过 28 字；\n"
        "- preferences：字符串，用户偏好、兴趣、沟通习惯等；\n"
        "- trouble_events：字符串，困扰、压力源或诉求；若无则写「无明显困扰」或「暂不明确」。\n\n"
        "不要输出 JSON 以外的任何文字。"
    )
    user_content = (
        "【旧用户画像】\n"
        + old_block
        + "\n\n【近期对话】\n"
        + (dialogue_block or "（空）")
    )
    opts: dict[str, Any] = {"num_predict": 2048, "temperature": 0.15, "format": "json"}
    cli = _ollama_client()
    model = model_name()
    try:
        r = cli.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            options=opts,
        )
    except Exception as e:
        logger.warning("用户画像刷新 LLM 失败 model=%s: %s", model, e)
        try:
            del opts["format"]
            r = cli.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
                options=opts,
            )
        except Exception as e2:
            logger.warning("用户画像刷新 LLM 重试失败: %s", e2)
            return None
    raw = _ollama_message_content(r)
    return _extract_json_blob(raw)


def _coerce_profile_fields(obj: dict[str, Any]) -> Optional[UserProfile]:
    name = _truncate(str(obj.get("display_name") or "").strip(), _DISPLAY_NAME_MAX)
    tags = _truncate(str(obj.get("user_tags") or "").strip(), _USER_TAGS_MAX)
    emo = _truncate(str(obj.get("emotion_state") or "").strip(), _EMOTION_MAX)
    pref = _truncate(str(obj.get("preferences") or "").strip(), _PREFS_MAX)
    trouble = _truncate(str(obj.get("trouble_events") or "").strip(), _TROUBLE_MAX)
    if name in _AMBIGUOUS_NAMES:
        name = ""
    if not any((name, tags, emo, pref, trouble)):
        return None
    return UserProfile(
        user_id=0,
        display_name=name or None,
        user_tags=tags or None,
        emotion_state=emo or None,
        preferences=pref or None,
        trouble_events=trouble or None,
    )


def _dedupe_tags_from_display_name(p: UserProfile) -> None:
    name = (p.display_name or "").strip()
    tags = (p.user_tags or "").strip()
    if not name or not tags:
        return
    parts = re.split(r"[、,，;；\s]+", tags)
    kept = [x.strip() for x in parts if x.strip() and x.strip() != name and name not in x.strip()]
    p.user_tags = "、".join(kept) if kept else None


def _merge_profile_with_existing(merged: UserProfile, old: Optional[UserProfile]) -> UserProfile:
    if old is None:
        _dedupe_tags_from_display_name(merged)
        return merged
    if not (merged.display_name or "").strip():
        merged.display_name = old.display_name
    for attr in ("user_tags", "emotion_state", "preferences", "trouble_events"):
        if not (getattr(merged, attr) or "").strip():
            setattr(merged, attr, getattr(old, attr))
    _dedupe_tags_from_display_name(merged)
    return merged


def consolidate_user_profile(conn: Any, user_id: int, *, manual: bool = False) -> bool:
    """对单个用户执行一轮画像合并；成功写入返回 True。

    ``manual=True``（如 ``POST consolidate-now``）：取 24h 窗内全部会话，不受后台最短间隔限制。
    """
    now = datetime.now()
    window_start = now - timedelta(seconds=_source_window_sec())
    old = UserProfileRepository.get_by_user_id(conn, user_id)
    if manual:
        since_exclusive = None
    else:
        since_exclusive = old.update_time if old is not None else None

    raw_keys = ChatSessionRepository.distinct_package_keys_for_user(conn, user_id)
    if not raw_keys:
        logger.info("用户画像跳过：无 chat_session 记录 user_id=%s", user_id)
        return False

    rows = ChatSessionRepository.list_for_long_memory_window(
        conn,
        user_id,
        raw_keys,
        since_exclusive=since_exclusive,
        window_start=window_start,
        limit=5000,
    )
    if not rows:
        logger.info(
            "用户画像跳过：窗内无增量对话 user_id=%s since=%s",
            user_id,
            since_exclusive,
        )
        return False

    dialogue = format_chat_sessions_block(rows)
    if not dialogue.strip():
        return False

    old_block = format_stored_profile_block(old)
    obj = _call_profile_llm(old_block, dialogue)
    if not obj:
        logger.info(
            "用户画像跳过：LLM 不可解析 user_id=%s model=%s",
            user_id,
            model_name(),
        )
        return False
    merged = _coerce_profile_fields(obj)
    if merged is None:
        logger.info("用户画像跳过：字段全空 user_id=%s", user_id)
        return False
    merged.user_id = user_id
    merged = _merge_profile_with_existing(merged, old)
    UserProfileRepository.upsert_by_user_id(conn, merged)
    logger.info(
        "用户画像已刷新 user_id=%s rows=%s model=%s",
        user_id,
        len(rows),
        model_name(),
    )
    return True


def _run_tick() -> list[int]:
    """扫描候选并刷新；返回本次成功写入的 user_id 列表。"""
    if not refresh_enabled():
        return []
    window = _source_window_sec()
    gap = _min_gap_sec()
    with connection_ctx(DbConfig.from_env()) as conn:
        candidates = UserProfileRepository.list_candidates_for_profile_refresh(
            conn, window, gap
        )
    updated: list[int] = []
    for uid in candidates:
        try:
            with connection_ctx(DbConfig.from_env()) as conn2:
                ok = consolidate_user_profile(conn2, uid)
            if ok:
                updated.append(uid)
        except Exception:
            logger.exception("用户画像单用户刷新异常 user_id=%s", uid)
    return updated


async def _sleep_interval_or_until_stop() -> None:
    assert _stop_event is not None
    interval = float(_poll_interval_sec())
    t_stop = asyncio.create_task(_stop_event.wait())
    t_sleep = asyncio.create_task(asyncio.sleep(interval))
    _done, pending = await asyncio.wait({t_stop, t_sleep}, return_when=asyncio.FIRST_COMPLETED)
    for p in pending:
        p.cancel()


async def _background_loop() -> None:
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            await asyncio.to_thread(_run_tick)
        except Exception:
            logger.exception("用户画像后台 tick 异常")
        if _stop_event.is_set():
            break
        await _sleep_interval_or_until_stop()


async def start_user_profile_consolidator() -> None:
    """在 FastAPI lifespan 中启动后台 asyncio 任务。"""
    global _stop_event, _background_task
    if not refresh_enabled():
        logger.info("用户画像后台任务未启动：USER_PROFILE_REFRESH_ENABLED 已关闭")
        return
    if _background_task is not None and not _background_task.done():
        return
    _stop_event = asyncio.Event()
    _background_task = asyncio.create_task(
        _background_loop(), name="user_profile_consolidator"
    )
    logger.info(
        "用户画像后台任务已启动 poll_interval=%ss source_window=%ss min_gap=%ss",
        _poll_interval_sec(),
        _source_window_sec(),
        _min_gap_sec(),
    )


async def stop_user_profile_consolidator() -> None:
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
    logger.info("用户画像后台任务已停止")
