"""从单轮对话（用户输入 + 助手回复）抽取定时关怀条目，写入 ``remind_trigger``。

在 ``/ws/chat`` **每轮**落库后异步调用（``remind_extract_turn_should_run`` 固定为每轮触发，不按 N 轮节流）。
可通过 ``REMIND_EXTRACT_FROM_DIALOGUE`` 开关总功能。
若同时配置 ``PERSONA_EXPAND_OPENAI_BASE_URL``、``PERSONA_EXPAND_OPENAI_API_KEY``、``PERSONA_EXPAND_OPENAI_MODEL``（与人设扩写云端同源），则关怀抽取 **优先** 走该 OpenAI 兼容 ``/chat/completions``，否则回退本地 Ollama（``REMIND_EXTRACT_MODEL`` / ``OLLAMA_MODEL``）。
**抽取材料**仅为本轮用户消息与助手回复；注入 LLM 的上下文 **不含** Redis 瞬时记忆、短期记忆，也 **不含** MySQL 长期摘要——与日程相关的依据只能来自当前这一轮的两段正文。
人设绑定与人设正文仅用于角色指称一致，**不得**据此编造本轮未出现的日程。
``trigger_content`` 的事实必须全部出自【本轮对话】。
**仅** ``trigger_type`` 为 **生日、纪念日、考试** 之一的条目才会入库；模型若显式标成「其他」等占位类别（见 ``_OTHER_TRIGGER_LABELS_*``）或与三类不符的输出一律 **不入库**。
抽取 LLM 所见的「当前时刻」先取 **Unix 时间戳** 再换算为本机本地 **``年月日:时分秒``** 字符串（与相对时间换算表同一格式），再写入 User 消息文首，便于模型推算 ``trigger_time``。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any

import ollama
import pymysql.err

from live2d_db.connection import connection_ctx
from live2d_db.db_config import DbConfig
from live2d_db.entities import Persona, RemindTrigger
from live2d_db.package_key_util import normalize_package_key
from live2d_db.repositories import PersonaRepository, RemindTriggerRepository

logger = logging.getLogger(__name__)

_ALLOWED_TYPES = frozenset({"生日", "纪念日", "考试"})
_OTHER_TRIGGER_LABELS_CN = frozenset(
    {
        "其他",
        "其它",
        "其他类",
        "其它类",
        "其他类型",
        "其它类型",
        "无类",
        "不属于",
        "不适用",
        "无法归类",
        "待定",
    }
)
_OTHER_TRIGGER_LABELS_EN = frozenset(
    {
        "other",
        "others",
        "misc",
        "miscellaneous",
        "unknown",
        "none",
        "na",
        "n/a",
        "othercategory",
        "other_category",
    }
)
_MAX_REMINDERS_PER_TURN = 8
_CONTENT_MAX = 4000
_SKEW = timedelta(minutes=2)
_REMIND_CTX_PERSONA_MAX = 3500


def _now_local_from_timestamp() -> datetime:
    """当前时刻：取 Unix 时间戳再转为本机本地 naive datetime（秒精度），与调度到期判定一致。"""
    return datetime.fromtimestamp(time.time()).replace(microsecond=0)


def _format_datetime_for_extract_llm(dt: datetime) -> str:
    """传给抽取 LLM 的公历时间：``年月日:时分秒``（例如 ``2026年05月09日:14:30:45``），便于模型对齐中文语境。"""
    return dt.replace(microsecond=0).strftime("%Y年%m月%d日:%H:%M:%S")


def remind_extract_turn_should_run(
    user_id: int,
    package_key: str | None,
    ws_session_key: str,
) -> bool:
    """本轮结束后是否应触发关怀抽取：固定为每轮执行（保留参数供调用方兼容）。"""
    _ = (package_key, ws_session_key)
    return user_id >= 1


def _enabled() -> bool:
    return os.getenv("REMIND_EXTRACT_FROM_DIALOGUE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _env_ollama_model_or_none(raw: str | None) -> str | None:
    """解析环境变量中的模型名；空串、纯空白、或以 # 开头（误把注释写进 .env）视为未配置。"""
    if raw is None:
        return None
    s = raw.strip()
    if not s or s.startswith("#"):
        return None
    return s


def _persona_expand_openai_triplet() -> tuple[str, str, str] | None:
    """人设扩写云端同款三项均已配置时返回 ``(api_key, base_url, model)``，否则 ``None``。"""
    api_key = (os.getenv("PERSONA_EXPAND_OPENAI_API_KEY") or "").strip()
    base = (os.getenv("PERSONA_EXPAND_OPENAI_BASE_URL") or "").strip().rstrip("/")
    model = (os.getenv("PERSONA_EXPAND_OPENAI_MODEL") or "").strip()
    if api_key and base and model:
        return api_key, base, model
    return None


def _remind_extract_http_timeout_sec() -> float:
    for key in ("REMIND_EXTRACT_HTTP_TIMEOUT", "PERSONA_EXPAND_HTTP_TIMEOUT"):
        raw = (os.getenv(key) or "").strip()
        if raw:
            try:
                return max(15.0, min(600.0, float(raw)))
            except ValueError:
                pass
    return 120.0


def _remind_extract_openai_max_tokens() -> int:
    raw = (os.getenv("REMIND_EXTRACT_MAX_TOKENS") or "2048").strip()
    try:
        return max(256, min(8192, int(raw or "2048")))
    except ValueError:
        return 2048


def _openai_chat_completion_extract(
    api_key: str,
    base: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> str:
    """POST ``{base}/chat/completions``；优先带 ``response_format=json_object``，400 时回退不带。"""
    url = f"{base}/chat/completions"
    base_payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    tries: list[dict[str, Any]] = [
        {**base_payload, "response_format": {"type": "json_object"}},
        base_payload,
    ]

    last_http: urllib.error.HTTPError | None = None
    for payload in tries:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_txt = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            last_http = e
            if e.code == 400 and "response_format" in payload:
                logger.debug(
                    "关怀抽取云端忽略 response_format 并重试 HTTP %s: %s",
                    e.code,
                    err_body[:400],
                )
                continue
            logger.warning("关怀抽取云端 HTTP %s: %s", e.code, err_body[:800])
            raise
        except OSError as e:
            logger.warning("关怀抽取云端请求失败 url=%s: %s", url, e)
            raise

        try:
            data = json.loads(raw_txt)
        except json.JSONDecodeError:
            logger.warning("关怀抽取云端返回非 JSON")
            return ""

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            logger.warning("关怀抽取云端返回无 choices")
            return ""
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = ""
        if isinstance(msg, dict):
            content = str(msg.get("content") or "").strip()
        return content

    if last_http:
        raise last_http
    return ""


def _model_name() -> str:
    trip = _persona_expand_openai_triplet()
    if trip:
        return trip[2]
    for key in ("REMIND_EXTRACT_MODEL", "OLLAMA_MODEL"):
        m = _env_ollama_model_or_none(os.getenv(key))
        if m:
            return m
    return "qwen2.5:3b"


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


def _mysql_persona_row_for_package(user_id: int, package_key: str) -> Persona | None:
    """从 MySQL ``persona`` 表解析当前用户 + 模型包的人设行（与 ``/ws/chat``、关怀投递同源）。"""
    if user_id < 1:
        return None
    pkg = (package_key or "").strip()
    if not pkg:
        return None
    try:
        with connection_ctx(DbConfig.from_env()) as conn:
            return PersonaRepository.resolve_persona_for_package(conn, user_id, pkg)
    except pymysql.err.ProgrammingError as e:
        code = e.args[0] if e.args else None
        if code in (1146, 1054):
            logger.warning(
                "关怀抽取读取人设跳过（persona 表或列不可用 errno=%s），仍可仅用本轮对话生成文案",
                code,
            )
            return None
        logger.exception("关怀抽取读取人设失败 user_id=%s package=%s", user_id, pkg)
        return None
    except Exception:
        logger.exception("关怀抽取读取人设失败 user_id=%s package=%s", user_id, pkg)
        return None


def _format_persona_block_from_row(row: Persona | None) -> str:
    """将 persona 行格式化为抽取 LLM 可读块（语气 + 角色设定）。"""
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


def _build_remind_extract_context(
    user_id: int, pkg_norm: str
) -> str:
    blocks: list[str] = []
    persona_row = _mysql_persona_row_for_package(user_id, pkg_norm)
    bind_lines = [
        "【MySQL 当前人设绑定】（以下为服务端查询 ``persona`` 表结果，抽取时须以此为「当前数字人」依据）",
        f"user_id={user_id}；规范化 package_key={pkg_norm}",
    ]
    if persona_row is None:
        bind_lines.append(
            "查询结果：该用户与本包无绑定人设行，或 persona 表不可用；"
            "【语气风格】【角色设定】不会出现；trigger_content 中指涉角色时请仅用本轮用户明确使用的称呼，勿编造专名。"
        )
    else:
        pid = persona_row.persona_id
        pk_db = (persona_row.package_key or "").strip() or pkg_norm
        bind_lines.append(
            f"查询结果：已命中 persona_id={pid}；绑定 package_key={pk_db}。"
            "紧随其后的【语气风格】【角色设定】均来自该 MySQL 行，与本轮「当前模型角色回复」为同一人。"
        )
    blocks.append("\n".join(bind_lines))
    persona = _format_persona_block_from_row(persona_row)
    if persona:
        blocks.append(f"【当前模型人设】{persona}")
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


def _raw_trigger_type_is_explicit_other(raw: object) -> bool:
    """模型把类别写成「其他」等占位 → **不入库**（与空白、胡填区分，便于日志）。"""
    if raw is None:
        return False
    s = str(raw).strip()
    if not s:
        return False
    compact = re.sub(r"[\s\u3000]+", "", s)
    if compact in _OTHER_TRIGGER_LABELS_CN:
        return True

    fw = "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
    hw = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    low = s.translate(str.maketrans(fw, hw)).casefold().strip()
    eng = re.sub(r"[\s\u3000_-]+", "", low)
    if eng in _OTHER_TRIGGER_LABELS_EN or low in _OTHER_TRIGGER_LABELS_EN:
        return True
    return False


def _normalize_trigger_type(raw: object) -> str | None:
    """将模型输出的 ``trigger_type`` 规范为三类之一（生日 / 纪念日 / 考试）。

    **仅**接受与白名单一致的中文（可去空白后匹配），或少数完整英文等价词。
    不属于三类、胡填、无法归类 → 返回 ``None``，上层**跳过入库**。
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
        "你是信息抽取器。根据给定的一轮对话（用户与「当前模型角色回复」各一段），判断是否包含可以落实到具体日期或时间的"
        "将来事项，用于数字人稍后主动关怀。\n"
        "**禁止**依据瞬时记忆、短期记忆、长期摘要或其它多轮历史推断日程；``trigger_type`` / ``trigger_time`` / ``trigger_content`` 所依据的事实 **只能**出现在下文【本轮对话】两段中。"
        "【当前模型人设】块仅用于称呼与人设一致，**禁止**用其捏造本轮未口述的日期或事件。\n\n"
        "规则：\n"
        "1. **trigger_type**（键名固定为 trigger_type，不要用 type/kind 代替）：仅当判断属于下面 **三类之一** 时才输出该条，"
        "且值必须是 **生日、纪念日、考试** 原文之一（生日、纪念日为两字，考试为两字；勿加空格或前后缀）。\n"
        "   若事项可归为「将来要做的琐事提醒」但 **不属于** 生日、纪念日、考试（例如普通约会、去医院复查、加班截止），则 **不要写入 reminders**——"
        "服务端 **仅接受这三类**，其它一律不入库。\n"
        "   若你只能判断为 **「其他」**（不归生日、纪念日、考试），必须输出 **空列表** ``{\"reminders\":[]}``，"
        "**禁止**输出一条 ``trigger_type`` 为「其他」「待定」等的对象——服务端会 **丢弃** 该类条目、**不入库**。\n"
        "   **禁止**输出 trigger_type 为空字符串或「日常关怀」等——无法识别的类型一律 **不抽取**。\n"
        "2. 生日：明确提及生日日期（可按年重复）。\n"
        "3. 纪念日：恋爱、结婚、相识等重要纪念日。\n"
        "4. 考试：明确考试、面试、答辩等日期。\n"
        "5. 若没有可信的具体日期/时间，或纯属泛泛闲聊、情绪倾诉而无日程，必须输出空列表。\n"
        "6. trigger_time 字符串格式见上文「用户消息」开头说明（可到月/日/时，不必强行写分秒）。\n"
        "   若文首列出【相对时间换算结果】且本条与之对应，**直接采用表中绝对时间**；否则须以【对话语境参考时间】为基准换算相对时间。\n"
        "   对照【服务器当前时间】核对是否合理，禁止凭空年份。\n"
        "   绝对日期：用户说「今年 6 月 15 日」须用参考时间所在自然年；未提年份时用「即将到来」的那一次日期。\n"
        "7. trigger_content 填写「情景概要」（入库备忘，非对用户终稿）：**事实来源仅限【本轮对话】**（用户一段与「当前模型角色回复」一段）；"
        "**不得**写入仅存在于人设块或多轮记忆中的细节。"
        "用若干句客观叙述整合为一条连贯概要，须显式覆盖下列维度（缺一不可；未知项须写明占位语，禁止空白）："
        "**用户时间**（对话语境下的时刻、日期或时段）；**用户地点**（本轮未提及则写「地点未提及」）；"
        "**角色**（数字人称呼遵守下文角色指称）；**事件**（约定或触发事由）；**氛围**（用户情绪、语气或场景气氛，材料不足可写「氛围未凸显」）。"
        "用于系统在**触发当日**再结合关联对话当场生成最终关怀话术；**不要**写成已经对用户念出口的台词式问候。"
        "描述须与人设及本轮对话一致，勿编造矛盾事实。\n"
        "   **角色指称**：提到本轮对话里的数字人时，须与上文【当前模型人设】（角色设定、语气）或用户本轮明确使用的**专名**一致；"
        "**禁止**单独写「助手」「AI」「虚拟助手」等泛指而不交代具体是谁（无人设专名时可用「当前对话中的 Live2D 角色」一次交代）。\n"
        "   若用户口头里的「助手」与角色专名实为同一人（与人设、本轮「当前模型角色回复」一致），须在描述中**合并为一种称呼**，"
        "**禁止**写成「西奥和助手」「喜欢某某和助手」这类易被理解成**两个不同主体**的并列。\n\n"
        '只输出一个 JSON 对象，格式严格为：{"reminders":[{"trigger_type":"...","trigger_time":"...","trigger_content":"..."}]} '
        "；其中 trigger_content 为情景概要（须含用户时间、用户地点、角色、事件、氛围五维，未知项用占位语）。"
        "**仅** trigger_type 为 **生日、纪念日、考试** 的条目会由服务端入库。"
    )
    prefix = (context_prefix or "").strip()
    dialogue = (
        f"【本轮对话】\n【用户】\n{user_input}\n\n"
        f"【当前模型角色回复】（与本包【当前模型人设】为同一人，非第三者）\n{ai_reply}"
    )
    user_block = ref_line + (f"{prefix}\n\n{dialogue}" if prefix else dialogue)
    messages_primary: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_block},
    ]
    fix_followup = (
        "你的上一段输出无法被标准 JSON 解析。"
        "请只输出一个 JSON 对象，不要 markdown、不要中文解释。"
        '格式：{"reminders":[]}；多条时每项须含 trigger_type（仅允许 生日、纪念日、考试）、trigger_time、trigger_content；'
        "**仅能归为「其他」时不要输出条目，给空 reminders。**字符串内双引号必须转义为 \\\" 。"
    )
    model = _model_name()
    trip = _persona_expand_openai_triplet()
    opts: dict[str, Any] = {"num_predict": 2048, "temperature": 0.05, "format": "json"}
    cli: ollama.Client | None = None
    raw = ""

    if trip:
        api_key, base, om = trip
        timeout = _remind_extract_http_timeout_sec()
        max_tok = _remind_extract_openai_max_tokens()
        logger.debug(
            "关怀抽取使用 PERSONA_EXPAND_OPENAI_* 云端接口 base=%s model=%s",
            base,
            om,
        )
        try:
            raw = _openai_chat_completion_extract(
                api_key,
                base,
                om,
                messages_primary,
                temperature=0.05,
                max_tokens=max_tok,
                timeout=timeout,
            )
        except Exception as e:
            logger.warning("关怀抽取云端调用失败 model=%s: %s", model, e)
            return None
    else:
        cli = _ollama_client()
        try:
            r = cli.chat(
                model=model,
                messages=messages_primary,
                options=opts,
            )
        except Exception as e:
            logger.warning("关怀抽取 LLM 调用失败 model=%s: %s", model, e)
            try:
                del opts["format"]
                r = cli.chat(
                    model=model,
                    messages=messages_primary,
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

    if trip:
        api_key, base, om = trip
        timeout = _remind_extract_http_timeout_sec()
        max_tok = _remind_extract_openai_max_tokens()
        fix_messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_block},
            {"role": "assistant", "content": tail[:8000]},
            {"role": "user", "content": fix_followup},
        ]
        try:
            raw2 = _openai_chat_completion_extract(
                api_key,
                base,
                om,
                fix_messages,
                temperature=0.05,
                max_tokens=max_tok,
                timeout=timeout,
            )
        except Exception as e:
            logger.warning("关怀抽取云端纠错轮调用失败: %s", e)
            raw2 = ""
        else:
            logger.debug(
                "关怀抽取纠错轮模型输出: %r",
                (raw2[:800] + ("…" if len(raw2) > 800 else "")) if raw2 else raw2,
            )
            parsed = _parse_llm_reminder_json(raw2)
            if parsed is not None:
                return parsed
    else:
        assert cli is not None
        fix_opts = {k: v for k, v in opts.items() if k != "format"}
        try:
            r2 = cli.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_block},
                    {"role": "assistant", "content": tail[:8000]},
                    {"role": "user", "content": fix_followup},
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

    ``package_key`` 用于规范化包键并拉取人设；抽取 LLM **不**读取长期/瞬时/短期记忆，日程依据仅为 ``user_input`` 与 ``ai_reply``。
    **入库绑定**仅为 ``session_id``（本轮 ``chat_session``）。
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

    pkg_norm = normalize_package_key((package_key or "").strip() or None, fallback="default")
    context_prefix = _build_remind_extract_context(user_id, pkg_norm)

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
    inserted = 0
    skipped_parse = 0
    skipped_past = 0
    skipped_bad_type = 0
    skipped_explicit_other = 0
    for it in items[:_MAX_REMINDERS_PER_TURN]:
        if not isinstance(it, dict):
            continue
        raw_type = _raw_trigger_type_from_item(it)
        if _raw_trigger_type_is_explicit_other(raw_type):
            skipped_explicit_other += 1
            logger.debug(
                "关怀抽取跳过：LLM 归类为「其他」不入库 raw=%r item_keys=%s",
                raw_type,
                sorted(it.keys()),
            )
            continue
        tt = _normalize_trigger_type(raw_type)
        if not tt:
            skipped_bad_type += 1
            logger.info(
                "关怀抽取跳过：trigger_type 非生日/纪念日/考试或未识别 raw=%r item_keys=%s",
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
        "(跳过:其他=%s 非三类=%s 时间解析=%s 已过期=%s)",
        user_id,
        _model_name(),
        len(items),
        inserted,
        skipped_explicit_other,
        skipped_bad_type,
        skipped_parse,
        skipped_past,
    )
    return inserted
