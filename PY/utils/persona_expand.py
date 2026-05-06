"""根据简短关键词扩写人物设定与语气风格。

支持两种后端（``PERSONA_EXPAND_BACKEND``）：

- **ollama**（默认）：本地 Ollama，读 ``OLLAMA_HOST`` / ``OLLAMA_MODEL`` 等。
- **openai**：任意 **OpenAI 兼容** HTTPS 接口（联网），读 ``PERSONA_EXPAND_OPENAI_*``；
  未单独配置时可回落到已有的 ``DASHSCOPE_API_KEY`` + ``DASHSCOPE_API_BASE``（阿里云兼容模式）。

产出字段用途与库表一致：
- ``character_desc`` → 聊天 LLM system / MiMo user【人设】
- ``tone_style`` → MiMo user【语气】（截断至 50 字以匹配 DB）

相关环境变量见各 ``_chat_*`` 函数内注释。
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_DESC_MAX = 8000
_TONE_DB_MAX = 50


class PersonaExpandError(RuntimeError):
    """本地或联网 LLM 不可用、返回不可解析或字段为空。"""


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _chat_content_ollama(resp: Any) -> str:
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
    if isinstance(m, dict):
        return str(m.get("content") or "").strip()
    if m is not None:
        c = getattr(m, "content", None)
        return str(c).strip() if c is not None else ""
    return ""


def _extract_json_object(raw: str) -> dict[str, Any]:
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty")
    m = _JSON_BLOCK_RE.search(s)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    brace = s.find("{")
    if brace >= 0:
        depth = 0
        for i in range(brace, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[brace : i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
                    break
    raise ValueError("no json object")


def _expand_system_and_user(character_hint: str, tone_hint: str) -> tuple[str, str]:
    ch = (character_hint or "").strip()
    th = (tone_hint or "").strip()
    system = (
        "你是中文虚拟角色人设编辑。用户会给出「角色」与「语气」的简短关键词（可能各只有几个字）。"
        "请扩展为可直接用于角色扮演对话的内容：不要编造与关键词明显矛盾的形象。"
        "必须只输出一个 JSON 对象，不要 Markdown、不要前后解释。"
        '键名固定为 "character_desc" 与 "tone_style"。'
        "character_desc 须为中文连贯正文（约 320～950 字），且必须包含两块内容，中间空一行分隔："
        "第一块写身份、性格、处事方式与说话习惯；"
        "第二块必须以单独一行「背景故事」开头（四字后换行），接着写与关键词一致的简短过往、关键经历或在情境中的位置（约 120～320 字），"
        "要有可演绎的具体情节或关系锚点，避免与第一块重复堆砌形容词，勿写无关冗长史诗。"
        "全文禁止使用【人设】【语气】【场景】等方括号标签（宿主会在 MiMo user 侧自动包一层【人设】/【语气】）。"
        f"tone_style 为一句语气与表达指导，不超过 {_TONE_DB_MAX} 个字符。"
    )
    user_msg = (
        "【角色关键词】\n"
        + (ch if ch else "（未提供，请结合语气关键词推断最小自洽人设）")
        + "\n\n【语气关键词】\n"
        + (th if th else "（未提供，请结合角色关键词推断常见说话方式）")
        + '\n\n输出示例：{"character_desc":"……","tone_style":"……"}'
    )
    return system, user_msg


def _expand_backend() -> str:
    raw = (os.getenv("PERSONA_EXPAND_BACKEND") or "ollama").strip().lower()
    if raw in ("openai", "online", "remote", "http", "https"):
        return "openai"
    return "ollama"


def _openai_resolve_credentials() -> tuple[str, str, str]:
    """返回 (api_key, base_url 不含末尾斜杠, model_name)。"""
    api_key = (
        (os.getenv("PERSONA_EXPAND_OPENAI_API_KEY") or "").strip()
        or (os.getenv("OPENAI_API_KEY") or "").strip()
        or (os.getenv("DASHSCOPE_API_KEY") or "").strip()
    )
    base = (
        (os.getenv("PERSONA_EXPAND_OPENAI_BASE_URL") or "").strip().rstrip("/")
        or (os.getenv("OPENAI_BASE_URL") or "").strip().rstrip("/")
        or (os.getenv("DASHSCOPE_API_BASE") or "").strip().rstrip("/")
    )
    model = (
        (os.getenv("PERSONA_EXPAND_OPENAI_MODEL") or "").strip()
        or (os.getenv("PERSONA_EXPAND_MODEL") or "").strip()
    )
    if not model and base:
        bl = base.lower()
        if "dashscope" in bl or "aliyuncs.com" in bl:
            model = (os.getenv("PERSONA_EXPAND_DASHSCOPE_MODEL") or "qwen-plus").strip()
    if not api_key:
        raise PersonaExpandError(
            "联网扩写需配置 API Key：PERSONA_EXPAND_OPENAI_API_KEY、OPENAI_API_KEY 或 DASHSCOPE_API_KEY 其一"
        )
    if not base:
        raise PersonaExpandError(
            "联网扩写需配置 Base URL：PERSONA_EXPAND_OPENAI_BASE_URL、OPENAI_BASE_URL 或 DASHSCOPE_API_BASE 其一"
            "（须为 OpenAI 兼容根路径，例如 https://dashscope.aliyuncs.com/compatible-mode/v1）"
        )
    if not model:
        raise PersonaExpandError(
            "联网扩写需配置模型名：PERSONA_EXPAND_OPENAI_MODEL 或 PERSONA_EXPAND_MODEL"
        )
    return api_key, base, model


def _chat_openai_compatible(system: str, user_msg: str) -> str:
    """POST ``{base}/chat/completions``，Bearer 鉴权。"""
    api_key, base, model = _openai_resolve_credentials()
    try:
        max_tokens = int((os.getenv("PERSONA_EXPAND_MAX_TOKENS") or "2048").strip() or "2048")
    except ValueError:
        max_tokens = 2048
    max_tokens = max(256, min(8192, max_tokens))
    try:
        timeout = float((os.getenv("PERSONA_EXPAND_HTTP_TIMEOUT") or "120").strip() or "120")
    except ValueError:
        timeout = 120.0
    timeout = max(15.0, min(600.0, timeout))

    url = f"{base}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.55,
        "max_tokens": max_tokens,
    }
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
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        logger.warning("人设扩写 HTTP %s: %s", e.code, err_body[:800])
        detail = err_body
        try:
            err_json = json.loads(err_body)
            if isinstance(err_json, dict):
                err_obj = err_json.get("error")
                if isinstance(err_obj, dict):
                    detail = str(err_obj.get("message") or err_body)
                elif isinstance(err_obj, str):
                    detail = err_obj
        except json.JSONDecodeError:
            pass
        raise PersonaExpandError(f"联网扩写接口错误 HTTP {e.code}：{detail[:500]}") from None
    except OSError as e:
        logger.exception("人设扩写网络请求失败 url=%s", url)
        raise PersonaExpandError(f"联网扩写网络失败：{e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise PersonaExpandError("联网扩写返回非 JSON") from None

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise PersonaExpandError("联网扩写返回无 choices")
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = ""
    if isinstance(msg, dict):
        content = str(msg.get("content") or "").strip()
    if not content:
        raise PersonaExpandError("联网扩写返回空正文")
    return content


def _chat_ollama(system: str, user_msg: str) -> str:
    try:
        import ollama
    except ImportError as e:
        raise PersonaExpandError("未安装 ollama 包，请先 pip install ollama") from e

    host = (os.getenv("OLLAMA_HOST") or "http://127.0.0.1:11434").strip()
    model = (os.getenv("PERSONA_EXPAND_MODEL") or os.getenv("OLLAMA_MODEL") or "qwen2.5:3b").strip()
    try:
        num_predict = int((os.getenv("PERSONA_EXPAND_NUM_PREDICT") or "1600").strip() or "1600")
    except ValueError:
        num_predict = 1600
    num_predict = max(256, min(4096, num_predict))

    client = ollama.Client(host=host)
    try:
        resp = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            options={"temperature": 0.55, "num_predict": num_predict},
        )
    except Exception as e:
        logger.exception("人设扩写 Ollama 调用失败 model=%s", model)
        raise PersonaExpandError(f"人设扩写失败：{e}") from e

    text = _chat_content_ollama(resp)
    if not text:
        raise PersonaExpandError("人设扩写返回空内容")
    return text


def expand_persona_from_hints(character_hint: str, tone_hint: str) -> tuple[str, str]:
    """返回 ``(character_desc, tone_style)``；语气风格保证不超过 ``_TONE_DB_MAX``。"""
    ch = (character_hint or "").strip()
    th = (tone_hint or "").strip()
    if not ch and not th:
        raise PersonaExpandError("角色与语气关键词至少填其一")

    system, user_msg = _expand_system_and_user(ch, th)
    backend = _expand_backend()
    if backend == "openai":
        text = _chat_openai_compatible(system, user_msg)
    else:
        text = _chat_ollama(system, user_msg)

    try:
        obj = _extract_json_object(text)
    except ValueError:
        logger.warning("人设扩写 JSON 解析失败，原文片段：%s", text[:500])
        raise PersonaExpandError("人设扩写返回无法解析的 JSON") from None

    desc = str(obj.get("character_desc") or "").strip()
    tone = str(obj.get("tone_style") or "").strip()
    if not desc:
        raise PersonaExpandError("扩写结果缺少 character_desc")
    if not tone:
        raise PersonaExpandError("扩写结果缺少 tone_style")

    if len(desc) > _DESC_MAX:
        desc = desc[: _DESC_MAX - 1].rstrip() + "…"
    if len(tone) > _TONE_DB_MAX:
        tone = tone[:_TONE_DB_MAX]

    return desc, tone
