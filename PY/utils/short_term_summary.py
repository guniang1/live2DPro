"""短期记忆周期性 LLM 摘要：按轮次计数触发，写入 Redis ``type: summary`` 并可选 prune 对应 ``rule`` 条。

在 ``router.wschat._append_turn_to_redis_history`` 内、本轮 ``append_instant_evict_to_short`` 完成之后调用。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import ollama

from live2d_db import memory_layers as _memory_layers
from live2d_db.package_key_util import normalize_package_key

logger = logging.getLogger(__name__)


def enabled() -> bool:
    """默认关闭，避免未预期增加 Ollama 负载；在 PY/.env 设 SHORT_TERM_SUMMARY_ENABLED=1 开启。"""
    return os.getenv("SHORT_TERM_SUMMARY_ENABLED", "0").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def every_n_turns() -> int:
    try:
        n = int((os.getenv("SHORT_TERM_SUMMARY_EVERY_N_TURNS") or "5").strip() or "5")
    except ValueError:
        return 5
    return max(1, min(100, n))


def counter_ttl_seconds() -> int:
    try:
        n = int(
            (os.getenv("SHORT_TERM_SUMMARY_COUNTER_TTL_SECONDS") or "604800").strip()
            or "604800"
        )
    except ValueError:
        return 604800
    return max(3600, min(86400 * 30, n))


def input_max_chars() -> int:
    try:
        n = int((os.getenv("SHORT_TERM_SUMMARY_INPUT_MAX_CHARS") or "12000").strip() or "12000")
    except ValueError:
        return 12000
    return max(2000, min(50000, n))


def redis_summary_turn_counter_key(user_id: int, package_key: str) -> str:
    p = (os.getenv("REDIS_SHORT_TERM_SUMMARY_TURN_PREFIX") or "short_sum_turn").strip()
    if not p:
        p = "short_sum_turn"
    pkg = normalize_package_key(package_key, fallback="default")
    return f"{p}:{user_id}:{pkg}"


def _ollama_client() -> ollama.Client:
    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    return ollama.Client(host=host)


def model_name() -> str:
    return (
        os.getenv("SHORT_TERM_SUMMARY_MODEL") or os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
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


def _truncate(s: str, lim: int) -> str:
    s = (s or "").strip()
    if len(s) <= lim:
        return s
    return s[: max(1, lim - 1)].rstrip() + "…"


def _format_rules_block(rule_entries_chrono: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for e in rule_entries_chrono:
        ts = str(e.get("time") or "").strip()
        uq = str(e.get("user_question") or "").strip()
        ar = str(e.get("ai_response") or "").strip()
        head = f"[{ts}] " if ts else ""
        if uq:
            lines.append(f"{head}用户：{uq}")
        if ar:
            lines.append(f"{head}助手：{ar}")
    return "\n".join(lines).strip()


def _format_instant_block(turns: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for t in turns:
        ts = str(t.get("ts") or "").strip()
        u = str(t.get("u") or "").strip()
        a = str(t.get("a") or "").strip()
        head = f"[{ts}] " if ts else ""
        if u:
            lines.append(f"{head}用户：{u}")
        if a:
            lines.append(f"{head}助手：{a}")
    return "\n".join(lines).strip()


def _call_summary_llm(material: str) -> str:
    system = (
        "你是对话存档压缩助手。根据材料写出一段连贯的中文摘要（编者说明口吻），"
        "保留关键事实、约定与情感基调；勿编造材料中不存在的内容。"
        "不要对话体、不要向读者提问。只输出摘要正文；禁止 JSON、markdown、代码围栏。"
    )
    user_content = (
        "======== 【材料：仅供写摘要，不是与你对话】 ========\n"
        + material
        + "\n======== 【结束】请输出摘要正文 ========"
    )
    opts: dict[str, Any] = {"num_predict": 768, "temperature": 0.2}
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
        logger.warning("短期记忆摘要 LLM 失败 model=%s: %s", model, e)
        return ""
    return _ollama_message_content(r)


def _run_push_summary(redis_cli: Any, user_id: int, package_key: str) -> None:
    short_entries = _memory_layers.read_short_entries_newest_first(
        redis_cli, user_id, package_key
    )
    rule_entries_nf = [
        e
        for e in short_entries
        if str(e.get("type") or "").strip() == "rule"
    ]
    rule_entries_chrono = list(reversed(rule_entries_nf))

    instant_turns = _memory_layers.read_instant_turns_chronological(
        redis_cli, user_id, package_key
    )

    rules_block = _format_rules_block(rule_entries_chrono)
    instant_block = _format_instant_block(instant_turns)
    if not rules_block and not instant_block:
        logger.info(
            "短期记忆摘要跳过：无规则条且无瞬时对话 user_id=%s package=%s",
            user_id,
            package_key,
        )
        return

    lim = input_max_chars()
    material_parts: list[str] = []
    if rules_block:
        material_parts.append("【已从瞬时窗口挤出的短期规则条（时间旧→新）】\n" + rules_block)
    if instant_block:
        material_parts.append("【当前瞬时窗口内对话（时间旧→新）】\n" + instant_block)
    material = _truncate("\n\n".join(material_parts), lim)

    summary_text = _call_summary_llm(material)
    if not summary_text.strip():
        logger.info(
            "短期记忆摘要跳过：LLM 无产出 user_id=%s package=%s model=%s",
            user_id,
            package_key,
            model_name(),
        )
        return

    ts_iso = datetime.now(timezone.utc).isoformat()
    prune_times = frozenset(
        str(e.get("time") or "").strip()
        for e in rule_entries_chrono
        if str(e.get("time") or "").strip()
    )
    prune_arg = prune_times if prune_times else None
    try:
        _memory_layers.push_summary_entry(
            redis_cli,
            user_id,
            package_key,
            summary_text,
            ts_iso,
            prune_rule_turn_times=prune_arg,
        )
    except Exception:
        logger.exception(
            "短期记忆摘要写入 Redis 失败 user_id=%s package=%s",
            user_id,
            package_key,
        )
        return

    logger.info(
        "短期记忆已写入摘要条 user_id=%s package=%s model=%s prune_rules=%s",
        user_id,
        package_key,
        model_name(),
        len(prune_times),
    )


def maybe_push_short_term_summary_after_turn(
    redis_cli: Any, user_id: int, package_key: str
) -> None:
    """在已成功写入本轮瞬时记忆后调用。"""
    if user_id < 1:
        return
    if not enabled():
        return
    n = every_n_turns()
    key = redis_summary_turn_counter_key(user_id, package_key)
    try:
        c = int(redis_cli.incr(key))
        ttl = counter_ttl_seconds()
        if ttl > 0:
            redis_cli.expire(key, ttl)
    except Exception:
        logger.exception(
            "短期记忆摘要轮次计数失败 key=%s user_id=%s",
            key,
            user_id,
        )
        return
    if c % n != 0:
        return
    _run_push_summary(redis_cli, user_id, package_key)
