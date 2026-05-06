"""瞬时记忆（Redis List）、短期记忆（规则精简 + LLM 摘要条）与长期摘要缓存（Redis STRING）共享逻辑。

键均按 user_id + package_key 分桶；供 wschat 与 http_api 登录预热共用。
另有 MiMo 导演用人设字段缓存（``mimo:director:persona:*``），见 ``get_mimo_director_persona_cached``。

设计对照 MemGPT 的思路（单次请求字数有限，列表与表字段承担较长留存）：本模块实现 Redis List/String 与 TTL、条数上限等；
何时拼进 Prompt、何时写 Redis、长期文本何时从 MySQL 回填，由 ``router.wschat`` 等编排层触发，
不由模型自行决定分页。周期概要写入 MySQL 见 ``live2d_db.long_memory_consolidator``。
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from live2d_db.entities import ChatSession
from live2d_db.package_key_util import normalize_package_key

logger = logging.getLogger(__name__)


def _pkg_norm(package_key: str) -> str:
    return normalize_package_key(package_key, fallback="default")


def redis_instant_list_key(user_id: int, package_key: str) -> str:
    p = (os.getenv("REDIS_INSTANT_LIST_PREFIX") or "moment").strip() or "moment"
    return f"{p}:{user_id}:{_pkg_norm(package_key)}"


def redis_short_term_list_key(user_id: int, package_key: str) -> str:
    p = (os.getenv("REDIS_SHORT_TERM_PREFIX") or "short").strip() or "short"
    return f"{p}:{user_id}:{_pkg_norm(package_key)}"


def redis_long_memory_key(user_id: int, package_key: str) -> str:
    p = (os.getenv("REDIS_LONG_MEMORY_PREFIX") or "long").strip() or "long"
    return f"{p}:{user_id}:{_pkg_norm(package_key)}"


def long_memory_ttl_seconds() -> int:
    try:
        n = int((os.getenv("LONG_MEMORY_TTL_SECONDS") or "604800").strip() or "604800")
    except ValueError:
        return 604800
    return max(300, min(86400 * 30, n))


def long_memory_prompt_max_chars() -> int:
    try:
        n = int((os.getenv("LONG_MEMORY_PROMPT_MAX_CHARS") or "2000").strip() or "2000")
    except ValueError:
        return 2000
    return max(200, min(20000, n))


def instant_memory_max_turns() -> int:
    try:
        n = int((os.getenv("INSTANT_MEMORY_MAX_TURNS") or "5").strip() or "5")
    except ValueError:
        return 5
    return max(1, min(50, n))


def instant_memory_idle_ttl_seconds() -> int:
    try:
        n = int((os.getenv("INSTANT_MEMORY_IDLE_TTL_SECONDS") or "3600").strip() or "3600")
    except ValueError:
        return 3600
    return max(60, min(86400 * 7, n))


def short_term_ttl_seconds() -> int:
    try:
        n = int((os.getenv("SHORT_TERM_TTL_SECONDS") or "86400").strip() or "86400")
    except ValueError:
        return 86400
    return max(300, min(86400 * 14, n))


def short_term_max_entries() -> int:
    try:
        n = int((os.getenv("SHORT_TERM_MAX_ENTRIES") or "20").strip() or "20")
    except ValueError:
        return 20
    return max(5, min(500, n))


def short_term_prompt_max_chars() -> int:
    try:
        n = int((os.getenv("SHORT_TERM_PROMPT_MAX_CHARS") or "4000").strip() or "4000")
    except ValueError:
        return 4000
    return max(500, min(20000, n))


_BRIEF_AI_RE = re.compile(r"^(嗯+|哦+|啊+|好的|好哒|ok|OK|行|好吧|明白了|了解)[。！？!.]*$")


def _truncate(s: str, lim: int) -> str:
    s = (s or "").strip()
    if len(s) <= lim:
        return s
    return s[: max(1, lim - 1)].rstrip() + "…"


def rule_compact_turn(
    user_input: str,
    ai_reply: str,
    ts_iso: str,
    *,
    user_max: int = 600,
    ai_max: int = 600,
) -> dict[str, Any]:
    """方案 1：结构化精简条目（写入短期 List）。"""
    ui = _truncate(user_input, user_max)
    ar_raw = (ai_reply or "").strip()
    ar = _truncate(ar_raw, ai_max)
    if len(ar_raw) < 10 and _BRIEF_AI_RE.match(ar_raw):
        ar = ""
    return {
        "type": "rule",
        "time": ts_iso,
        "user_question": ui,
        "ai_response": ar,
    }


def instant_turn_json(user_input: str, ai_reply: str, ts_iso: str) -> str:
    payload = {
        "u": _truncate(user_input, 65500),
        "a": _truncate(ai_reply, 65500),
        "ts": ts_iso,
    }
    return json.dumps(payload, ensure_ascii=False)


def parse_instant_turn(raw: str) -> dict[str, str] | None:
    try:
        o = json.loads(raw)
    except Exception:
        return None
    if not isinstance(o, dict):
        return None
    return {
        "u": str(o.get("u") or "").strip(),
        "a": str(o.get("a") or "").strip(),
        "ts": str(o.get("ts") or "").strip(),
    }


def parse_short_entry(raw: str) -> dict[str, Any] | None:
    try:
        o = json.loads(raw)
    except Exception:
        return None
    if not isinstance(o, dict):
        return None
    return o


def format_short_entry_line(entry: dict[str, Any]) -> str:
    t = str(entry.get("type") or "").strip()
    if t == "summary":
        txt = str(entry.get("text") or "").strip()
        ts = str(entry.get("time") or "").strip()
        head = f"[摘要 {ts}] " if ts else "[摘要] "
        return head + txt
    uq = str(entry.get("user_question") or "").strip()
    ar = str(entry.get("ai_response") or "").strip()
    ts = str(entry.get("time") or "").strip()
    parts = []
    if ts:
        parts.append(ts)
    if uq:
        parts.append(f"用户：{uq}")
    if ar:
        parts.append(f"助手：{ar}")
    return " | ".join(parts) if parts else ""


def format_short_term_block(entries_newest_first: list[dict[str, Any]]) -> str:
    """将短期 List（从新到旧）格式化为一段 system 正文。"""
    lim = short_term_prompt_max_chars()
    lines: list[str] = []
    used = 0
    for e in entries_newest_first:
        line = format_short_entry_line(e)
        if not line:
            continue
        chunk = line + "\n"
        if used + len(chunk) > lim:
            break
        lines.append(line)
        used += len(chunk)
    return "\n".join(lines).strip()


def append_instant_evict_to_short(
    redis_cli: Any,
    user_id: int,
    package_key: str,
    user_input: str,
    ai_reply: str,
    ts_iso: str,
) -> None:
    """LPUSH 瞬时一轮；超长则将被挤出瞬时窗口的最旧轮规则精简后写入短期 List。"""
    if user_id <= 0:
        return
    ik = redis_instant_list_key(user_id, package_key)
    sk = redis_short_term_list_key(user_id, package_key)
    max_turns = instant_memory_max_turns()
    idle_ttl = instant_memory_idle_ttl_seconds()
    st_ttl = short_term_ttl_seconds()
    st_max = short_term_max_entries()

    turn_js = instant_turn_json(user_input, ai_reply, ts_iso)
    pipe = redis_cli.pipeline(transaction=True)
    pipe.lpush(ik, turn_js)
    pipe.lrange(ik, max_turns, -1)
    pipe.ltrim(ik, 0, max_turns - 1)
    pipe.expire(ik, idle_ttl)
    _, evicted_raw, _, _ = pipe.execute()

    if evicted_raw:
        pipe2 = redis_cli.pipeline(transaction=True)
        for raw in reversed(evicted_raw):
            prev = parse_instant_turn(raw)
            if not prev:
                continue
            ru = rule_compact_turn(prev.get("u") or "", prev.get("a") or "", prev.get("ts") or ts_iso)
            if not ru.get("user_question") and not ru.get("ai_response"):
                continue
            pipe2.lpush(sk, json.dumps(ru, ensure_ascii=False))
        pipe2.ltrim(sk, 0, st_max - 1)
        pipe2.expire(sk, st_ttl)
        pipe2.execute()

    try:
        redis_cli.expire(sk, st_ttl)
    except Exception:
        pass


def push_summary_entry(
    redis_cli: Any,
    user_id: int,
    package_key: str,
    summary_text: str,
    ts_iso: str,
    *,
    prune_rule_turn_times: frozenset[str] | None = None,
) -> None:
    """写入摘要条；若给定 prune_rule_turn_times，则从短期 List 移除 time 命中的 rule（避免与摘要重复堆叠）。"""
    sk = redis_short_term_list_key(user_id, package_key)
    st_max = short_term_max_entries()
    st_ttl = short_term_ttl_seconds()
    entry = {"type": "summary", "text": _truncate(summary_text, 2000), "time": ts_iso}
    summary_json = json.dumps(entry, ensure_ascii=False)

    prune = prune_rule_turn_times and len(prune_rule_turn_times) > 0
    if not prune:
        pipe = redis_cli.pipeline(transaction=True)
        pipe.lpush(sk, summary_json)
        pipe.ltrim(sk, 0, st_max - 1)
        pipe.expire(sk, st_ttl)
        pipe.execute()
        return

    try:
        raw_list = redis_cli.lrange(sk, 0, -1)
    except Exception:
        logger.exception("读取短期记忆失败 key=%s，摘要写入降级为仅 LPUSH", sk)
        pipe = redis_cli.pipeline(transaction=True)
        pipe.lpush(sk, summary_json)
        pipe.ltrim(sk, 0, st_max - 1)
        pipe.expire(sk, st_ttl)
        pipe.execute()
        return

    kept_raw: list[str] = []
    for raw in raw_list:
        e = parse_short_entry(raw)
        if not e:
            kept_raw.append(raw)
            continue
        if str(e.get("type") or "").strip() == "rule":
            tm = str(e.get("time") or "").strip()
            if tm and tm in prune_rule_turn_times:
                continue
        kept_raw.append(raw)

    pipe = redis_cli.pipeline(transaction=True)
    pipe.delete(sk)
    pipe.rpush(sk, summary_json)
    for r in kept_raw:
        pipe.rpush(sk, r)
    pipe.ltrim(sk, 0, st_max - 1)
    pipe.expire(sk, st_ttl)
    pipe.execute()


def read_instant_turns_chronological(redis_cli: Any, user_id: int, package_key: str) -> list[dict[str, str]]:
    ik = redis_instant_list_key(user_id, package_key)
    try:
        raw_list = redis_cli.lrange(ik, 0, -1)
    except Exception:
        logger.exception("读取瞬时记忆失败 key=%s", ik)
        return []
    turns: list[dict[str, str]] = []
    for raw in raw_list:
        p = parse_instant_turn(raw)
        if p and (p.get("u") or p.get("a")):
            turns.append(p)
    turns.reverse()
    return turns


def read_short_entries_newest_first(redis_cli: Any, user_id: int, package_key: str) -> list[dict[str, Any]]:
    sk = redis_short_term_list_key(user_id, package_key)
    try:
        raw_list = redis_cli.lrange(sk, 0, -1)
    except Exception:
        logger.exception("读取短期记忆失败 key=%s", sk)
        return []
    out: list[dict[str, Any]] = []
    for raw in raw_list:
        e = parse_short_entry(raw)
        if e:
            out.append(e)
    return out


def write_long_memory_text(
    redis_cli: Any, user_id: int, package_key: str, text: str
) -> None:
    """将长期记忆合并正文写入 Redis STRING（由 ``merge_long_memory_record_for_prompt`` 生成）。"""
    if redis_cli is None or user_id <= 0:
        return
    key = redis_long_memory_key(user_id, package_key)
    ttl = long_memory_ttl_seconds()
    try:
        redis_cli.set(key, (text or "").strip(), ex=ttl)
    except Exception:
        logger.exception("写入长期记忆 Redis 失败 key=%s", key)


def read_long_memory_text(redis_cli: Any, user_id: int, package_key: str) -> str:
    if redis_cli is None or user_id <= 0:
        return ""
    key = redis_long_memory_key(user_id, package_key)
    try:
        raw = redis_cli.get(key)
    except Exception:
        logger.exception("读取长期记忆 Redis 失败 key=%s", key)
        return ""
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", errors="replace").strip()
    return str(raw).strip()


def format_long_memory_block(text: str) -> str:
    lim = long_memory_prompt_max_chars()
    s = (text or "").strip()
    if len(s) <= lim:
        return s
    return s[: max(1, lim - 1)].rstrip() + "…"


def delete_memory_keys(redis_cli: Any, user_id: int, package_key: str) -> None:
    redis_cli.delete(
        redis_instant_list_key(user_id, package_key),
        redis_short_term_list_key(user_id, package_key),
        redis_long_memory_key(user_id, package_key),
    )


def redis_mimo_director_persona_key(user_id: int, package_key: str) -> str:
    """MiMo 导演中高频块【人设】【语气】所依赖的字段（不含【场景】）。"""
    pfx = (os.getenv("REDIS_MIMO_DIRECTOR_PERSONA_PREFIX") or "mimo:director:persona").strip()
    if not pfx:
        pfx = "mimo:director:persona"
    return f"{pfx}:{user_id}:{_pkg_norm(package_key)}"


def mimo_director_persona_redis_cache_enabled() -> bool:
    v = (os.getenv("REDIS_MIMO_DIRECTOR_PERSONA_CACHE") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def mimo_director_persona_redis_ttl_seconds() -> int:
    try:
        return max(300, min(86400 * 30, int(os.getenv("REDIS_MIMO_DIRECTOR_PERSONA_TTL") or "86400")))
    except ValueError:
        return 86400


def get_mimo_director_persona_cached(
    redis_cli: Any, user_id: int, package_key: str
) -> Optional[Tuple[str, str]]:
    """命中返回 ``(character_desc, tone_style)``；未命中返回 ``None``。"""
    if (
        redis_cli is None
        or user_id <= 0
        or not mimo_director_persona_redis_cache_enabled()
    ):
        return None
    try:
        raw = redis_cli.get(redis_mimo_director_persona_key(user_id, package_key))
        if raw is None:
            return None
        s = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        o = json.loads(s)
        if not isinstance(o, dict):
            return None
        return (
            str(o.get("character_desc") or ""),
            str(o.get("tone_style") or ""),
        )
    except Exception:
        logger.exception(
            "读取 MiMo 导演人设 Redis 失败 user_id=%s pkg=%s",
            user_id,
            package_key,
        )
        return None


def set_mimo_director_persona_cached(
    redis_cli: Any,
    user_id: int,
    package_key: str,
    character_desc: str,
    tone_style: str,
) -> None:
    if (
        redis_cli is None
        or user_id <= 0
        or not mimo_director_persona_redis_cache_enabled()
    ):
        return
    payload = json.dumps(
        {"character_desc": character_desc, "tone_style": tone_style},
        ensure_ascii=False,
    )
    try:
        redis_cli.set(
            redis_mimo_director_persona_key(user_id, package_key),
            payload,
            ex=mimo_director_persona_redis_ttl_seconds(),
        )
    except Exception:
        logger.exception(
            "写入 MiMo 导演人设 Redis 失败 user_id=%s pkg=%s",
            user_id,
            package_key,
        )


def delete_mimo_director_persona_cached(
    redis_cli: Any, user_id: int, package_key: str
) -> None:
    if redis_cli is None or user_id <= 0:
        return
    try:
        redis_cli.delete(redis_mimo_director_persona_key(user_id, package_key))
    except Exception:
        logger.exception(
            "删除 MiMo 导演人设 Redis 失败 user_id=%s pkg=%s",
            user_id,
            package_key,
        )


def replace_instant_turns(
    redis_cli: Any,
    user_id: int,
    package_key: str,
    turns_chronological: list[tuple[str, str, str]],
) -> None:
    """覆盖瞬时 List（时间从旧到新）；每项为 (user, assistant, ts_iso)。"""
    if user_id <= 0:
        return
    ik = redis_instant_list_key(user_id, package_key)
    redis_cli.delete(ik)
    if not turns_chronological:
        return
    idle_ttl = instant_memory_idle_ttl_seconds()
    pipe = redis_cli.pipeline(transaction=True)
    for ui, ar, ts in reversed(turns_chronological):
        pipe.lpush(ik, instant_turn_json(ui, ar, ts))
    pipe.expire(ik, idle_ttl)
    pipe.execute()


def seed_from_mysql_rows(
    redis_cli: Any,
    user_id: int,
    package_key: str,
    rows_chronological: list[ChatSession],
) -> None:
    """登录预热：rows 按时间从旧到新；最近 N 轮写入瞬时 List，更早的写入短期 List。"""
    if user_id <= 0 or not rows_chronological:
        return
    pkg = _pkg_norm(package_key)
    ik = redis_instant_list_key(user_id, pkg)
    sk = redis_short_term_list_key(user_id, pkg)
    max_turns = instant_memory_max_turns()
    idle_ttl = instant_memory_idle_ttl_seconds()
    st_ttl = short_term_ttl_seconds()
    st_max = short_term_max_entries()

    redis_cli.delete(ik, sk)

    def _row_ts(r: ChatSession) -> str:
        ct = r.create_time
        if ct is not None:
            try:
                if hasattr(ct, "isoformat"):
                    return ct.isoformat()
            except Exception:
                pass
        return datetime.now(timezone.utc).isoformat()

    turns: list[tuple[str, str, str]] = []
    for r in rows_chronological:
        ui = (r.user_input or "").strip()
        ar = (r.ai_reply or "").strip()
        if not ui and not ar:
            continue
        turns.append((ui, ar, _row_ts(r)))

    if not turns:
        return

    instant_slice = turns[-max_turns:] if len(turns) > max_turns else turns
    short_slice = turns[: -max_turns] if len(turns) > max_turns else []

    pipe = redis_cli.pipeline(transaction=True)
    for ui, ar, ts in reversed(instant_slice):
        pipe.lpush(ik, instant_turn_json(ui, ar, ts))
    pipe.expire(ik, idle_ttl)

    for ui, ar, ts in reversed(short_slice):
        ru = rule_compact_turn(ui, ar, ts)
        if ru.get("user_question") or ru.get("ai_response"):
            pipe.lpush(sk, json.dumps(ru, ensure_ascii=False))
    pipe.ltrim(sk, 0, st_max - 1)
    pipe.expire(sk, st_ttl)

    pipe.execute()
    logger.info(
        "【登录预热】chat_session → Redis user_id=%s pkg=%s mysql_turns=%s "
        "→ 瞬时 List %s 条（上限 INSTANT_MEMORY_MAX_TURNS=%s）| 挤出写短期规则 %s 条（上限 SHORT_TERM_MAX_ENTRIES=%s）",
        user_id,
        pkg,
        len(turns),
        len(instant_slice),
        max_turns,
        len(short_slice),
        st_max,
    )
