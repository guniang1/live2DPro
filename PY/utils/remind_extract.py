"""从单轮对话（用户输入 + 助手回复）抽取定时关怀条目，写入 ``remind_trigger``。

在 ``/ws/chat`` 每轮落库后由后台线程调用；可通过环境变量关闭或换模型。
抽取 LLM 所见的「当前时刻」先取 **Unix 时间戳** 再换算为本机本地 **``年月日:时分秒``** 字符串（与相对时间换算表同一格式），再写入 User 消息文首，便于模型推算 ``trigger_time``。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any

import ollama
import pymysql.err

from live2d_db.connection import connection_ctx
from live2d_db.db_config import DbConfig
from live2d_db.entities import RemindTrigger
from live2d_db.memory_layers import (
    format_long_memory_block,
    read_instant_turns_chronological,
)
from live2d_db.package_key_util import normalize_package_key
from live2d_db.redis_factory import get_redis_client
from live2d_db.repositories import (
    LongMemoryRepository,
    PersonaRepository,
    RemindTriggerRepository,
)

logger = logging.getLogger(__name__)

_ALLOWED_TYPES = frozenset({"生日", "纪念日", "考试", "日常关怀"})
_MAX_REMINDERS_PER_TURN = 8
_CONTENT_MAX = 4000
_SKEW = timedelta(minutes=2)
_REMIND_CTX_PERSONA_MAX = 3500
_REMIND_CTX_INSTANT_MAX = 4500


def _now_local_from_timestamp() -> datetime:
    """当前时刻：取 Unix 时间戳再转为本机本地 naive datetime（秒精度），与调度到期判定一致。"""
    return datetime.fromtimestamp(time.time()).replace(microsecond=0)


def _format_datetime_for_extract_llm(dt: datetime) -> str:
    """传给抽取 LLM 的公历时间：``年月日:时分秒``（例如 ``2026年05月09日:14:30:45``），便于模型对齐中文语境。"""
    return dt.replace(microsecond=0).strftime("%Y年%m月%d日:%H:%M:%S")


def _enabled() -> bool:
    return os.getenv("REMIND_EXTRACT_FROM_DIALOGUE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _model_name() -> str:
    return (os.getenv("REMIND_EXTRACT_MODEL") or os.getenv("OLLAMA_MODEL", "qwen2.5:3b")).strip()


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


def _sanitize_llm_json_blob(blob: str) -> str:
    """修正小模型常见非标准 JSON（尾随逗号、弯引号等），不改变语义结构。"""
    s = blob.strip()
    # 中文语境下偶发全角/弯引号，仅替换明显成对的弯双引号，避免误伤字符串内的 ASCII '
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u300c", '"').replace("\u300d", '"')
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r",(\s*})", r"\1", s)
        s = re.sub(r",(\s*\])", r"\1", s)
    return s


def _loads_json_dict(blob: str) -> dict[str, Any] | None:
    for candidate in (blob, _sanitize_llm_json_blob(blob)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else None
    return None


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
    parsed = _loads_json_dict(blob)
    if parsed is not None:
        return parsed
    # 部分模型会输出多个 {...}；从首个 { 起用栈配对找到与之匹配的 }，避免 rfind 截错
    depth = 0
    start = lo
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                inner = text[start : i + 1]
                parsed = _loads_json_dict(inner)
                if parsed is not None:
                    return parsed
                break
    return None


def _coerce_reminders_root(parsed: Any) -> dict[str, Any] | None:
    """将 ``json.loads`` 结果规范为含 ``reminders`` 的字典。

    兼容：顶层数组 ``[]`` / ``[{...}]``（部分小模型在 ``format=json`` 下只输出数组）、
    以及单条提醒对象未包在 ``reminders`` 键下的情况。
    """
    if isinstance(parsed, list):
        return {"reminders": parsed}
    if not isinstance(parsed, dict):
        return None
    if "reminders" in parsed or "items" in parsed or "triggers" in parsed:
        return parsed
    if "trigger_type" in parsed and (
        "trigger_time" in parsed
        or "trigger_content" in parsed
        or "scenario_detail" in parsed
    ):
        return {"reminders": [parsed]}
    return None


def _parse_llm_reminder_json(text: str) -> dict[str, Any] | None:
    """解析抽取模型输出：整段 JSON（含裸 ``[]``）、或文中嵌入的 ``{...}``。"""
    text = (text or "").strip()
    if not text:
        return None
    for candidate in (text, _sanitize_llm_json_blob(text)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        coerced = _coerce_reminders_root(parsed)
        if coerced is not None:
            return coerced
    embedded = _extract_json_blob(text)
    if embedded is not None:
        return _coerce_reminders_root(embedded)
    return None


def _parse_scene_time_string(raw: str | None) -> datetime | None:
    """解析前端 ``scene_time``（如 zh-CN ``2026/5/9 14:30:45``）；失败则返回 None。"""
    s = (raw or "").strip()
    if not s:
        return None
    # 去掉常见星期前缀，便于匹配日期段
    s = re.sub(
        r"^(?:周[一二三四五六日天]|星期[一二三四五六日天])+[,，\s]*",
        "",
        s,
    )
    m = re.search(
        r"(?P<y>\d{4})\s*[/\-\.年]\s*(?P<mo>\d{1,2})\s*[/\-\.月]\s*(?P<d>\d{1,2})\s*日?"
        r"(?:\s+(?P<h>\d{1,2}):(?P<mi>\d{1,2})(?::(?P<sec>\d{1,2}))?)?",
        s,
    )
    if m:
        y, mo, d = int(m["y"]), int(m["mo"]), int(m["d"])
        h = int(m.group("h") or 0)
        mi = int(m.group("mi") or 0)
        sec = int(m.group("sec") or 0)
        try:
            return datetime(y, mo, d, h, mi, sec)
        except ValueError:
            pass
    try:
        norm = s.replace(" ", "T", 1) if " " in s and "T" not in s else s
        return datetime.fromisoformat(norm[:19])
    except ValueError:
        return None


def _resolve_relative_offsets(user_input: str, ref_now: datetime) -> str:
    """扫描用户句中的相对时间表达，按参考时刻算出绝对时间，供注入抽取 prompt。

    短时偏移（几分钟后等）由服务器计算，避免小模型分钟进位错误。
    """
    logger.debug(
        "relative offsets: system_now=%s ref_now=%s",
        _format_datetime_for_extract_llm(_now_local_from_timestamp()),
        _format_datetime_for_extract_llm(ref_now),
    )
    hints: list[str] = []
    ref = ref_now.replace(microsecond=0)
    patterns = [
        (r"(\d+)\s*分钟后", lambda m: ref + timedelta(minutes=int(m.group(1)))),
        (r"(\d+)\s*小时后", lambda m: ref + timedelta(hours=int(m.group(1)))),
        (r"半小时后", lambda m: ref + timedelta(minutes=30)),
        (r"(\d+)\s*天后", lambda m: ref + timedelta(days=int(m.group(1)))),
        (
            r"明天",
            lambda m: (ref + timedelta(days=1)).replace(
                hour=9, minute=0, second=0, microsecond=0
            ),
        ),
        (
            r"后天",
            lambda m: (ref + timedelta(days=2)).replace(
                hour=9, minute=0, second=0, microsecond=0
            ),
        ),
    ]
    for pattern, calc in patterns:
        for m in re.finditer(pattern, user_input):
            try:
                dt = calc(m).replace(microsecond=0)
                hints.append(
                    f"「{m.group(0)}」= {_format_datetime_for_extract_llm(dt)}（已由服务器换算，直接使用此值）"
                )
            except (ValueError, OverflowError):
                pass
    return "\n".join(hints)


def _relative_minute_anchor(user_input: str, ref_now: datetime) -> datetime | None:
    """用户句中「N 分钟后」对应的绝对时刻（与 :func:`_resolve_relative_offsets` 一致）。"""
    m = re.search(r"(\d+)\s*分钟后", (user_input or "").strip())
    if not m:
        return None
    try:
        return ref_now.replace(microsecond=0) + timedelta(minutes=int(m.group(1)))
    except (ValueError, OverflowError):
        return None


def _parse_trigger_time(raw: str) -> datetime | None:
    """解析模型输出的触发时刻；支持粗粒度（仅需年月、年月日、或到小时），缺失部分按规则补齐。

    - ``YYYY-MM`` → 该月 1 日 09:00:00
    - ``YYYY-MM-DD``（无时刻）→ 当日 09:00:00
    - 有时刻仅到小时（如 ``2026-05-09T13``、``2026-05-09 13``）→ 该小时 00 分 00 秒（**会丢失亚小时精度**；「几分钟后」类应依赖 ``_resolve_relative_offsets`` 注入的绝对时间）
    - 含分即可不带秒（解析后秒为 0）
    """
    s = (raw or "").strip()
    if not s:
        return None
    s = s.replace("Z", "").split("+")[0].strip().replace("/", "-")

    # 仅年月（整月粒度）
    m_ym = re.match(r"^(\d{4})-(\d{1,2})$", s)
    if m_ym:
        y, mo = int(m_ym.group(1)), int(m_ym.group(2))
        try:
            return datetime(y, mo, 1, 9, 0, 0)
        except ValueError:
            return None

    y: int | None = None
    mo: int | None = None
    d: int | None = None
    rest = ""

    md = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if md:
        y, mo, d = int(md.group(1)), int(md.group(2)), int(md.group(3))
        rest = s[md.end() :].strip()
    else:
        mc = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", s)
        if mc:
            y, mo, d = int(mc.group(1)), int(mc.group(2)), int(mc.group(3))
            rest = s[mc.end() :].strip()
        else:
            return None

    rest = re.sub(r"^[Tt\s]+", "", rest)
    if "." in rest:
        rest = rest.split(".", 1)[0].strip()
    h, mi, sec = 9, 0, 0
    if rest:
        tm = re.match(r"^(\d{1,2})(?::(\d{1,2})(?::(\d{1,2}))?)?", rest)
        if tm:
            h = int(tm.group(1))
            mi = int(tm.group(2)) if tm.group(2) is not None else 0
            sec = int(tm.group(3)) if tm.group(3) is not None else 0
    try:
        return datetime(y, mo, d, h, mi, sec)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace(" ", "T", 1))
    except ValueError:
        return None


def _truncate_ctx(text: str, max_chars: int) -> str:
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    return s[: max(1, max_chars - 1)].rstrip() + "…"


def _long_memory_overview_for_package(
    user_id: int, package_key: str | None
) -> tuple[str, str]:
    """当前包的规范化键与 ``long_memory.period_overview`` 摘要（仅作抽取 LLM 上下文，不入库为外键）。"""
    pkg_norm = normalize_package_key((package_key or "").strip() or None, fallback="default")
    if user_id < 1:
        return pkg_norm, ""
    try:
        with connection_ctx(DbConfig.from_env()) as conn:
            lm = LongMemoryRepository.get_by_user_pkg(conn, user_id, pkg_norm)
        if lm is None:
            return pkg_norm, ""
        overview = format_long_memory_block(lm.period_overview or "")
        return pkg_norm, overview
    except Exception:
        logger.exception(
            "查询 long_memory 概要失败 user_id=%s package_key=%s",
            user_id,
            pkg_norm,
        )
        return pkg_norm, ""


def _persona_block_for_package(user_id: int, package_key: str) -> str:
    """与聊天链路一致的包级人设（语气 + 角色设定），用于关怀话术风格。"""
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
        return _truncate_ctx("\n\n".join(parts), _REMIND_CTX_PERSONA_MAX)
    except pymysql.err.ProgrammingError as e:
        code = e.args[0] if e.args else None
        if code in (1146, 1054):
            logger.warning(
                "关怀抽取读取人设跳过（persona 表或列不可用 errno=%s），仍可仅用记忆与对话生成文案",
                code,
            )
            return ""
        logger.exception("关怀抽取读取人设失败 user_id=%s package=%s", user_id, pkg)
        return ""
    except Exception:
        logger.exception("关怀抽取读取人设失败 user_id=%s package=%s", user_id, pkg)
        return ""


def _instant_memory_context_block(user_id: int, package_key: str) -> str:
    """Redis 瞬时记忆（本会话近期多轮）；写入时机可能略晚于本轮，故 LLM 侧仍配有「本轮对话」块。"""
    if user_id < 1:
        return ""
    cli = get_redis_client(logger)
    if cli is None:
        return ""
    turns = read_instant_turns_chronological(cli, user_id, package_key)
    if not turns:
        return ""
    lines: list[str] = []
    for t in turns:
        u = (t.get("u") or "").strip()
        a = (t.get("a") or "").strip()
        if u:
            lines.append(f"用户：{u}")
        if a:
            lines.append(f"助手：{a}")
    return _truncate_ctx("\n".join(lines), _REMIND_CTX_INSTANT_MAX)


def _build_remind_extract_context(
    user_id: int, pkg_norm: str, period_overview: str
) -> str:
    blocks: list[str] = []
    persona = _persona_block_for_package(user_id, pkg_norm)
    if persona:
        blocks.append(f"【当前模型人设】\n{persona}")
    ov = (period_overview or "").strip()
    if ov:
        blocks.append(
            "【长期记忆摘要】（long_memory 周期概要，仅供抽取参考；入库时将绑定本轮 chat_session.session_id）\n"
            + ov
        )
    instant = _instant_memory_context_block(user_id, pkg_norm)
    if instant:
        blocks.append(
            "【瞬时记忆】（Redis 中本会话近期轮次；若尚未写入本轮，以下一节「本轮对话」为准）\n"
            + instant
        )
    return "\n\n".join(blocks).strip()


def _clamp_model_year_to_reference(
    dt: datetime, ref_now: datetime, user_input: str, ai_reply: str
) -> datetime:
    """小模型常在无「明年」依据时把月日写成下一公历年，导致永不触发；收回至参考年。"""
    if dt.year <= ref_now.year:
        return dt
    blob = f"{user_input}\n{ai_reply}"
    if any(k in blob for k in ("明年", "来年", "下一年", "后年")):
        return dt
    if "next year" in blob.casefold():
        return dt
    ys = str(dt.year)
    if ys in blob:
        return dt
    try:
        fixed = dt.replace(year=ref_now.year)
    except ValueError:
        return dt
    logger.info(
        "关怀抽取：trigger_time 年份超前且无依据，已按参考年纠正 %s → %s",
        dt.isoformat(sep=" ", timespec="seconds"),
        fixed.isoformat(sep=" ", timespec="seconds"),
    )
    return fixed


def _roll_trigger_time_to_future(dt: datetime, now: datetime) -> datetime:
    """将早于当前的触发时刻滚到下一次合理未来时刻。

    - 同一天内时刻偏早（多为模型压成整点）：抬到刚过扫描门槛，避免错误滚到下一年同日。
    - 否则：递增公历年匹配「去年的月日」类输出。
    """
    threshold = now - _SKEW
    if dt >= threshold:
        return dt
    if dt.date() == now.date():
        bumped = max(dt, threshold + timedelta(seconds=1)).replace(microsecond=0)
        logger.info(
            "关怀抽取：同日 trigger 早于当前，已顺延时刻 %s → %s",
            dt.isoformat(sep=" ", timespec="seconds"),
            bumped.isoformat(sep=" ", timespec="seconds"),
        )
        return bumped
    y0 = now.year
    for y in range(y0, y0 + 6):
        try:
            cand = dt.replace(year=y)
        except ValueError:
            continue
        if cand >= threshold:
            logger.info(
                "关怀抽取：模型给出的时间早于当前，已顺延 %s → %s",
                dt.isoformat(sep=" ", timespec="seconds"),
                cand.isoformat(sep=" ", timespec="seconds"),
            )
            return cand
    return dt


def _normalize_trigger_type(raw: object) -> str | None:
    """将模型输出的 ``trigger_type`` 规范为四类之一。

    **仅**接受与白名单一致的中文四字（可去空白后匹配），或少数完整英文等价词。
    不属于四类、胡填、无法归类 → 返回 ``None``，上层**跳过入库**（等价于 trigger_type 为空、不启用本条关怀）。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    compact = re.sub(r"[\s\u3000]+", "", s)
    if compact in _ALLOWED_TYPES:
        return compact

    fw = "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
    hw = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    low = s.translate(str.maketrans(fw, hw)).casefold().strip()
    parts = [p for p in re.split(r"[\s\u3000,_-]+", low) if p]
    eng = re.sub(r"[\s\u3000_-]+", "", low)
    exam_kw = frozenset({"exam", "test", "quiz", "midterm", "final", "interview"})

    if parts == ["birthday"] or eng == "birthday":
        return "生日"
    if parts == ["anniversary"] or eng == "anniversary":
        return "纪念日"
    if exam_kw.intersection(parts) or eng in exam_kw:
        return "考试"
    if (
        eng in ("dailycare", "routinecare", "dailyroutine")
        or ("daily" in parts and "care" in parts)
        or ("routine" in parts and "care" in parts)
    ):
        return "日常关怀"
    return None


def _call_extract_llm(
    user_input: str,
    ai_reply: str,
    *,
    context_prefix: str = "",
    reference_now: datetime | None = None,
    server_now: datetime | None = None,
    offset_hints: str = "",
) -> dict[str, Any] | None:
    """``server_now`` 由 Unix 时间戳换算的本地时刻（与调度扫描一致）；``reference_now`` 为相对语义基准（多为设备上报）。"""
    srv = server_now if server_now is not None else _now_local_from_timestamp()
    ref = reference_now if reference_now is not None else srv
    oh = (offset_hints or "").strip()
    hint_block = ""
    if oh:
        hint_block = (
            "\n【相对时间换算结果（服务器已按【对话语境参考时间】计算，须直接使用，禁止自行重算或取整到整点）】\n"
            f"{oh}\n"
        )
    if oh:
        derive_part = (
            "凡是上表中列出且与本条提醒对应的相对说法，trigger_time **必须**采用该行的绝对时间（含分秒）。"
            "未列入表内的相对时刻仍须你根据【对话语境参考时间】自行推算（不要用占位日期搪塞）。"
            "「下个月」等都要换算成具体公历日期。\n"
        )
    else:
        derive_part = (
            "你必须**自行完成全部时刻推算**（不要用占位日期搪塞）。相对说法一律以【对话语境参考时间】为起点换算。"
            "「明天」「后天」「下个月」等都要换算成具体公历日期。\n"
        )
    short_offset_rule = (
        "若约定「几分钟后」「几小时后」等短时偏移，必须在参考时间上做完加减法，写出对应的具体日期时间（至少到分钟更稳妥，"
        "但若模型只愿给到整点也可接受 ``…THH``）。完整 ISO 亦可。\n"
        if not oh
        else "已列入换算表的短时偏移必须写出表内完整日期时间（至少到分钟，**禁止**只给到小时粒度）。"
        "其余相对说法同上：至少到分钟。完整 ISO 亦可。\n"
    )
    ref_line = (
        f"【服务器当前时间（Unix 时间戳换算为本机本地「年月日:时分秒」，后端判定到期与此对齐）】"
        f"{_format_datetime_for_extract_llm(srv)}\n"
        f"【对话语境参考时间（同上格式；附带用户设备 scene_time 时已解析对齐；否则与服务器当前时间相同）】"
        f"{_format_datetime_for_extract_llm(ref)}\n"
        f"{hint_block}"
        f"{derive_part}"
        "trigger_time 写到对话里承诺的精度即可（后端会补齐缺失的分秒）："
        "仅约定「某月」「某天」「某天上午/下午某时」时，可用 ``YYYY-MM``、``YYYY-MM-DD``、``YYYY-MM-DDTHH`` 等粗格式；"
        f"{short_offset_rule}"
        "公历年**默认与【对话语境参考时间】的自然年相同**；只有用户或助手明确说过「明年」「下一年」等才可顺延到下一年。\n"
        "禁止无依据写成参考年的下一年（例如参考为 2026 却写 2027）。\n\n"
    )
    system = (
        "你是信息抽取器。根据给定的一轮对话（用户与助手各一段），判断是否包含可以落实到具体日期或时间的"
        "将来事项，用于数字人稍后主动关怀。\n\n"
        "规则：\n"
        "1. **trigger_type**（键名固定为 trigger_type，不要用 type/kind 代替）：仅当判断属于下面 **四类之一** 时才输出该条，"
        "且值必须是 **生日、纪念日、考试、日常关怀** 四字原文之一（勿加空格或前后缀）。\n"
        "   若判断该事项 **不属于** 以上四类（例如「加班 deadline」「去医院复查」但说不清归哪类），则 **整条不要写入 reminders**，"
        "**禁止**输出 trigger_type 为空字符串或「其他」「待定」等占位项——服务端会将无法识别的类型一律视为 **不抽取**。\n"
        "2. 生日：明确提及生日日期（可按年重复）。\n"
        "3. 纪念日：恋爱、结婚、相识等重要纪念日。\n"
        "4. 考试：明确考试、面试、答辩等日期。\n"
        "5. 日常关怀：用户明确约定未来某日要做某事、需要被提醒或问候，且不属于以上三类。\n"
        "6. 若没有可信的具体日期/时间，或纯属泛泛闲聊、情绪倾诉而无日程，必须输出空列表。\n"
        "7. trigger_time 字符串格式见上文「用户消息」开头说明（可到月/日/时，不必强行写分秒）。\n"
        "   若文首列出【相对时间换算结果】且本条与之对应，**直接采用表中绝对时间**；否则须以【对话语境参考时间】为基准换算相对时间。\n"
        "   对照【服务器当前时间】核对是否合理，禁止凭空年份。\n"
        "   绝对日期：用户说「今年 6 月 15 日」须用参考时间所在自然年；未提年份时用「即将到来」的那一次日期。\n"
        "8. trigger_content 填写「情景详细描述」：客观记录当时约定的时间点、事件、用户情绪与关键事实（若干句），"
        "用于系统在**触发当日**再结合该轮对话记录当场生成最终关怀话术；**不要**写成已经对用户念出口的台词式问候。"
        "描述须与人设、长期记忆摘要及瞬时记忆一致，勿编造矛盾事实。\n\n"
        '只输出一个 JSON 对象，格式严格为：{"reminders":[{"trigger_type":"...","trigger_time":"...","trigger_content":"..."}]} ；其中 trigger_content 为情景详细描述；'
    )
    prefix = (context_prefix or "").strip()
    dialogue = f"【本轮对话】\n【用户】\n{user_input}\n\n【助手】\n{ai_reply}"
    user_block = ref_line + (f"{prefix}\n\n{dialogue}" if prefix else dialogue)
    opts: dict[str, Any] = {"num_predict": 2048, "temperature": 0.05, "format": "json"}
    cli = _ollama_client()
    model = _model_name()
    try:
        r = cli.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_block},
            ],
            options=opts,
        )
    except Exception as e:
        logger.warning("关怀抽取 LLM 调用失败 model=%s: %s", model, e)
        try:
            del opts["format"]
            r = cli.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_block},
                ],
                options=opts,
            )
        except Exception as e2:
            logger.warning("关怀抽取 LLM 重试失败: %s", e2)
            return None
    raw = _ollama_message_content(r)
    logger.debug(
        "关怀抽取模型原始输出: %r",
        (raw[:800] + ("…" if len(raw) > 800 else "")) if raw else raw,
    )
    parsed = _parse_llm_reminder_json(raw)
    if parsed is not None:
        return parsed

    tail = (raw or "").strip()
    if not tail:
        logger.debug("关怀抽取模型返回空正文 model=%s", model)
        return None

    logger.debug(
        "关怀抽取首次解析失败，尝试纠错轮 model=%s raw_prefix=%r",
        model,
        tail[:400],
    )
    fix_opts = {k: v for k, v in opts.items() if k != "format"}
    try:
        r2 = cli.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_block},
                {"role": "assistant", "content": tail[:8000]},
                {
                    "role": "user",
                    "content": (
                        "你的上一段输出无法被标准 JSON 解析。"
                        "请只输出一个 JSON 对象，不要 markdown、不要中文解释。"
                        '格式：{"reminders":[]}；多条时每项须含四类之一的 trigger_type、trigger_time、trigger_content；'
                        "**无法归类则省略该条，不要编造类型。**字符串内双引号必须转义为 \\\" 。"
                    ),
                },
            ],
            options=fix_opts,
        )
    except Exception as e:
        logger.warning("关怀抽取纠错轮调用失败: %s", e)
    else:
        raw2 = _ollama_message_content(r2)
        logger.debug(
            "关怀抽取纠错轮模型输出: %r",
            (raw2[:800] + ("…" if len(raw2) > 800 else "")) if raw2 else raw2,
        )
        parsed = _parse_llm_reminder_json(raw2)
        if parsed is not None:
            return parsed

    logger.info(
        "关怀抽取解析失败 model=%s raw_len=%s raw_prefix=%s",
        model,
        len(tail),
        tail[:480].replace("\r", " ").replace("\n", "\\n"),
    )
    return None


def _raw_trigger_type_from_item(it: dict[str, Any]) -> object:
    """读取 ``trigger_type``；兼容小模型把类别写在 ``type`` / ``kind`` 等键下。"""
    v = it.get("trigger_type")
    if v is not None and str(v).strip():
        return v
    for alt in ("type", "kind", "category", "reminder_type", "triggerType"):
        v2 = it.get(alt)
        if v2 is not None and str(v2).strip():
            logger.debug("关怀抽取：条目使用别名字段 %r 作为 trigger_type", alt)
            return v2
    return it.get("trigger_type")


def extract_and_persist_reminders(
    user_id: int,
    user_input: str,
    ai_reply: str,
    *,
    package_key: str | None = None,
    session_id: int | None = None,
    scene_time: str | None = None,
) -> int:
    """同步：调用抽取模型并插入 ``remind_trigger``；返回新增条数。

    ``package_key`` 用于拉取 long_memory 概要等抽取上下文；**入库绑定**仅为 ``session_id``（本轮
    ``chat_session``）。
    """
    if user_id < 1:
        return 0
    if not _enabled():
        logger.info(
            "关怀抽取未执行：已关闭 REMIND_EXTRACT_FROM_DIALOGUE user_id=%s",
            user_id,
        )
        return 0
    ui = (user_input or "").strip()
    ar = (ai_reply or "").strip()
    if len(ui) < 2 or len(ar) < 2:
        return 0

    pkg_norm, period_overview = _long_memory_overview_for_package(user_id, package_key)
    context_prefix = _build_remind_extract_context(user_id, pkg_norm, period_overview)

    server_now = _now_local_from_timestamp()
    parsed_scene = _parse_scene_time_string(scene_time)
    ref_now = parsed_scene if parsed_scene is not None else server_now
    offset_hints = _resolve_relative_offsets(ui, ref_now)

    obj = _call_extract_llm(
        ui,
        ar,
        context_prefix=context_prefix,
        reference_now=ref_now,
        server_now=server_now,
        offset_hints=offset_hints,
    )
    if not obj:
        logger.info(
            "关怀抽取无结果：LLM 返回不可解析或非 JSON user_id=%s model=%s",
            user_id,
            _model_name(),
        )
        return 0
    items = obj.get("reminders")
    if items is None:
        items = obj.get("items") or obj.get("triggers")
    if not isinstance(items, list):
        logger.info(
            "关怀抽取跳过：JSON 内 reminders 缺失或类型不对 user_id=%s keys=%s",
            user_id,
            list(obj.keys()),
        )
        return 0

    sid_bind = int(session_id) if session_id is not None and int(session_id) > 0 else None
    if sid_bind is not None:
        logger.info("关怀抽取将绑定 session_id=%s package_key=%s", sid_bind, pkg_norm)
    else:
        logger.warning(
            "关怀抽取无 session_id（将无法按单轮对话召回语境）user_id=%s pkg=%s",
            user_id,
            pkg_norm,
        )
    if not (period_overview or "").strip():
        logger.info(
            "关怀抽取：该用户+包尚无 long_memory 概要（抽取上下文较弱）user_id=%s pkg=%s",
            user_id,
            pkg_norm,
        )

    inserted = 0
    skipped_parse = 0
    skipped_past = 0
    skipped_bad_type = 0
    for it in items[:_MAX_REMINDERS_PER_TURN]:
        if not isinstance(it, dict):
            continue
        raw_type = _raw_trigger_type_from_item(it)
        tt = _normalize_trigger_type(raw_type)
        if not tt:
            skipped_bad_type += 1
            logger.info(
                "关怀抽取跳过：trigger_type 不属于四类或未识别（视为 LLM 判定跳过本条）raw=%r item_keys=%s",
                raw_type,
                sorted(it.keys()),
            )
            continue
        dt_raw = _parse_trigger_time(str(it.get("trigger_time") or ""))
        anchor = _relative_minute_anchor(ui, ref_now)
        if dt_raw is None:
            if anchor is None:
                skipped_parse += 1
                logger.debug("关怀抽取跳过：无法解析时间 %r", it.get("trigger_time"))
                continue
            dt_raw = anchor
        elif anchor is not None and abs((dt_raw - anchor).total_seconds()) > 120:
            logger.info(
                "关怀抽取：「N 分钟后」锚点与模型 trigger_time 偏差>120s，采用服务器锚点 model=%s anchor=%s",
                dt_raw,
                anchor,
            )
            dt_raw = anchor
        dt_raw = _clamp_model_year_to_reference(dt_raw, ref_now, ui, ar)
        dt = _roll_trigger_time_to_future(dt_raw, server_now)
        if dt < server_now - _SKEW:
            skipped_past += 1
            logger.debug("关怀抽取跳过：顺延后仍早于当前 %s", dt)
            continue
        content = str(
            it.get("trigger_content") or it.get("scenario_detail") or ""
        ).strip()
        if not content:
            continue
        if len(content) > _CONTENT_MAX:
            content = content[:_CONTENT_MAX]

        row = RemindTrigger(
            user_id=user_id,
            trigger_type=tt[:30],
            trigger_time=dt,
            session_id=sid_bind,
            trigger_content=content,
            is_triggered=0,
        )
        try:
            with connection_ctx(DbConfig.from_env()) as conn:
                RemindTriggerRepository.insert(conn, row)
            inserted += 1
        except Exception:
            logger.exception(
                "写入 remind_trigger 失败 user_id=%s type=%s time=%s",
                user_id,
                tt,
                dt,
            )
    logger.info(
        "关怀抽取本轮 user_id=%s model=%s 模型给出条目=%s 写入库=%s "
        "(跳过:无效类型=%s 时间解析=%s 已过期=%s)",
        user_id,
        _model_name(),
        len(items),
        inserted,
        skipped_bad_type,
        skipped_parse,
        skipped_past,
    )
    return inserted
