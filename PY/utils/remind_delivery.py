"""定时关怀投递时生成对用户说的话术。

库表 ``remind_trigger.trigger_content`` 存**情景详细描述**；投递时由 LLM **重新撰写**面向用户的台词。
写入 LLM **User** 消息的拼装顺序（与论文 6.5.1 一致）：

1. 人设块（可选）：从 MySQL 表 ``persona`` 经 ``PersonaRepository.resolve_persona_for_package`` 读取当前包的 ``tone_style`` / ``character_desc``；
2. 用户画像块（可选）：``USER_PROFILE_IN_CHAT`` 未关闭且画像存在非空字段时，``format_profile_for_chat_system`` → ``【用户画像】``；
3. 当前对话瞬时记忆（可选）：与 ``/ws/chat`` 相同 ``(user_id, package_key)`` 分桶，Redis List 按时间顺序的多轮 user/assistant 原文，**不含**短期压缩层；
4. **关怀类型**：``trigger_type``；
5. **情景详细描述**：库内 ``trigger_content``（无则正文写 ``未提供``）；
6. **关联对话原文**：``session_id`` → ``chat_session.user_input`` / ``ai_reply``；无有效 ``session_id`` 或无法读取则正文写 ``未绑定``。

系统提示要求当场口语、以情景与瞬时对话为主轴，可呼应关联单轮与人设/画像；输出 1～3 句中文；禁止 JSON/Markdown；约 800 字上限（代码侧超长截断加 ``…``）。
模型 ``REMIND_DELIVERY_MODEL``（缺省同 ``OLLAMA_MODEL``）；可选 ``REMIND_DELIVERY_TEMPERATURE``、``REMIND_DELIVERY_NUM_PREDICT``。
``delivery_message`` 为生成稿；``trigger_content`` 与 REST 一致为库内情景原文。
"""

from __future__ import annotations

import logging
import os
import re

import ollama
import pymysql.err

from live2d_db.connection import connection_ctx
from live2d_db.db_config import DbConfig
from live2d_db.entities import RemindTrigger
from live2d_db.memory_layers import read_instant_turns_chronological
from live2d_db.redis_factory import get_redis_client
from live2d_db.repositories import (
    ChatSessionRepository,
    PersonaRepository,
    UserProfileRepository,
)
from utils.user_profile_refresh import chat_inject_enabled, format_profile_for_chat_system

logger = logging.getLogger(__name__)

_MAX_OUT_CHARS = 800
_MAX_SESSION_TURN_CHARS = 12_000
_MAX_INSTANT_BLOCK = 4500
_PERSONA_BLOCK_MAX = 3500

# 去掉模型复读 trigger_content 中带相对时间的悖论措辞（如「1分钟前祝你…」）
_TIME_PARADOX_RE = re.compile(
    r"[0-9０-９一两三四五六七八九十百]+\s*(?:分钟|小时|秒钟?)(?:前|后)[^，。！？\n]{0,30}",
    re.UNICODE,
)


def _delivery_model() -> str:
    return (
        os.getenv("REMIND_DELIVERY_MODEL")
        or os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    ).strip()


def _delivery_temperature() -> float:
    raw = (os.getenv("REMIND_DELIVERY_TEMPERATURE") or "").strip()
    if not raw:
        return 0.55
    try:
        return max(0.0, min(2.0, float(raw)))
    except ValueError:
        return 0.55


def _delivery_num_predict() -> int:
    raw = (os.getenv("REMIND_DELIVERY_NUM_PREDICT") or "").strip()
    if not raw:
        return 512
    try:
        return max(64, min(2048, int(raw)))
    except ValueError:
        return 512


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


def _persona_mysql_block(user_id: int, package_key: str) -> str:
    """从 MySQL ``persona`` 表解析用户与模型包的人设（语气 + 角色设定），与 ``/ws/chat`` 包级人设同源。"""
    if user_id < 1:
        return ""
    pkg = (package_key or "").strip()
    if not pkg:
        return ""
    try:
        with connection_ctx(DbConfig.from_env()) as conn:
            row = PersonaRepository.resolve_persona_for_package(conn, user_id, pkg)
        if row is None:
            return ""
        tone = (row.tone_style or "").strip()
        desc = (row.character_desc or "").strip()
        if not tone and not desc:
            return ""
        parts: list[str] = []
        if tone:
            parts.append(f"【语气风格】\n{tone}")
        if desc:
            parts.append(f"【角色设定】\n{desc}")
        blob = "\n\n".join(parts).strip()
        if len(blob) <= _PERSONA_BLOCK_MAX:
            return blob
        return blob[: max(1, _PERSONA_BLOCK_MAX - 1)].rstrip() + "…"
    except pymysql.err.ProgrammingError as e:
        code = e.args[0] if e.args else None
        if code in (1146, 1054):
            logger.warning(
                "关怀话术读取人设跳过（persona 表或列不可用 errno=%s）",
                code,
            )
            return ""
        logger.exception(
            "关怀话术读取 MySQL persona 失败 user_id=%s package=%s",
            user_id,
            pkg,
        )
        return ""
    except Exception:
        logger.exception(
            "关怀话术读取 MySQL persona 失败 user_id=%s package=%s",
            user_id,
            pkg,
        )
        return ""


def _redis_instant_memory_prompt_block(user_id: int, package_key: str) -> str:
    """与主对话相同分桶的 Redis 瞬时 List（当前对话多轮原文），不含短期压缩层。"""
    if user_id < 1:
        return ""
    cli = get_redis_client(logger)
    if cli is None:
        return ""
    try:
        turns = read_instant_turns_chronological(cli, user_id, package_key)
    except Exception:
        logger.exception(
            "关怀话术：读取瞬时记忆失败 user_id=%s package=%s",
            user_id,
            package_key,
        )
        turns = []
    if not turns:
        return ""
    instant_lines: list[str] = []
    for turn in turns:
        u = (turn.get("u") or "").strip()
        a = (turn.get("a") or "").strip()
        if u:
            instant_lines.append(f"用户：{u}")
        if a:
            instant_lines.append(f"助手：{a}")
    block = "\n".join(instant_lines).strip()
    if len(block) > _MAX_INSTANT_BLOCK:
        block = block[: _MAX_INSTANT_BLOCK - 1].rstrip() + "…"
    if not block:
        return ""
    return f"【当前对话瞬时记忆】\n{block}"


def _session_dialogue_prompt_block(t: RemindTrigger) -> str:
    """``session_id`` → 该轮 ``chat_session`` 原文（用户 + 助手），作为触发时语境。"""
    sid = t.session_id
    if sid is None:
        return ""
    try:
        if int(sid) < 1:
            return ""
    except (TypeError, ValueError):
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


def _post_process_delivery(s: str) -> str:
    """去掉模型输出中含时间悖论的短语（如「1分钟前祝你…」）。"""
    cleaned = _TIME_PARADOX_RE.sub("", (s or "").strip()).strip()
    cleaned = re.sub(r"[，、]\s*[。！？]", "。", cleaned)
    cleaned = re.sub(r"[。！？]{2,}", "。", cleaned)
    return cleaned.strip()


def _strip_trigger_type_brackets(s: str, trigger_type: str | None) -> str:
    """去掉对用户可见文案中的「【日常关怀】」等与 trigger_type 一致或模型残留的【…】栏目标签。"""
    out = (s or "").strip()
    tt = (trigger_type or "").strip()
    if tt:
        out = re.sub(re.escape(f"【{tt}】") + r"[：:\s\u3000]*", "", out).strip()
    while True:
        m = re.match(r"^【[^】]{1,32}】[：:\s\u3000]*", out)
        if not m:
            break
        out = out[m.end() :].strip()
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def _fallback_line(t: RemindTrigger) -> str:
    if not (t.trigger_type or "").strip():
        return ""
    s = (t.trigger_content or "").strip()
    if s:
        s = (s[:240] + "…") if len(s) > 240 else s
    else:
        s = "今天过得怎么样？过来陪你聊一会儿好吗？"
    return _strip_trigger_type_brackets(s, t.trigger_type)


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
    s = s.strip()
    # 去掉模型偶发的栏目前缀（如「【日常关怀】」「【日常关怀】：…」；兼容全角冒号与空白）
    while True:
        m = re.match(r"^【[^】]{1,32}】[：:\s\u3000]*", s)
        if not m:
            break
        s = s[m.end() :].strip()
    logger.debug("关怀话术剥除后: %r", s[:200])
    return s


def delivery_use_llm() -> bool:
    return os.getenv("REMIND_DELIVERY_USE_LLM", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def generate_remind_delivery_message(t: RemindTrigger, package_key: str) -> str:
    """结合情景描述、关联单轮、Redis 瞬时多轮对话与人设/画像，由 LLM 重新生成对用户展示的话术。"""
    if not (t.trigger_type or "").strip():
        return ""
    if not delivery_use_llm():
        return _fallback_line(t)

    scenario = (t.trigger_content or "").strip()
    session_text = _session_dialogue_prompt_block(t)
    pkg = (package_key or "").strip() or "default"
    memory_block = _redis_instant_memory_prompt_block(int(t.user_id), pkg)

    try:
        persona = _persona_mysql_block(int(t.user_id), pkg)
    except Exception:
        logger.exception(
            "关怀话术：读取人设块失败 trigger_id=%s user_id=%s",
            t.trigger_id,
            t.user_id,
        )
        persona = ""

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

    if not scenario and not session_text and not memory_block:
        return _fallback_line(t)

    system = (
        "【绝对禁止】不许说「X分钟后我会……」「稍后我将……」「我等会儿……」；"
        "不许在输出里出现「【日常关怀】」「【生日】」等类型标签。\n"
        "此刻就是触发当下，直接对用户表达关心，就像朋友突然想起来搭一句话。\n\n"
        "你是 Live2D 数字人。此刻请**重新撰写**你对用户**当场说出的一段台词**（就像真人刚好想起来搭一句话），"
        "不是在播报定时任务、也不是客服工单。\n\n"
        "输入材料按顺序可能包含：【当前模型人设】（来自服务端 MySQL 人设表 persona，仅供语气与角色一致性）；【用户画像】（内部摘要，仅用于把握语气与关切点，勿背诵原文，"
        "勿对用户提「画像」「系统」「后台」）；【当前对话瞬时记忆】（与主对话同包分桶的 Redis 最近多轮原文，不含短期压缩摘要；"
        "可能比提醒创建时更新；若无则该段不出现）；【关怀类型】；【情景详细描述】（入库备忘，常含相对时间或草稿措辞，"
        "**不是**要你逐字念给用户听的台词）；【关联对话原文】（生成提醒那一轮的 user/assistant，若无有效绑定则正文为「未绑定」）。\n\n"
        "**主轴**：以「情景详细描述」与「当前对话瞬时记忆」为主组织语气与话题；可自然呼应「关联对话原文」以及人设、画像中的关切点。\n"
        "**输出**：连续 1～3 句口语化中文，温暖自然；不要编造材料里不存在的事实；禁止 JSON、Markdown、项目符号列表。\n"
        "**长度**：约 800 汉字以内；若自觉会超长则自行收束（服务端也会对过长输出截断）。\n\n"
        "勿嵌套复述情景里的整句草稿；勿照搬情景中带「N 分钟前/后」等易造成时间悖论的措辞；"
        "勿输出「定时关怀」「预约提醒」等机器话术。"
    )
    parts: list[str] = []
    if persona:
        parts.append(persona)
    if profile_block:
        parts.append(profile_block)
    if memory_block:
        parts.append(memory_block)
    parts.append(f"【关怀类型】{t.trigger_type}")
    parts.append(f"【情景详细描述】\n{scenario or '未提供'}")
    parts.append(f"【关联对话原文】\n{session_text or '未绑定'}")
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
            options={
                "temperature": _delivery_temperature(),
                "num_predict": _delivery_num_predict(),
            },
        )
    except Exception:
        logger.exception(
            "关怀话术生成 Ollama 调用失败 trigger_id=%s model=%s",
            t.trigger_id,
            model,
        )
        return _fallback_line(t)

    line = _strip_model_output(_ollama_message_content(r))
    line = _post_process_delivery(line)
    line = _strip_trigger_type_brackets(line, t.trigger_type)
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
