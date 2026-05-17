"""长期记忆的周期概要更新（滚动合并，非分段追加）。

流程：

1. 从 ``chat_session`` **增量**取材（``create_time > last_consolidate_time``，且落在 24h 滑动窗内）；
2. 改写成叙述性一段 → LLM 写 **本窗增量摘要**（5～8 句）；
3. 与库内已有 ``period_overview`` **LLM 滚动合并为单段**（替换写入，禁止 ``────────────`` 堆叠）；
4. 若检测到历史堆叠或超长，可仅对旧文做一次性压缩。

稳定用户信息由 ``user_profile`` 维护；本模块只保留**近期对话脉络**。
``OLLAMA_MODEL`` / ``OLLAMA_HOST`` 与聊天共用。后台轮询见 ``LONG_MEMORY_SCAN_POLL_INTERVAL_SEC``。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Optional

import ollama

from live2d_db.connection import connection_ctx
from live2d_db.entities import LongMemory
from live2d_db.long_memory_fields import (
    LONG_MEMORY_DB_TEXT_COLUMNS,
    long_memory_has_any_content,
    merge_long_memory_record_for_prompt,
)
from live2d_db.package_key_util import normalize_package_key
from live2d_db.redis_factory import get_redis_client
from live2d_db import memory_layers as _mem
from live2d_db.repositories import ChatSessionRepository, LongMemoryRepository

logger = logging.getLogger(__name__)

_stop_event: Optional[asyncio.Event] = None
_background_task: Optional[asyncio.Task[None]] = None

# 周期概要更新的调度与时间窗。模型与 Ollama 地址与聊天共用 OLLAMA_MODEL / OLLAMA_HOST。
# 后台「扫描候选」间隔（秒）：仅决定多久跑一次 list_candidates；与同一 user×包最短合并间隔 _MIN_GAP_SEC 无关。
_POLL_INTERVAL_SEC = max(60, int(os.getenv("LONG_MEMORY_SCAN_POLL_INTERVAL_SEC", "300")))
_SOURCE_WINDOW_SEC = 86400  # 最近 24 小时内 chat_session
_MIN_GAP_SEC = 86400  # 同一 user×包两次周期概要更新之间的最短间隔
# 送入摘要模型的叙述材料上限；超长时用「首+尾」保留，避免只留尾部时丢掉早期称呼/话题
_MAX_RAW_CHARS = 30000
_NUM_PREDICT = 1536  # 纯文本摘要生成长度上限（材料长时需要多分句）
_PERIOD_OVERVIEW_MAX_CHARS = 4000  # 单次 LLM 产出上限
_PERIOD_OVERVIEW_REPAIR_PREDICT = 1024
_PERIOD_OVERVIEW_EXPAND_PREDICT = 1536
_PERIOD_OVERVIEW_MERGE_PREDICT = 1536
_STACKED_OVERVIEW_SEPARATOR = "────────────"
_SUBSTANCE_RAW_MIN_CHARS = 80


def _long_memory_db_max_chars() -> int:
    try:
        n = int((os.getenv("LONG_MEMORY_DB_MAX_CHARS") or "5000").strip() or "5000")
    except ValueError:
        return 5000
    return max(500, min(20000, n))


def _merge_enabled() -> bool:
    return os.getenv("LONG_MEMORY_MERGE_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _expand_enabled() -> bool:
    return os.getenv("LONG_MEMORY_EXPAND_ENABLED", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _compact_on_stacked_enabled() -> bool:
    return os.getenv("LONG_MEMORY_COMPACT_ON_STACKED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _overview_is_stacked(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _STACKED_OVERVIEW_SEPARATOR in t:
        return True
    return t.count("\n\n") >= 4 and len(t) > 1200


def _overview_needs_compact(existing: str) -> bool:
    t = (existing or "").strip()
    if not t:
        return False
    if _compact_on_stacked_enabled() and _overview_is_stacked(t):
        return True
    return len(t) > _long_memory_db_max_chars()
# 调试日志：chat_session 逐条预览与 LLM 正文截断
_CHAT_SESSION_LOG_SNIPPET = 160  # user_input / ai_reply 单行预览上限
_CHAT_SESSION_LOG_MAX_ROWS = 80  # 最多打印多少条会话明细
_LLM_LOG_BODY_MAX = 12000  # INFO 日志中单次 LLM 输出展示上限


def _truncate_recount_for_llm(recount: str, max_chars: int = _MAX_RAW_CHARS) -> str:
    """叙述按时间正序拼接；超长时若只截尾部会把早期关键话题丢掉，故改为保留首尾。"""
    r = (recount or "").strip()
    if len(r) <= max_chars:
        return r
    mid = (
        "\n\n…（中间较长叙述已省略；以下为时间轴上更靠后的片段，摘要须兼顾首尾两段话题）…\n\n"
    )
    budget = max(1000, max_chars - len(mid))
    head_n = budget // 2
    tail_n = budget - head_n
    logger.warning(
        "叙述合并超长，已改为首尾保留：raw_chars=%s → 上限≈%s（head=%s tail=%s）",
        len(r),
        max_chars,
        head_n,
        tail_n,
    )
    return r[:head_n] + mid + r[-tail_n:]


def _snippet_for_log(text: str, lim: int = _CHAT_SESSION_LOG_SNIPPET) -> str:
    s = " ".join(str(text or "").strip().split())
    if len(s) <= lim:
        return s
    return s[: max(1, lim - 1)] + "…"


def _log_chat_sessions_fetched_for_consolidation(
    rows: list[Any],
    *,
    user_id: int,
    package_key_norm: str,
    raw_keys: list[str],
    window_start: datetime,
    sql_limit: int,
) -> None:
    """打印本次周期概要更新从 MySQL 扫到的 chat_session（便于核对是否扫全）。"""
    n = len(rows)
    logger.info(
        "【周期概要更新】chat_session SQL 命中 user_id=%s pkg_norm=%s raw_package_keys=%s rows=%s "
        "window_start=%s sql_limit=%s（create_time>=window 且 package_key IN raw_keys）",
        user_id,
        package_key_norm,
        raw_keys,
        n,
        window_start.isoformat(timespec="seconds"),
        sql_limit,
    )
    cap = min(n, _CHAT_SESSION_LOG_MAX_ROWS)
    for i in range(cap):
        r = rows[i]
        sid = getattr(r, "session_id", None)
        pk = getattr(r, "package_key", None)
        sk = str(getattr(r, "session_key", "") or "")[:24]
        ct = getattr(r, "create_time", None)
        ui = _snippet_for_log(getattr(r, "user_input", None) or "")
        ar = _snippet_for_log(getattr(r, "ai_reply", None) or "")
        logger.info(
            "  行[%s/%s] session_id=%s create_time=%s db_pkg=%s session_key=%s",
            i + 1,
            n,
            sid,
            ct,
            pk,
            sk or "(空)",
        )
        logger.info("    user_input: %s", ui or "(空)")
        logger.info("    ai_reply: %s", ar or "(空)")
    if n > cap:
        logger.info("  … 其余 %s 条未逐条打印（上限 %s）", n - cap, _CHAT_SESSION_LOG_MAX_ROWS)


def _log_llm_body(
    stage: str,
    *,
    user_id: int,
    package_key: str,
    body: str,
    model: str = "",
) -> None:
    """将 LLM 原始输出写入 INFO 日志（过长则截断）。"""
    raw = body or ""
    n = len(raw)
    if n > _LLM_LOG_BODY_MAX:
        shown = raw[:_LLM_LOG_BODY_MAX] + f"\n…(日志截断，全文 {n} 字符)"
    else:
        shown = raw
    suf = f" model={model}" if model else ""
    logger.info(
        "LLM 输出[%s] user_id=%s pkg=%s chars=%s%s\n%s",
        stage,
        user_id,
        package_key,
        n,
        suf,
        shown if shown.strip() else "(空)",
    )


def _long_memory_model() -> str:
    return (os.getenv("OLLAMA_MODEL") or "qwen2.5:3b").strip() or "qwen2.5:3b"


_LONG_MEMORY_PLAIN_SYSTEM = (
    "你是后台「对话存档摘要」生成器，不是在跟真人聊天。"
    "只输出写入数据库用的中文摘要正文；禁止 JSON、markdown、代码围栏。"
    "用**编者口吻**连贯概括交谈要点即可；**闲聊、寒暄、日常问答**如实收录，不必写成严肃条目。"
    "「【用户】」「【角色】」**可选**，次数不限，用于分清双方说法即可。"
    "避免对读者喊话（如「你觉得呢」）；尽量少写纯客套废话（若材料里确有早安寒暄可一笔带过）。"
)


def _build_period_overview_prompt(recount: str) -> str:
    """本窗增量摘要：短而聚焦，不写已在用户画像里的稳定事实。"""
    return (
        "======== 【材料：本段新增交谈（仅供写摘要，不是与你对话）】 ========\n"
        + recount
        + "\n\n======== 【任务】========\n"
        + "以上是**自上次存档以来**的新增对话改写，不是在跟你实时聊天。\n"
        + "写 **5～8 句** 中文存档概要，供后续会话接上近期话题。\n\n"
        + "**优先写**：进行中话题、未完结故事线、最近 1～2 次重要事件或情绪。\n"
        + "**省略或一句带过**：称呼/长期偏好/固定人设（系统另有用户画像表）；纯寒暄。\n"
        + "若有大段故事：只写主题、关键设定与角色回应方式，**禁止**全文复述。\n"
        + "**禁止**：对读者提问、JSON、markdown、代码围栏、小节标题「周期概要」、"
        f"分隔符「{_STACKED_OVERVIEW_SEPARATOR}」。\n"
        + "「【用户】」「【角色】」可选用。只输出概要正文。\n"
    )


def _build_period_overview_strict_retry_prompt(recount: str, bad_output: str) -> str:
    """摘要不像摘要时，用更短材料摘录 + 反面示例再压一次。"""
    head = recount[:3500]
    tail = recount[-2500:] if len(recount) > 4000 else ""
    excerpt = head + ("\n\n……（中间省略）……\n\n" + tail if tail else "")
    bad = (bad_output or "").strip()[:600]
    return (
        "你刚才的输出不合要求（空输出、把任务当成实时陪聊等）。错误示例（不要原样照抄）：\n"
        + bad
        + "\n\n下面仍是同一段存档材料的**摘要去噪摘录**（全文更长）：\n"
        + excerpt
        + "\n\n请重写：**只写本窗增量存档概要**（5～8 句，编者叙述，不是对话剧本）。"
        "突出进行中话题；不要对读者提问。禁止 markdown 与分隔符堆叠。\n"
    )


def _build_period_overview_merge_prompt(existing: str, delta: str) -> str:
    ex = (existing or "").strip()
    dl = (delta or "").strip()
    return (
        "======== 【已有存档概要】 ========\n"
        + ex
        + "\n\n======== 【本次新增概要】 ========\n"
        + dl
        + "\n\n======== 【任务】========\n"
        + "将两段合并为 **一条** 滚动存档概要（**6～10 句** 中文），整体替换旧文写入数据库。\n"
        + "保留：进行中话题、未完结约定、最近重要事件。\n"
        + "删除：已结束寒暄、与新增段重复、已在用户画像中的称呼/长期偏好。\n"
        + f"**禁止**：分隔符「{_STACKED_OVERVIEW_SEPARATOR}」、多段堆叠、JSON、markdown、对读者提问。\n"
        + "只输出合并后的概要正文。\n"
    )


def _build_period_overview_compact_prompt(stacked_text: str) -> str:
    raw = (stacked_text or "").strip()
    if len(raw) > 14000:
        raw = raw[:7000] + "\n\n…（中间已省略）…\n\n" + raw[-7000:]
    return (
        "======== 【待压缩的多段堆积概要】 ========\n"
        + raw
        + "\n\n======== 【任务】========\n"
        + "上文可能由多次摘要用分隔符拼接而成。请压成 **一条** 6～10 句的近期对话脉络概要。\n"
        + "保留未完结话题与最近事件；删重复与过时寒暄。\n"
        + f"禁止「{_STACKED_OVERVIEW_SEPARATOR}」、JSON、markdown。只输出正文。\n"
    )


def _ollama_chat_plain(
    ollama_cli: ollama.Client,
    model: str,
    user_content: str,
    *,
    num_predict: Optional[int] = None,
) -> str:
    np = _NUM_PREDICT if num_predict is None else int(num_predict)
    opts: dict[str, Any] = {"num_predict": np, "temperature": 0}
    messages = [
        {"role": "system", "content": _LONG_MEMORY_PLAIN_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    kw: dict[str, Any] = dict(model=model, messages=messages, options=opts)
    resp = ollama_cli.chat(**kw)
    return _ollama_message_content(resp).strip()


def _normalize_plain_summary(raw: str) -> str:
    """去掉模型偶发的 markdown 围栏、重复标题与首尾空白。"""
    t = (raw or "").strip()
    if not t:
        return ""
    if "```" in t:
        fence = re.search(r"```(?:\w*)?\s*([\s\S]*?)\s*```", t)
        if fence:
            t = fence.group(1).strip()
    t = re.sub(r"^【周期概要】\s*", "", t.strip())
    t = re.sub(r"^周期概要\s*[:：]?\s*", "", t, flags=re.IGNORECASE)
    return t.strip()


def _ollama_client() -> ollama.Client:
    host = (os.getenv("OLLAMA_HOST") or "http://127.0.0.1:11434").strip()
    os.environ.setdefault("OLLAMA_HOST", host)
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


def _dialog_window_has_substance(merged_recount: str) -> bool:
    """判断叙述合并后的材料是否超出简短寒暄（用于决定是否必须有规范的 period_overview）。"""
    t = (merged_recount or "").strip()
    if len(t) < _SUBSTANCE_RAW_MIN_CHARS:
        return False
    # 叙述合并用分号串联多段，段数多通常即有实质往来
    if t.count("；") >= 2:
        return True
    return len(t) >= 200


def _period_overview_sentence_ends(text: str) -> int:
    """粗略统计句末标点数量（用于判断是否一句话糊弄）。"""
    return len(re.findall(r"[。！？…]", (text or "").strip()))


def _period_overview_density_ok(summary: str, recount: str) -> bool:
    """摘要相对叙述材料不能过短，否则长期记忆里只剩泛泛一句。"""
    s = (summary or "").strip()
    r = (recount or "").strip()
    rl = len(r)
    sl = len(s)
    if rl < 400:
        return sl >= 35 and _period_overview_sentence_ends(s) >= 1
    # 期望字数：随材料变长，封顶避免对小模型苛求过长
    min_chars = max(140, min(950, rl // 22))
    if sl < min_chars:
        return False
    min_sent = 3 if rl < 1200 else (4 if rl < 4000 else 6)
    return _period_overview_sentence_ends(s) >= min_sent


def _build_period_overview_expand_prompt(recount: str, skinny_summary: str) -> str:
    """主摘要相对材料过短时追加一轮：补足话题覆盖面。"""
    prev = (skinny_summary or "").strip()[:700]
    head = recount[:4500]
    tail = recount[-3500:] if len(recount) > 6000 else ""
    excerpt = head + ("\n\n……（中间省略）……\n\n" + tail if tail else "")
    return (
        "上一份「存档概要」信息量不足，示例（禁止照抄句式，须重写得更充实）：\n"
        + prev
        + "\n\n======== 【完整叙述材料】 ========\n"
        + excerpt
        + "\n\n======== 【任务】========\n"
        "请重写 **全新的** 存档概要（编者叙述，不是多轮剧本）。要求：\n"
        "1）写到材料里 **多条线索**：称呼/人设、故事梗概、旅行/太空等话题及角色回应方式；禁止全文复述长篇故事。\n"
        "2）「【用户】」「【角色】」**可选用**，次数不限。\n"
        "3）材料很长时 **不少于 10 句**，字数明显多于上一份；不要对读者追问。\n"
        "4）禁止 markdown。\n"
        "只输出概要正文。\n"
    )


def _truncate_period_overview(text: str) -> str:
    """单次 LLM 段上限。"""
    s = (text or "").strip()
    if len(s) <= _PERIOD_OVERVIEW_MAX_CHARS:
        return s
    return s[-(_PERIOD_OVERVIEW_MAX_CHARS - 1) :].lstrip()


def _truncate_period_overview_for_db(text: str) -> str:
    """库内字段上限：超长时保留尾部（最新脉络）。"""
    lim = _long_memory_db_max_chars()
    s = (text or "").strip()
    if len(s) <= lim:
        return s
    return "…" + s[-(lim - 1) :].lstrip()


def _merge_overviews_via_llm(
    ollama_cli: ollama.Client,
    model: str,
    existing: str,
    delta: str,
    *,
    user_id: int,
    package_key: str,
) -> str:
    if not (delta or "").strip():
        return (existing or "").strip()
    if not (existing or "").strip():
        return _truncate_period_overview_for_db(_truncate_period_overview(delta))
    if not _merge_enabled():
        return _truncate_period_overview_for_db(
            _truncate_period_overview((existing or "").strip() + "\n\n" + (delta or "").strip())
        )
    prompt = _build_period_overview_merge_prompt(existing, delta)
    try:
        blob = _ollama_chat_plain(
            ollama_cli,
            model,
            prompt,
            num_predict=_PERIOD_OVERVIEW_MERGE_PREDICT,
        )
    except Exception:
        logger.exception(
            "长期记忆滚动合并 LLM 失败 user_id=%s pkg=%s model=%s",
            user_id,
            package_key,
            model,
        )
        return _truncate_period_overview_for_db((existing or "").strip())
    merged = _normalize_plain_summary(blob)
    _log_llm_body(
        "period_overview_merge_raw",
        user_id=user_id,
        package_key=package_key,
        body=merged,
        model=model,
    )
    if not merged:
        logger.warning(
            "滚动合并无产出，保留旧概要 user_id=%s pkg=%s",
            user_id,
            package_key,
        )
        return _truncate_period_overview_for_db((existing or "").strip())
    return _truncate_period_overview_for_db(_truncate_period_overview(merged))


def _compact_stacked_overview_via_llm(
    ollama_cli: ollama.Client,
    model: str,
    stacked_text: str,
    *,
    user_id: int,
    package_key: str,
) -> str:
    raw = (stacked_text or "").strip()
    if not raw:
        return ""
    prompt = _build_period_overview_compact_prompt(raw)
    try:
        blob = _ollama_chat_plain(
            ollama_cli,
            model,
            prompt,
            num_predict=_PERIOD_OVERVIEW_MERGE_PREDICT,
        )
    except Exception:
        logger.exception(
            "长期记忆堆积压缩 LLM 失败 user_id=%s pkg=%s",
            user_id,
            package_key,
        )
        return _truncate_period_overview_for_db(raw)
    compact = _normalize_plain_summary(blob)
    _log_llm_body(
        "period_overview_compact_raw",
        user_id=user_id,
        package_key=package_key,
        body=compact,
        model=model,
    )
    if not compact:
        return _truncate_period_overview_for_db(raw)
    return _truncate_period_overview_for_db(_truncate_period_overview(compact))


def _repair_period_overview_only(
    ollama_cli: ollama.Client,
    model: str,
    raw_text: str,
    rejected_preview: str,
    *,
    user_id: int = 0,
    package_key: str = "",
) -> str:
    """针对不合规的概要再压缩一次，输出纯文本。"""
    raw = (raw_text or "").strip()
    if len(raw) > 16000:
        raw = "…（前文过长已省略）…\n" + raw[-16000:]
    user_msg = (
        "下面是一段 **叙述性的来往经过**（由聊天记录改写而成，**禁止照抄原句**）。\n\n"
        "你上一次写的摘要不合要求，预览如下（请整体重写，禁止改成另一种寒暄）：\n"
        + (rejected_preview[:900] or "（空）")
        + "\n\n请重新阅读全文，写 **存档摘要**（连贯编者说明；可含寒暄与闲聊，不是多轮问答剧本）。"
        "不要对读摘要的人追问。\n"
        "「【用户】」「【角色】」可用可不用，次数不限。\n"
        "不要 JSON、markdown；长篇故事不全文复述，但要多句交代主题与走向。\n\n"
        "======== 【叙述性经过全文】 ========\n"
        + raw
        + "\n\n======== 【再次强调】========\n"
        "只输出摘要正文；不要对读者说话。\n"
    )
    try:
        blob = _ollama_chat_plain(
            ollama_cli,
            model,
            user_msg,
            num_predict=_PERIOD_OVERVIEW_REPAIR_PREDICT,
        )
    except Exception:
        logger.exception("period_overview 专用修复 LLM 请求失败 model=%s", model)
        return ""
    if user_id and package_key:
        _log_llm_body(
            "period_overview_repair_raw",
            user_id=user_id,
            package_key=package_key,
            body=blob,
            model=model,
        )
    return _normalize_plain_summary(blob)


def _finalize_period_overview(
    ollama_cli: ollama.Client,
    model: str,
    raw_text: str,
    merged: LongMemory,
    *,
    substance: bool,
) -> None:
    """就地修正 ``merged.period_overview``（主模型纯文本摘要已写入 merged）。"""
    po = str(merged.period_overview or "").strip()
    po = _truncate_period_overview(po)

    if not substance:
        merged.period_overview = po
        return

    if po:
        merged.period_overview = po
        return

    fixed = _repair_period_overview_only(
        ollama_cli,
        model,
        raw_text,
        po,
        user_id=merged.user_id,
        package_key=str(merged.package_key or ""),
    )
    fixed = _truncate_period_overview(fixed)
    if fixed:
        merged.period_overview = fixed
        logger.info("period_overview 经专用修复 LLM 写入 user_id=%s pkg=%s", merged.user_id, merged.package_key)
        return

    merged.period_overview = ""
    logger.warning(
        "实质对话窗口内 period_overview 仍为空（修复失败） user_id=%s pkg=%s",
        merged.user_id,
        merged.package_key,
    )


def _merge_chat_sessions_to_narrative_string(rows: list[Any]) -> str:
    """把 ``chat_session`` 多轮记录改写成 **一段** 口述式叙述（不按轮次排版、不用聊天记录标记）。

    供下游模型像「听故事」一样把握大意后再写摘要。
    """
    if not rows:
        return ""
    pieces: list[str] = []
    for r in rows:
        u = (getattr(r, "user_input", None) or "").strip()
        a = (getattr(r, "ai_reply", None) or "").strip()
        if u and a:
            pieces.append(f"有人先聊到「{u}」，接着对话里又谈到「{a}」")
        elif u:
            pieces.append(f"谈话里又提到「{u}」")
        elif a:
            pieces.append(f"另一边说到「{a}」")
    if not pieces:
        return ""
    return "；".join(pieces) + "。"


def _long_memory_with_merged_overview(
    period_overview: str,
    *,
    user_id: int,
    package_key: str,
    now: datetime,
) -> LongMemory:
    """写入滚动合并后的单段 period_overview。"""
    m = LongMemory(
        user_id=user_id,
        package_key=package_key,
        memory_type="long",
        last_consolidate_time=now,
    )
    merged = _truncate_period_overview_for_db((period_overview or "").strip())
    for k in LONG_MEMORY_DB_TEXT_COLUMNS:
        setattr(m, k, merged if k == "period_overview" else "")
    return m


def consolidate_one(
    conn: Any,
    redis_cli: Any,
    user_id: int,
    package_key_norm: str,
) -> bool:
    """对单用户、单逻辑包执行一次周期概要更新；成功返回 True。

    增量取材 → 本窗摘要 → 与已有概要滚动合并（或仅压缩历史堆叠）。
    """
    now = datetime.now()
    window_start = now - timedelta(seconds=_SOURCE_WINDOW_SEC)

    raw_keys_all = ChatSessionRepository.distinct_package_keys_for_user(conn, user_id)
    raw_keys = [
        k
        for k in raw_keys_all
        if normalize_package_key(k, fallback="default") == package_key_norm
    ]
    if not raw_keys:
        raw_keys = [package_key_norm]

    existing_rec = LongMemoryRepository.get_by_user_pkg(conn, user_id, package_key_norm)
    existing_overview = ""
    since_exclusive: Optional[datetime] = None
    if existing_rec is not None:
        existing_overview = str(getattr(existing_rec, "period_overview", "") or "").strip()
        since_exclusive = getattr(existing_rec, "last_consolidate_time", None)
        if since_exclusive is not None:
            logger.info(
                "增量取材 since_exclusive=%s user_id=%s pkg=%s 现有概要长度=%s",
                since_exclusive.isoformat(timespec="seconds"),
                user_id,
                package_key_norm,
                len(existing_overview),
            )

    needs_compact = _overview_needs_compact(existing_overview)

    rows = ChatSessionRepository.list_for_long_memory_window(
        conn,
        user_id,
        raw_keys,
        since_exclusive=since_exclusive,
        window_start=window_start,
        limit=5000,
    )
    _log_chat_sessions_fetched_for_consolidation(
        rows,
        user_id=user_id,
        package_key_norm=package_key_norm,
        raw_keys=raw_keys,
        window_start=window_start,
        sql_limit=5000,
    )
    if not rows and not needs_compact:
        logger.info("长期记忆跳过：无新对话 user_id=%s pkg=%s", user_id, package_key_norm)
        return False

    ollama_cli = _ollama_client()
    model = _long_memory_model()

    if not rows and needs_compact:
        logger.info(
            "长期记忆仅压缩堆积概要 user_id=%s pkg=%s len=%s",
            user_id,
            package_key_norm,
            len(existing_overview),
        )
        compacted = _compact_stacked_overview_via_llm(
            ollama_cli,
            model,
            existing_overview,
            user_id=user_id,
            package_key=package_key_norm,
        )
        if not compacted or compacted == existing_overview:
            return False
        m = _long_memory_with_merged_overview(
            compacted,
            user_id=user_id,
            package_key=package_key_norm,
            now=now,
        )
        if not long_memory_has_any_content(m):
            return False
        LongMemoryRepository.upsert_by_user_pkg(conn, m)
        if redis_cli is not None:
            try:
                merged_txt = merge_long_memory_record_for_prompt(m)
                _mem.write_long_memory_text(redis_cli, user_id, package_key_norm, merged_txt)
            except Exception:
                logger.exception("长期记忆写 Redis 失败 user_id=%s pkg=%s", user_id, package_key_norm)
        logger.info("长期记忆堆积压缩完成 user_id=%s pkg=%s", user_id, package_key_norm)
        return True

    recount_full = _merge_chat_sessions_to_narrative_string(rows)
    recount = _truncate_recount_for_llm(recount_full, _MAX_RAW_CHARS)

    logger.info(
        "叙述性合并 user_id=%s pkg=%s rows=%s merged_chars=%s → llm_chars=%s recount_head=%r recount_tail=%r",
        user_id,
        package_key_norm,
        len(rows),
        len(recount_full),
        len(recount),
        recount[:400],
        recount[-400:] if recount else "",
    )

    prompt = _build_period_overview_prompt(recount)

    substance = _dialog_window_has_substance(recount)
    rc_len = len(recount)
    np_main = _NUM_PREDICT
    if rc_len > 6000:
        np_main = min(3072, _NUM_PREDICT + 1024)
    elif rc_len > 2500:
        np_main = min(2560, _NUM_PREDICT + 512)

    resp_text = ""
    try:
        resp_text = _ollama_chat_plain(ollama_cli, model, prompt, num_predict=np_main)
    except Exception:
        logger.exception(
            "长期记忆主摘要 LLM 请求异常 user_id=%s pkg=%s model=%s",
            user_id,
            package_key_norm,
            model,
        )

    _log_llm_body(
        "main_raw",
        user_id=user_id,
        package_key=package_key_norm,
        body=resp_text,
        model=model,
    )

    overview_plain = _normalize_plain_summary(resp_text)
    retry_strict = substance and not overview_plain
    if retry_strict:
        logger.info(
            "主摘要规范化后为空（模型未产出或非文本），将严格重试 user_id=%s pkg=%s raw_chars=%s",
            user_id,
            package_key_norm,
            len(resp_text or ""),
        )
        retry_prompt = _build_period_overview_strict_retry_prompt(recount, overview_plain or resp_text)
        try:
            retry_resp = _ollama_chat_plain(ollama_cli, model, retry_prompt)
        except Exception:
            logger.exception(
                "长期记忆严格重试 LLM 异常 user_id=%s pkg=%s model=%s",
                user_id,
                package_key_norm,
                model,
            )
            retry_resp = ""
        if retry_resp.strip():
            _log_llm_body(
                "main_strict_retry_raw",
                user_id=user_id,
                package_key=package_key_norm,
                body=retry_resp,
                model=model,
            )
            retry_plain = _normalize_plain_summary(retry_resp)
            if retry_plain:
                overview_plain = retry_plain
                resp_text = retry_resp

    # 相对叙述材料过短时可扩写（默认关闭，避免概要膨胀）
    if (
        _expand_enabled()
        and substance
        and overview_plain
        and not _period_overview_density_ok(overview_plain, recount)
    ):
        logger.warning(
            "period_overview 相对材料偏短，触发扩写 user_id=%s pkg=%s summary_len=%s recount_len=%s",
            user_id,
            package_key_norm,
            len(overview_plain),
            len(recount),
        )
        expand_prompt = _build_period_overview_expand_prompt(recount, overview_plain)
        exp_resp = ""
        try:
            exp_resp = _ollama_chat_plain(
                ollama_cli,
                model,
                expand_prompt,
                num_predict=_PERIOD_OVERVIEW_EXPAND_PREDICT,
            )
        except Exception:
            logger.exception(
                "长期记忆扩写 LLM 异常 user_id=%s pkg=%s model=%s",
                user_id,
                package_key_norm,
                model,
            )
        if exp_resp.strip():
            _log_llm_body(
                "period_overview_expand_raw",
                user_id=user_id,
                package_key=package_key_norm,
                body=exp_resp,
                model=model,
            )
        exp_plain = _normalize_plain_summary(exp_resp)
        if exp_plain:
            if _period_overview_density_ok(exp_plain, recount) or len(exp_plain) > len(
                overview_plain
            ):
                overview_plain = exp_plain

    if not overview_plain:
        logger.warning(
            "长期记忆主摘要 LLM 返回空 user_id=%s pkg=%s",
            user_id,
            package_key_norm,
        )

    work_existing = existing_overview
    if needs_compact and work_existing:
        logger.info(
            "合并前先压缩堆积概要 user_id=%s pkg=%s",
            user_id,
            package_key_norm,
        )
        work_existing = _compact_stacked_overview_via_llm(
            ollama_cli,
            model,
            work_existing,
            user_id=user_id,
            package_key=package_key_norm,
        )

    compact_only_progress = bool(
        work_existing
        and work_existing != existing_overview
        and not overview_plain
    )
    if rows and substance and not overview_plain and not compact_only_progress:
        logger.warning(
            "有实质新对话但本窗摘要为空，跳过写入以免推进 last_consolidate_time user_id=%s pkg=%s",
            user_id,
            package_key_norm,
        )
        return False

    if overview_plain:
        final_overview = _merge_overviews_via_llm(
            ollama_cli,
            model,
            work_existing,
            overview_plain,
            user_id=user_id,
            package_key=package_key_norm,
        )
    elif work_existing:
        final_overview = work_existing
    else:
        final_overview = ""

    m = _long_memory_with_merged_overview(
        final_overview,
        user_id=user_id,
        package_key=package_key_norm,
        now=now,
    )

    _po_preview = str(getattr(m, "period_overview", "") or "").strip()
    logger.info(
        "主模型摘要 period_overview user_id=%s pkg=%s len=%s",
        user_id,
        package_key_norm,
        len(_po_preview),
    )
    if _po_preview:
        _log_llm_body(
            "main_period_overview",
            user_id=user_id,
            package_key=package_key_norm,
            body=_po_preview,
            model=model,
        )

    _finalize_period_overview(ollama_cli, model, recount, m, substance=substance)

    _po_final = str(getattr(m, "period_overview", "") or "").strip()
    logger.info(
        "finalize 后 period_overview user_id=%s pkg=%s substance=%s len=%s",
        user_id,
        package_key_norm,
        substance,
        len(_po_final),
    )
    if _po_final:
        _log_llm_body(
            "final_period_overview",
            user_id=user_id,
            package_key=package_key_norm,
            body=_po_final,
            model=model,
        )

    if not long_memory_has_any_content(m):
        logger.warning(
            "长期记忆无可写入：period_overview 为空 user_id=%s pkg=%s",
            user_id,
            package_key_norm,
        )
        return False

    LongMemoryRepository.upsert_by_user_pkg(conn, m)
    if redis_cli is not None:
        try:
            merged = merge_long_memory_record_for_prompt(m)
            _mem.write_long_memory_text(redis_cli, user_id, package_key_norm, merged)
        except Exception:
            logger.exception("长期记忆写 Redis 失败 user_id=%s pkg=%s", user_id, package_key_norm)
    logger.info("长期记忆周期概要更新完成 user_id=%s pkg=%s", user_id, package_key_norm)
    return True


def run_manual_long_memory_backfill() -> int:
    """对统计窗口内所有出现过会话的 user×逻辑包各执行一次周期概要更新；返回组合数量。"""
    redis_cli = get_redis_client(logger)
    with connection_ctx() as conn:
        pairs = ChatSessionRepository.distinct_user_normalized_packages_in_window(
            conn, _SOURCE_WINDOW_SEC
        )
    for uid, pkg_norm in pairs:
        try:
            with connection_ctx() as conn2:
                consolidate_one(conn2, redis_cli, uid, pkg_norm)
        except Exception:
            logger.exception("长期记忆单组周期概要更新异常 user_id=%s pkg=%s", uid, pkg_norm)
    return len(pairs)


def _run_tick() -> list[tuple[int, str]]:
    """扫描候选并执行合并；返回本次 **确实写入库** 的 ``(user_id, package_key_norm)`` 列表。"""
    redis_cli = get_redis_client(logger)
    with connection_ctx() as conn:
        candidates = LongMemoryRepository.list_candidates_for_consolidation(
            conn, _SOURCE_WINDOW_SEC, _MIN_GAP_SEC
        )
    updated: list[tuple[int, str]] = []
    for uid, pkg_norm, _ in candidates:
        try:
            with connection_ctx() as conn2:
                ok = consolidate_one(conn2, redis_cli, uid, pkg_norm)
            if ok:
                updated.append((uid, pkg_norm))
        except Exception:
            logger.exception("长期记忆单组周期概要更新异常 user_id=%s pkg=%s", uid, pkg_norm)
    return updated


async def _sleep_interval_or_until_stop() -> None:
    assert _stop_event is not None
    interval = float(_POLL_INTERVAL_SEC)
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
            logger.exception("长期记忆后台 tick 异常")
        if _stop_event.is_set():
            break
        await _sleep_interval_or_until_stop()


async def start_long_memory_consolidator() -> None:
    """在 FastAPI lifespan 中启动后台 asyncio 任务。"""
    global _stop_event, _background_task
    if _background_task is not None and not _background_task.done():
        return
    _stop_event = asyncio.Event()
    _background_task = asyncio.create_task(_background_loop(), name="long_memory_consolidator")
    logger.info(
        "长期记忆后台任务已启动 poll_interval=%ss source_window=%ss min_gap=%ss（参数见 consolidator 模块常量）",
        _POLL_INTERVAL_SEC,
        _SOURCE_WINDOW_SEC,
        _MIN_GAP_SEC,
    )


async def stop_long_memory_consolidator() -> None:
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
    logger.info("长期记忆后台任务已停止")


if __name__ == "__main__":
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    logging.basicConfig(level=logging.INFO)
    n = run_manual_long_memory_backfill()
    logging.getLogger(__name__).info("长期记忆手动回填结束，组合数=%s", n)
