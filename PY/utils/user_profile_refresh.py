"""登录用户画像刷新：「旧用户画像 + 当前 Redis 瞬时记忆」经 LLM 合并写入 ``user_profile``。

- 每完成 N 轮对话：``router.wschat._append_turn_to_redis_history`` 内按计数触发。
- 用户关页面等导致 ``/ws/chat`` 断开：``router.wschat.chat_websocket`` 的 ``finally`` 中触发（不计入轮次计数）。
- 主对话 system 可拼接画像块（``format_profile_for_chat_system``）。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import ollama

from live2d_db.connection import connection_ctx
from live2d_db.db_config import DbConfig
from live2d_db.entities import UserProfile
from live2d_db import memory_layers as _memory_layers
from live2d_db.package_key_util import normalize_package_key
from live2d_db.repositories import UserProfileRepository

logger = logging.getLogger(__name__)

_INSTANT_BLOCK_MAX = 12000
_USER_TAGS_MAX = 255
_EMOTION_MAX = 30
_PREFS_MAX = 8000
_TROUBLE_MAX = 8000


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


def disconnect_refresh_enabled() -> bool:
    """关页 / WebSocket 正常断开时是否再跑一轮画像总结。"""
    return os.getenv("USER_PROFILE_REFRESH_ON_DISCONNECT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def every_n_turns() -> int:
    try:
        n = int((os.getenv("USER_PROFILE_REFRESH_EVERY_N_TURNS") or "5").strip() or "5")
    except ValueError:
        return 5
    return max(1, min(100, n))


def counter_ttl_seconds() -> int:
    try:
        n = int((os.getenv("USER_PROFILE_REFRESH_COUNTER_TTL_SECONDS") or "604800").strip() or "604800")
    except ValueError:
        return 604800
    return max(3600, min(86400 * 30, n))


def instant_block_max_chars() -> int:
    try:
        n = int((os.getenv("USER_PROFILE_REFRESH_INSTANT_MAX_CHARS") or "12000").strip() or "12000")
    except ValueError:
        return _INSTANT_BLOCK_MAX
    return max(2000, min(50000, n))


def redis_profile_turn_counter_key(user_id: int, package_key: str) -> str:
    p = (os.getenv("REDIS_PROFILE_TURN_PREFIX") or "profile_turn").strip() or "profile_turn"
    pkg = normalize_package_key(package_key, fallback="default")
    return f"{p}:{user_id}:{pkg}"


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


def format_instant_memory_block(
    redis_cli: Any, user_id: int, package_key: str
) -> str:
    turns = _memory_layers.read_instant_turns_chronological(
        redis_cli, user_id, package_key
    )
    if not turns:
        return ""
    lines: list[str] = []
    for i, t in enumerate(turns, start=1):
        u = (t.get("u") or "").strip()
        a = (t.get("a") or "").strip()
        if u:
            lines.append(f"第{i}轮 用户：{u}")
        if a:
            lines.append(f"第{i}轮 助手：{a}")
    blob = "\n".join(lines).strip()
    return _truncate(blob, instant_block_max_chars())


def format_stored_profile_block(p: Optional[UserProfile]) -> str:
    """供 LLM 阅读的「旧用户画像」正文。"""
    if p is None:
        return "（尚无已存画像，请仅依据瞬时记忆归纳；字段仍须填满合理默认值如「暂不明确」「无」等简短用语）"
    parts: list[str] = []
    tags = (p.user_tags or "").strip()
    emo = (p.emotion_state or "").strip()
    pref = (p.preferences or "").strip()
    trouble = (p.trouble_events or "").strip()
    parts.append(f"用户标签：{tags or '（空）'}")
    parts.append(f"情感状态：{emo or '（空）'}")
    parts.append(f"偏好与习惯：{pref or '（空）'}")
    parts.append(f"困扰与压力事件：{trouble or '（空）'}")
    return "\n".join(parts)


def format_profile_for_chat_system(p: Optional[UserProfile]) -> str:
    """拼入主对话 system 的画像块；若不存在或非空字段都没有则返回空串。"""
    if p is None:
        return ""
    tags = (p.user_tags or "").strip()
    emo = (p.emotion_state or "").strip()
    pref = (p.preferences or "").strip()
    trouble = (p.trouble_events or "").strip()
    if not any((tags, emo, pref, trouble)):
        return ""
    lines = ["【用户人设】"]
    if tags:
        lines.append(f"- 标签：{tags}")
    if emo:
        lines.append(f"- 当前情感概况：{emo}")
    if pref:
        lines.append(f"- 偏好与习惯：{pref}")
    if trouble:
        lines.append(f"- 困扰与压力：{trouble}")
    return "\n".join(lines)


def _call_profile_llm(old_block: str, instant_block: str) -> dict[str, Any] | None:
    system = (
        "你是用户画像维护模块。输入包含【旧用户画像】与【瞬时记忆】（近期最多若干轮对话原文）。\n"
        "任务：综合二者输出**更新后的完整画像**，继承旧画像中仍成立的信息，用瞬时记忆中的新事实修正或补充；"
        "不要编造对话未出现的具体姓名、日期、成绩等。\n\n"
        "输出严格为一个 JSON 对象，键名必须为：\n"
        "- user_tags：字符串，若干短标签用中文顿号或逗号分隔，总长度不超过 240 字；\n"
        "- emotion_state：字符串，概括用户当前情绪基调，不超过 28 字；\n"
        "- preferences：字符串，用户偏好、兴趣、沟通习惯等；\n"
        "- trouble_events：字符串，困扰、压力源或诉求；若无则写「无明显困扰」或「暂不明确」。\n\n"
        "不要输出 JSON 以外的任何文字。"
    )
    user_content = (
        "【旧用户画像】\n"
        + old_block
        + "\n\n【瞬时记忆】\n"
        + (instant_block or "（空）")
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
    tags = _truncate(str(obj.get("user_tags") or "").strip(), _USER_TAGS_MAX)
    emo = _truncate(str(obj.get("emotion_state") or "").strip(), _EMOTION_MAX)
    pref = _truncate(str(obj.get("preferences") or "").strip(), _PREFS_MAX)
    trouble = _truncate(str(obj.get("trouble_events") or "").strip(), _TROUBLE_MAX)
    if not any((tags, emo, pref, trouble)):
        return None
    return UserProfile(
        user_id=0,
        user_tags=tags or None,
        emotion_state=emo or None,
        preferences=pref or None,
        trouble_events=trouble or None,
    )


def _run_refresh(redis_cli: Any, user_id: int, package_key: str) -> None:
    instant = format_instant_memory_block(redis_cli, user_id, package_key)
    if not instant.strip():
        logger.info(
            "用户画像刷新跳过：瞬时记忆为空 user_id=%s package=%s",
            user_id,
            package_key,
        )
        return
    try:
        with connection_ctx(DbConfig.from_env()) as conn:
            old = UserProfileRepository.get_by_user_id(conn, user_id)
            old_block = format_stored_profile_block(old)
            obj = _call_profile_llm(old_block, instant)
            if not obj:
                logger.info(
                    "用户画像刷新跳过：LLM 返回不可解析 user_id=%s model=%s",
                    user_id,
                    model_name(),
                )
                return
            merged = _coerce_profile_fields(obj)
            if merged is None:
                logger.info("用户画像刷新跳过：字段全空 user_id=%s", user_id)
                return
            merged.user_id = user_id
            UserProfileRepository.upsert_by_user_id(conn, merged)
        logger.info(
            "用户画像已刷新 user_id=%s package=%s model=%s",
            user_id,
            package_key,
            model_name(),
        )
    except Exception:
        logger.exception(
            "用户画像刷新写库失败 user_id=%s package=%s",
            user_id,
            package_key,
        )


def maybe_refresh_user_profile_after_turn(
    redis_cli: Any, user_id: int, package_key: str
) -> None:
    """在已成功写入本轮瞬时记忆后调用。"""
    if user_id < 1:
        return
    if not refresh_enabled():
        return
    n = every_n_turns()
    key = redis_profile_turn_counter_key(user_id, package_key)
    try:
        c = int(redis_cli.incr(key))
        ttl = counter_ttl_seconds()
        if ttl > 0:
            redis_cli.expire(key, ttl)
    except Exception:
        logger.exception(
            "用户画像轮次计数失败 key=%s user_id=%s",
            key,
            user_id,
        )
        return
    if c % n != 0:
        return
    _run_refresh(redis_cli, user_id, package_key)


def refresh_user_profile_on_disconnect(
    redis_cli: Any, user_id: int, package_key: str
) -> None:
    """``/ws/chat`` 连接结束时调用：与按轮刷新共用同一套 LLM 合并逻辑，不修改轮次计数。"""
    if user_id < 1:
        return
    if not refresh_enabled() or not disconnect_refresh_enabled():
        return
    _run_refresh(redis_cli, user_id, package_key)
