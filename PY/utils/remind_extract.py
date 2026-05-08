"""从单轮对话（用户输入 + 助手回复）抽取定时关怀条目，写入 ``remind_trigger``。

在 ``/ws/chat`` 每轮落库后由后台线程调用；可通过环境变量关闭或换模型。
"""

from __future__ import annotations

import json
import logging
import os
import re
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


def _parse_trigger_time(raw: str) -> datetime | None:
    """解析模型输出的触发时刻；支持粗粒度（仅需年月、年月日、或到小时），缺失部分按规则补齐。

    - ``YYYY-MM`` → 该月 1 日 09:00:00
    - ``YYYY-MM-DD``（无时刻）→ 当日 09:00:00
    - 有时刻仅到小时（如 ``2026-05-09T13``、``2026-05-09 13``）→ 该小时 00 分 00 秒
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


def _roll_trigger_time_to_future(dt: datetime, now: datetime) -> datetime:
    """抽取模型常把「6 月 15 日面试」写成去年或错误公历年；按同月同日同时刻滚到 ``now`` 之后的下一次。"""
    threshold = now - _SKEW
    if dt >= threshold:
        return dt
    y0 = now.year
    for y in range(y0, y0 + 6):
        try:
            cand = dt.replace(year=y)
        except ValueError:
            # 如 2 月 29 日落在非闰年
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
    """将模型输出的 ``trigger_type`` 规范为四字枚举之一；无法识别时返回 ``None``（由上层决定是否降级）。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s in _ALLOWED_TYPES:
        return s
    # 全角拉丁字母 → 半角，便于匹配英文枚举
    fw = "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
    hw = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    s_lat = s.translate(str.maketrans(fw, hw))
    low = s_lat.casefold()

    if "birthday" in low:
        return "生日"
    if "anniversary" in low:
        return "纪念日"
    if any(
        k in low for k in ("exam", "quiz", "midterm", "final", "interview")
    ):
        return "考试"
    if "测试" in s:
        return "考试"

    if "生日" in s or "生辰" in s or "诞辰" in s or "庆生" in s:
        return "生日"
    if "纪念日" in s or "周年" in s or "婚庆" in s:
        return "纪念日"
    if (
        "考试" in s
        or "考研" in s
        or "期末" in s
        or "高考" in s
        or "面试" in s
        or "答辩" in s
        or "复试" in s
        or "笔试" in s
    ):
        return "考试"
    if (
        "关怀" in s
        or "提醒" in s
        or "问候" in s
        or "祝福" in s
        or "祝贺" in s
        or "定时" in s
        or "预约" in s
    ):
        return "日常关怀"
    if any(k in low for k in ("daily", "reminder", "routine", "schedule")):
        return "日常关怀"
    return None


def _call_extract_llm(
    user_input: str,
    ai_reply: str,
    *,
    context_prefix: str = "",
    reference_now: datetime | None = None,
    server_now: datetime | None = None,
) -> dict[str, Any] | None:
    """``server_now`` 为运行进程本地系统时间（与调度扫描一致）；``reference_now`` 为相对语义基准（多为设备上报）。"""
    srv = server_now if server_now is not None else datetime.now()
    ref = reference_now if reference_now is not None else srv
    ts_fmt = "%Y-%m-%d %H:%M:%S"
    ref_line = (
        f"【服务器当前时间（系统本地时间，后端判定到期与此对齐）】{srv.strftime(ts_fmt)}\n"
        f"【对话语境参考时间】{ref.strftime(ts_fmt)}"
        " —— 当前消息附带用户设备时间时已与之对齐；否则与服务器当前时间相同。\n"
        "你必须**自行完成全部时刻推算**（不要用占位日期搪塞）。相对说法一律以【对话语境参考时间】为起点换算。"
        "「明天」「后天」「下个月」等都要换算成具体公历日期。\n"
        "trigger_time 写到对话里承诺的精度即可（后端会补齐缺失的分秒）："
        "仅约定「某月」「某天」「某天上午/下午某时」时，可用 ``YYYY-MM``、``YYYY-MM-DD``、``YYYY-MM-DDTHH`` 等粗格式；"
        "若约定「几分钟后」「几小时后」等短时偏移，必须在参考时间上做完加减法，写出对应的具体日期时间（至少到分钟更稳妥，"
        "但若模型只愿给到整点也可接受 ``…THH``）。完整 ISO 亦可。\n"
        "公历年**默认与【对话语境参考时间】的自然年相同**；只有用户或助手明确说过「明年」「下一年」等才可顺延到下一年。\n"
        "禁止无依据写成参考年的下一年（例如参考为 2026 却写 2027）。\n\n"
    )
    system = (
        "你是信息抽取器。根据给定的一轮对话（用户与助手各一段），判断是否包含可以落实到具体日期或时间的"
        "将来事项，用于数字人稍后主动关怀。\n\n"
        "规则：\n"
        "1. trigger_type 必须是以下四字之一：生日、纪念日、考试、日常关怀。\n"
        "2. 生日：明确提及生日日期（可按年重复）。\n"
        "3. 纪念日：恋爱、结婚、相识等重要纪念日。\n"
        "4. 考试：明确考试、面试、答辩等日期。\n"
        "5. 日常关怀：用户明确约定未来某日要做某事、需要被提醒或问候，且不属于以上三类。\n"
        "6. 若没有可信的具体日期/时间，或纯属泛泛闲聊、情绪倾诉而无日程，必须输出空列表。\n"
        "7. trigger_time 由你完整推算；字符串格式见上文「用户消息」开头说明（可到月/日/时，不必强行写分秒）。\n"
        "   **必须以【对话语境参考时间】为基准**换算相对时间（「几分钟后」= 参考时刻 + 若干分钟，写出结果时刻）。\n"
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
                        '格式：{"reminders":[]}；多条时数组内每项含 trigger_type、trigger_time、'
                        "trigger_content。字符串内双引号必须转义为 \\\" 。"
                    ),
                },
            ],
            options=fix_opts,
        )
    except Exception as e:
        logger.warning("关怀抽取纠错轮调用失败: %s", e)
    else:
        raw2 = _ollama_message_content(r2)
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

    server_now = datetime.now()
    parsed_scene = _parse_scene_time_string(scene_time)
    ref_now = parsed_scene if parsed_scene is not None else server_now

    obj = _call_extract_llm(
        ui,
        ar,
        context_prefix=context_prefix,
        reference_now=ref_now,
        server_now=server_now,
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
    skipped_type = 0
    for it in items[:_MAX_REMINDERS_PER_TURN]:
        if not isinstance(it, dict):
            continue
        raw_type = it.get("trigger_type")
        tt = _normalize_trigger_type(raw_type)
        if not tt:
            raw_s = str(raw_type or "").strip()
            if raw_s:
                tt = "日常关怀"
                logger.info(
                    "关怀抽取：trigger_type 无法归入四类，已降级为「日常关怀」入库 raw=%r",
                    raw_type,
                )
            else:
                skipped_type += 1
                logger.debug("关怀抽取跳过：trigger_type 为空")
                continue
        dt_raw = _parse_trigger_time(str(it.get("trigger_time") or ""))
        if dt_raw is None:
            skipped_parse += 1
            logger.debug("关怀抽取跳过：无法解析时间 %r", it.get("trigger_time"))
            continue
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
        "(跳过:类型=%s 时间解析=%s 已过期=%s)",
        user_id,
        _model_name(),
        len(items),
        inserted,
        skipped_type,
        skipped_parse,
        skipped_past,
    )
    return inserted
