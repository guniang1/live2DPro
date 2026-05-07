"""定时关怀投递时生成对用户说的话术。

库表 ``remind_trigger.trigger_content`` 存**情景详细描述**；投递时按 ``session_id`` 读取 ``chat_session``
单轮原文，与情景一并交给 LLM 当场生成最终台词。
"""

from __future__ import annotations

import logging
import os
import re

import ollama

from live2d_db.connection import connection_ctx
from live2d_db.db_config import DbConfig
from live2d_db.entities import RemindTrigger
from live2d_db.repositories import ChatSessionRepository, UserProfileRepository
from utils.user_profile_refresh import chat_inject_enabled, format_profile_for_chat_system

logger = logging.getLogger(__name__)

_MAX_OUT_CHARS = 800
_MAX_SESSION_TURN_CHARS = 12_000


def _delivery_model() -> str:
    return (
        os.getenv("REMIND_DELIVERY_MODEL")
        or os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    ).strip()


def _ollama_client() -> ollama.Client:
    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    return ollama.Client(host=host)


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


def _session_dialogue_prompt_block(t: RemindTrigger) -> str:
    """``session_id`` → 该轮 ``chat_session`` 原文（用户 + 助手），作为触发时语境。"""
    sid = t.session_id
    if sid is None:
        return ""
    try:
        with connection_ctx(DbConfig.from_env()) as conn:
            cs = ChatSessionRepository.get_by_id(conn, int(sid))
        if cs is None:
            logger.warning(
                "关怀话术：trigger_id=%s session_id=%s 无对应 chat_session 行",
                t.trigger_id,
                sid,
            )
            return ""
        if int(cs.user_id) != int(t.user_id):
            logger.warning(
                "关怀话术：trigger_id=%s session_id=%s 与 user_id=%s 不匹配，跳过对话块",
                t.trigger_id,
                sid,
                t.user_id,
            )
            return ""
        u = (cs.user_input or "").strip()
        a = (cs.ai_reply or "").strip()
        if not u and not a:
            return ""
        block = f"用户：{u}\n助手：{a}".strip()
        if len(block) > _MAX_SESSION_TURN_CHARS:
            block = block[: _MAX_SESSION_TURN_CHARS - 1].rstrip() + "…"
        return block
    except Exception:
        logger.exception(
            "关怀话术：读取 chat_session 失败 trigger_id=%s session_id=%s",
            t.trigger_id,
            sid,
        )
        return ""


def _fallback_line(t: RemindTrigger) -> str:
    s = (t.trigger_content or "").strip()
    if s:
        return (s[:240] + "…") if len(s) > 240 else s
    return "今天过得怎么样？过来陪你聊一会儿好吗？"


def _strip_model_output(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("```"):
        lines = s.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    s = re.sub(r"^[\"'「](.+)[\"'」]$", r"\1", s.strip(), flags=re.DOTALL)
    return s.strip()


def delivery_use_llm() -> bool:
    return os.getenv("REMIND_DELIVERY_USE_LLM", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def generate_remind_delivery_message(t: RemindTrigger, package_key: str) -> str:
    """结合 ``session_id`` 对应单轮对话与情景描述，生成对用户展示的话术。"""
    if not delivery_use_llm():
        return _fallback_line(t)

    scenario = (t.trigger_content or "").strip()
    session_text = _session_dialogue_prompt_block(t)
    pkg = (package_key or "").strip() or "default"

    persona = ""
    try:
        from utils.remind_extract import _persona_block_for_package

        persona = _persona_block_for_package(t.user_id, pkg)
    except Exception:
        logger.exception(
            "关怀话术：读取人设块失败 trigger_id=%s user_id=%s",
            t.trigger_id,
            t.user_id,
        )

    profile_block = ""
    uid = int(t.user_id)
    if uid >= 1 and chat_inject_enabled():
        try:
            with connection_ctx(DbConfig.from_env()) as conn:
                prof = UserProfileRepository.get_by_user_id(conn, uid)
            profile_block = format_profile_for_chat_system(prof)
        except Exception:
            logger.exception(
                "关怀话术：读取用户画像失败 trigger_id=%s user_id=%s",
                t.trigger_id,
                t.user_id,
            )

    if not scenario and not session_text:
        return _fallback_line(t)

    system = (
        "你是 Live2D 数字人，正在对用户执行一条「预约定时关怀」。\n"
        "输入可能依次包含：【当前模型人设】；【用户画像】——系统侧对用户标签、情感基调、偏好与困扰等的摘要，"
        "用于你了解对方并写出更贴切的问候（勿逐条背诵画像原文，也不要向用户暴露「画像」「系统」等字眼）；"
        "【关怀类型】；【情景详细描述】（创建提醒时写入库的客观记录，不是最终台词）；"
        "【关联对话原文】（来自 remind_trigger.session_id 对应的单轮 chat_session）。\n"
        "请生成你对用户**当场说出**的一段话：1～3 句中文，口语化、自然温暖；以情景与关联对话为主，"
        "可适度呼应画像中的偏好与情绪基调；**不要编造对话与情景中均不存在的事实**。\n"
        "禁止输出 JSON、列表、Markdown；不要复述栏目名。"
    )
    parts: list[str] = []
    if persona:
        parts.append(persona)
    if profile_block:
        parts.append(profile_block)
    parts.append(f"【关怀类型】{t.trigger_type}")
    parts.append(f"【情景详细描述】\n{scenario or '（未提供）'}")
    parts.append(f"【关联对话原文】\n{session_text or '（未绑定）'}")
    user_content = "\n\n".join(parts)

    cli = _ollama_client()
    model = _delivery_model()
    try:
        r = cli.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            options={"temperature": 0.45, "num_predict": 512},
        )
    except Exception:
        logger.exception(
            "关怀话术生成 Ollama 调用失败 trigger_id=%s model=%s",
            t.trigger_id,
            model,
        )
        return _fallback_line(t)

    line = _strip_model_output(_ollama_message_content(r))
    if not line:
        return _fallback_line(t)
    if len(line) > _MAX_OUT_CHARS:
        line = line[: _MAX_OUT_CHARS - 1].rstrip() + "…"
    logger.info(
        "关怀话术已生成 trigger_id=%s user_id=%s chars=%s model=%s",
        t.trigger_id,
        t.user_id,
        len(line),
        model,
    )
    return line
