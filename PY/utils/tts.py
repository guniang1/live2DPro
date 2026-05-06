"""语音合成：默认 GPT-SoVITS 本地 HTTP；可选小米 MiMo TTS（``TTS_PROVIDER=mimo``）。

MiMo V2.5：预置音色（``mimo-v2.5-tts``）与 **音频样本复刻**（``mimo-v2.5-tts-voiceclone``，
``audio.voice`` 为 ``data:audio/*;base64,...``）见官方说明
<https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/speech-synthesis-v2.5> 与技能脚本
<https://github.com/XiaomiMiMo/MiMo-Skills/blob/main/skills/mimo-v2-5-tts/scripts/mimo_tts_voiceclone.py>。

调试：``TTS_DEBUG=1`` 开启耗时、载荷规模与网络错误拆解日志（排查间歇读超时）。
音色克隆时可选缓存 **参考音** 的 Data URL（``MIMO_VOICE_DATAURL_CACHE``）；后端见
``MIMO_VOICE_DATAURL_CACHE_BACKEND``（``memory`` / ``redis`` / ``both``）。**从不**缓存合成 WAV。
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Generator

from utils.audio_refer import ffmpeg_convert_file_to_wav, is_standard_riff_wav

_ENV = Path(__file__).resolve().parent.parent / ".env"
try:
    from dotenv import load_dotenv

    load_dotenv(_ENV)
except ImportError:
    pass

logger = logging.getLogger(__name__)


def tts_debug_enabled() -> bool:
    v = (os.getenv("TTS_DEBUG") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def normalized_tts_provider() -> str:
    """``gpt_sovits``（默认）或 ``mimo``（小米 MiMo 云端）。"""
    p = (os.getenv("TTS_PROVIDER") or "gpt_sovits").strip().lower()
    if p in ("mimo", "xiaomi_mimo", "mimo_v2", "mimo-v2", "xiaomimimo"):
        return "mimo"
    return "gpt_sovits"


def mimo_tts_configured() -> bool:
    return bool((os.getenv("MIMO_API_KEY") or "").strip())


def _mimo_resolve_voice_sample_path(refer_runtime: dict | None) -> str | None:
    """返回用于音色克隆的本地音频路径（wav/mp3），无则走预置音色。"""
    env_p = (os.getenv("MIMO_VOICE_SAMPLE_PATH") or "").strip()
    candidates: list[str] = []
    if env_p:
        candidates.append(env_p)
    if refer_runtime:
        rw = refer_runtime.get("refer_wav_path")
        if isinstance(rw, str) and rw.strip():
            candidates.append(rw.strip())
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def _mimo_network_err_detail(exc: BaseException) -> str:
    """供 ``TTS_DEBUG``：拆解 URLError / OSError / IncompleteRead，不含密钥与正文。"""
    if isinstance(exc, http.client.IncompleteRead):
        plen = len(exc.partial) if exc.partial else 0
        return (
            f"IncompleteRead partial_bytes={plen} expected={exc.expected!r}"
        )
    parts: list[str] = [type(exc).__name__]
    if isinstance(exc, urllib.error.URLError):
        r = exc.reason
        parts.append(f"reason={type(r).__name__}")
        if isinstance(r, OSError):
            if r.errno is not None:
                parts.append(f"errno={r.errno}")
            if r.strerror:
                parts.append(f"strerror={r.strerror!r}")
            we = getattr(r, "winerror", None)
            if we is not None:
                parts.append(f"winerror={we}")
        elif r is not None:
            parts.append(f"str={r!r}")
    elif isinstance(exc, OSError):
        if exc.errno is not None:
            parts.append(f"errno={exc.errno}")
        if exc.strerror:
            parts.append(f"strerror={exc.strerror!r}")
        we = getattr(exc, "winerror", None)
        if we is not None:
            parts.append(f"winerror={we}")
    c = exc.__cause__
    if c is not None and c is not exc:
        parts.append(f"cause={type(c).__name__}")
    return " ".join(parts)


def _mimo_http_timeout_s(explicit: float | None) -> float:
    """单次 ``urlopen`` 超时（秒）：含连接与读响应；过大会让「挂起」浪费很久才重试。"""
    if explicit is not None:
        return max(10.0, min(600.0, float(explicit)))
    raw = (os.getenv("MIMO_TTS_TIMEOUT") or "").strip()
    if raw:
        try:
            return max(10.0, min(600.0, float(raw)))
        except ValueError:
            pass
    # 默认低于旧版 120s：偶发读超时后重试往往很快成功，缩短单次等待可改善首包体感
    return 60.0


def _mimo_likely_read_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, urllib.error.URLError):
        r = exc.reason
        if isinstance(r, TimeoutError):
            return True
        if "timed out" in str(exc).lower():
            return True
    return False


def _mimo_large_payload_timeout_hint(
    payload_bytes: int, timeout_s: float, exc: BaseException
) -> str:
    """读超时且请求体很大（多为克隆样本 Base64）时给人可读 hints。"""
    if not _mimo_likely_read_timeout(exc):
        return ""
    if payload_bytes < 400_000:
        return ""
    if timeout_s >= 120.0:
        return ""
    return (
        " | hint: 音色克隆单次 POST 约数百 KB，上行+排队偶发超过当前 timeout；"
        "可在 .env 提高 MIMO_TTS_TIMEOUT（如 120）"
    )


def _mimo_retry_sleep_for_http_error(
    e: urllib.error.HTTPError, attempt: int
) -> float | None:
    """429 / 部分 5xx 可重试：返回休眠秒数；其它客户端错误返回 None（应立即失败）。"""
    if e.code == 429:
        wait_s: float | None = None
        hdrs = getattr(e, "headers", None)
        if hdrs:
            ra = hdrs.get("Retry-After") or hdrs.get("retry-after")
            if ra:
                try:
                    wait_s = float(ra)
                except ValueError:
                    wait_s = None
        if wait_s is None:
            base = float(os.getenv("MIMO_TTS_429_BACKOFF_BASE") or "3.0")
            cap = float(os.getenv("MIMO_TTS_429_BACKOFF_CAP") or "60.0")
            mult = base * (2**attempt)
            jitter = random.uniform(0, min(3.0, max(0.5, mult * 0.15)))
            wait_s = min(cap, mult + jitter)
        return min(120.0, max(1.0, wait_s))
    if e.code in (500, 502, 503, 504):
        return min(25.0, 0.75 * (2**attempt))
    return None


def _mimo_refer_ffmpeg_convert_enabled() -> bool:
    v = (os.getenv("MIMO_REFER_FFMPEG_CONVERT") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _mimo_try_ffmpeg_refer_to_wav(path: str) -> bytes | None:
    """将任意 ffmpeg 可读格式转为 PCM WAV；失败返回 None。"""
    if not _mimo_refer_ffmpeg_convert_enabled():
        return None
    return ffmpeg_convert_file_to_wav(
        path,
        timeout_s=120.0,
        max_out_bytes=10 * 1024 * 1024,
    )


def _sniff_mimo_clone_sample_mime(data: bytes) -> str | None:
    """音色克隆官方仅接受 wav/mp3；返回 ``audio/wav`` / ``audio/mpeg``，其它容器返回标记字符串。"""
    if len(data) < 12:
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:3] == b"ID3" or (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    if data[:4] == b"fLaC":
        return "__reject_flac__"
    if data[:4] == b"OggS":
        return "__reject_ogg__"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "__reject_m4a__"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "__reject_webm__"
    return None


def _encode_mimo_voice_sample_file(path: str) -> str:
    """官方要求 ``data:{MIME_TYPE};base64,...``，MIME 仅 ``audio/wav`` 或 ``audio/mpeg``（mp3）。

    仓库里常见「扩展名 .wav 实为 flac/ogg/webm」：若安装 ffmpeg 且未禁用
    ``MIMO_REFER_FFMPEG_CONVERT``，会先转为标准 PCM WAV 再编码。
    """
    data = Path(path).read_bytes()
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("MiMo 参考音频超过 10MB（Base64 编码前限制），请缩短样本")
    if len(data) >= 9 and (
        data[:9].lower().startswith(b"<!doctype")
        or data[:5].lower() == b"<html"
    ):
        raise ValueError(
            "参考音频文件内容为 HTML（多为下载失败或未登录），请检查 MinIO/URL"
        )

    suf = os.path.splitext(path)[1].lower()
    if suf not in (".wav", ".mp3"):
        raise ValueError(
            f"MiMo 音色克隆官方仅支持 wav/mp3 样本，当前后缀 {suf!r}（{path}）"
        )

    mime = _sniff_mimo_clone_sample_mime(data)
    out_data: bytes | None = None
    out_mime: str | None = None

    if mime == "audio/wav" and is_standard_riff_wav(data):
        out_data, out_mime = data, "audio/wav"
    elif mime == "audio/mpeg":
        if suf == ".wav":
            logger.warning(
                "MiMo 参考扩展名为 .wav 但实际为 mp3，按 audio/mpeg 发送（%s）",
                path,
            )
        out_data, out_mime = data, "audio/mpeg"
    elif mime is None and suf == ".mp3":
        out_data, out_mime = data, "audio/mpeg"

    if out_data is None:
        conv = _mimo_try_ffmpeg_refer_to_wav(path)
        if conv is not None:
            out_data, out_mime = conv, "audio/wav"
            logger.info(
                "MiMo 参考音频已由 ffmpeg 转为标准 WAV（扩展名与真实编码不一致时常需此步）源=%s",
                path,
            )

    if out_data is None:
        if mime == "__reject_webm__":
            hint = "（可安装 ffmpeg 并保留 MIMO_REFER_FFMPEG_CONVERT=1 尝试自动转 wav）"
            raise ValueError(
                f"参考音频为 WebM/Matroska；MiMo 仅接受 wav/mp3 载荷{hint}"
            )
        if mime in ("__reject_flac__", "__reject_ogg__", "__reject_m4a__"):
            hint = "请安装 ffmpeg 并确保 PATH 可用，或导出为真 wav/mp3 再上传"
            raise ValueError(
                f"检测到 flac/ogg/m4a 等编码，与扩展名 .wav 不符；{hint}"
            )
        if suf == ".wav":
            raise ValueError(
                "文件不是有效 WAV（无 RIFF/WAVE）。若实为其它编码，请安装 ffmpeg "
                "或重新导出为标准 WAV/MP3"
            )
        else:
            raise ValueError(
                f"无法作为 wav/mp3 提交 MiMo（路径={path!r}），请检查文件内容"
            )

    if len(out_data) > 10 * 1024 * 1024:
        raise ValueError("MiMo 参考音频（含转码后）超过 10MB，请缩短样本")

    if out_mime == "audio/wav" and not is_standard_riff_wav(out_data):
        raise ValueError("转码后仍非标准 WAV，请检查源文件")

    b64 = base64.b64encode(out_data).decode("ascii")
    return f"data:{out_mime};base64,{b64}"


_mimo_refer_voice_dataurl_cache: dict[tuple[str, int], str] = {}
_MIMO_REFER_VOICE_CACHE_MAX_KEYS = 16


def _mimo_refer_voice_cache_enabled() -> bool:
    """仅缓存 **参考音频文件** 编码成的 ``audio.voice`` Data URL；**从不**缓存 MiMo 合成返回的 WAV。"""
    v = (os.getenv("MIMO_VOICE_DATAURL_CACHE") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _refer_voice_cache_backend() -> str:
    """``memory`` | ``redis`` | ``both``（先内存再 Redis，写入两级）。"""
    raw = (os.getenv("MIMO_VOICE_DATAURL_CACHE_BACKEND") or "memory").strip().lower()
    if raw in ("redis", "both"):
        return raw
    return "memory"


def _refer_voice_redis_ttl_s() -> int:
    try:
        return max(60, min(86400 * 30, int(os.getenv("MIMO_VOICE_DATAURL_REDIS_TTL") or "604800")))
    except ValueError:
        return 604800


def _refer_voice_redis_key(realpath_norm: str, mt_ns: int) -> str:
    raw = f"{realpath_norm}\0{mt_ns}".encode("utf-8", errors="surrogateescape")
    h = hashlib.sha256(raw).hexdigest()
    pfx = (os.getenv("REDIS_MIMO_REFER_DATAURL_PREFIX") or "mimo:refer:dataurl").strip()
    return f"{pfx}:{h}"


def _try_redis_client():
    try:
        from live2d_db.redis_factory import get_redis_client

        return get_redis_client(logger)
    except Exception:
        return None


def _trim_mimo_refer_memory_cache() -> None:
    if len(_mimo_refer_voice_dataurl_cache) >= _MIMO_REFER_VOICE_CACHE_MAX_KEYS:
        _mimo_refer_voice_dataurl_cache.clear()


def _encode_mimo_voice_sample_file_cached(path: str) -> str:
    """在同一路径参考音、未改文件的前提下复用 Data URL；可选进程内 dict 与/或 Redis。

    键含 ``realpath`` + ``mtime_ns``；文件更新即换键。与合成结果无关。
    """
    if not _mimo_refer_voice_cache_enabled():
        return _encode_mimo_voice_sample_file(path)
    try:
        st = os.stat(path)
    except OSError:
        return _encode_mimo_voice_sample_file(path)
    mt_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    rp = os.path.normcase(os.path.realpath(path))
    mem_key = (rp, mt_ns)
    backend = _refer_voice_cache_backend()

    if backend in ("memory", "both"):
        hit = _mimo_refer_voice_dataurl_cache.get(mem_key)
        if hit is not None:
            return hit

    rkey = _refer_voice_redis_key(rp, mt_ns)
    if backend in ("redis", "both"):
        cli = _try_redis_client()
        if cli is not None:
            try:
                blob = cli.get(rkey)
                if blob:
                    s = blob.decode("utf-8") if isinstance(blob, (bytes, bytearray)) else str(blob)
                    if s:
                        if backend == "both":
                            _mimo_refer_voice_dataurl_cache[mem_key] = s
                            _trim_mimo_refer_memory_cache()
                        return s
            except Exception:
                logger.warning(
                    "MiMo 参考音 Data URL Redis 读取失败 key=%s",
                    rkey,
                    exc_info=False,
                )

    out = _encode_mimo_voice_sample_file(path)

    if backend in ("redis", "both"):
        cli = _try_redis_client()
        if cli is not None:
            try:
                cli.set(rkey, out, ex=_refer_voice_redis_ttl_s())
            except Exception:
                logger.warning(
                    "MiMo 参考音 Data URL Redis 写入失败 key=%s",
                    rkey,
                    exc_info=False,
                )

    if backend in ("memory", "both"):
        _mimo_refer_voice_dataurl_cache[mem_key] = out
        _trim_mimo_refer_memory_cache()

    return out


def _mimo_preset_voice_id(text_language: str) -> str:
    """V2.5 预置音色名（如 ``冰糖`` / ``Mia``）；可用 ``MIMO_TTS_VOICE`` 覆盖。"""
    explicit = (os.getenv("MIMO_TTS_VOICE") or "").strip()
    if explicit:
        return explicit
    lang = (text_language or "zh").strip().lower()
    if lang.startswith("en") or lang == "english":
        return "Mia"
    return "冰糖"


def _mimo_flatten_content_for_api(s: str) -> str:
    """MiMo 请求体：去掉 ``\\n`` / ``\\r``，合并空白，避免 assistant 多段故事等带入换行符。"""
    raw = (s or "").strip()
    if not raw:
        return ""
    t = re.sub(r"[\r\n]+", " ", raw)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


def _mimo_merge_user_content(
    *,
    sample_path: bool,
    user_director_prompt: str | None,
    ctx: str,
    user_prompt_env: str,
) -> str:
    """MiMo：自然语言导演指令放在 user；多条来源按顺序拼接。"""
    director = (user_director_prompt or "").strip()
    parts: list[str] = []
    if director:
        parts.append(director)
    c = (ctx or "").strip()
    if c:
        parts.append(c)
    u = (user_prompt_env or "").strip()
    if u:
        parts.append(u)
    merged = "\n\n".join(parts)
    if merged:
        return _mimo_flatten_content_for_api(merged)
    if sample_path:
        return ""
    return _mimo_flatten_content_for_api(
        user_prompt_env or "请根据下文合成自然、清晰的语音。"
    )


def mimo_tts(
    text: str,
    *,
    text_language: str = "zh",
    refer_runtime: dict | None = None,
    user_director_prompt: str | None = None,
    speech_assistant_only: bool = False,
    merge_env_user_prompts: bool = True,
    timeout_s: float | None = None,
    retries: int | None = None,
) -> bytes:
    """调用小米 MiMo 语音合成，成功返回 WAV 二进制。

    **预置音色**：``mimo-v2.5-tts``（默认），``audio.voice`` 为音色名（如 ``冰糖``）。

    **音色克隆**：模型固定为 ``mimo-v2.5-tts-voiceclone``（官方当前唯一支持）。
    ``audio.voice`` 为 ``data:audio/wav;base64,...`` 或 ``data:audio/mpeg;base64,...``，
    样本仅 **wav/mp3**，编码前 ≤10MB。可在 **user** 消息中传入自然语言风格指令（可为空字符串）；
    待合成文本在 **assistant**。鉴权默认请求头 ``api-key``（与官方 Curl 一致），可选
    ``MIMO_USE_BEARER_AUTH=1`` 改用 ``Authorization: Bearer``。

    ``speech_assistant_only=True``：不把传入的导演/``MIMO_TTS_CONTEXT``/``MIMO_TTS_USER_PROMPT``
    拼进 user（WebSocket 默认改为 **关闭** 此项并携带完整导演，见路由侧）；**user** 仅保留
    ``MIMO_TTS_WS_USER_HINT``（若配置）。``merge_env_user_prompts=False`` 时忽略环境变量里的
    CONTEXT/USER_PROMPT，以免与同一条请求里的 ``user_director_prompt`` 重复堆叠。

    文档: https://platform.xiaomimimo.com/docs/zh-CN/usage-guide/speech-synthesis-v2.5

    环境变量：
        ``MIMO_API_KEY``（必填）、``MIMO_API_BASE``（默认 ``https://api.xiaomimimo.com``）、
        ``MIMO_TTS_MODEL``（可强制指定模型）、``MIMO_TTS_VOICE``（预置音色）、
        ``MIMO_VOICE_SAMPLE_PATH``（克隆样本，仅 wav/mp3）、
        ``MIMO_VOICE_DATAURL_CACHE``（默认 ``1``：启用参考音 Data URL 缓存；``0`` 关闭）、
        ``MIMO_VOICE_DATAURL_CACHE_BACKEND``（``memory`` | ``redis`` | ``both``；默认 ``memory``；
        ``redis`` 用项目 Redis，``both`` 先内存再 Redis）、
        ``MIMO_VOICE_DATAURL_REDIS_TTL``（Redis 过期秒数，默认 ``604800``）、
        ``REDIS_MIMO_REFER_DATAURL_PREFIX``（默认 ``mimo:refer:dataurl``）、
        ``MIMO_TTS_STYLE``（可选，V2.5 整体风格标签 ``(风格)正文``）、
        ``MIMO_TTS_CONTEXT``（可选，克隆时优先作为 user 侧「导演/语气」指令）、
        ``MIMO_TTS_USER_PROMPT``（预置模式默认 user 句；克隆无 CONTEXT 时可作 user）。
        ``MIMO_TTS_SYSTEM_PROMPT``（可选）：携带数据库人设导演（``user_director_prompt`` 非空且非
        ``speech_assistant_only``）时，作为 **user** 消息前缀（官方 TTS 模型不接受 ``role=system``）；
        不设则默认为「你是语音合成助手，请按照【人设】与【语气】合成语音。」
        ``MIMO_TTS_WS_USER_HINT``（仅在 ``speech_assistant_only=True`` 时生效）：写入 **user** 的固定短句，
        用于约束语气连贯（例如要求平稳、不要音效）；不传则 user 为空。
        调用方传入的 ``user_director_prompt`` 与（默认可用的）CONTEXT / USER_PROMPT **按顺序拼接**
        （导演指令在前）。``merge_env_user_prompts=False`` 时不拼环境变量段。
        发往 MiMo 的 user/assistant ``content`` 会 **压平换行**（``\\n``/``\\r`` 改为空格），避免长文本含段落分隔。
        ``MIMO_TTS_TIMEOUT``（单次 HTTP 超时秒数，默认 60；长句或 **音色克隆大包体**
        （单次 POST 常数百 KB）上行排队偶发超时可调高如 **120**）、
        ``MIMO_TTS_RETRIES``（默认 8，应对 429 限流）、``MIMO_TTS_429_BACKOFF_BASE`` /
        ``MIMO_TTS_429_BACKOFF_CAP``（429 指数退避）。
        间歇读超时：设 ``TTS_DEBUG=1`` 后可对照 ``mimo_http req begin`` 的 ``payload_bytes`` /
        ``voice_field_chars`` / ``text_chars`` 与 ``retry`` 行的 ``detail``（ errno 等）。
    """
    req_timeout_s = _mimo_http_timeout_s(timeout_s)
    persona_director_saved = (user_director_prompt or "").strip()
    api_key = (os.getenv("MIMO_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("MIMO_API_KEY 未配置，无法使用 MiMo TTS")
    base = (
        os.getenv("MIMO_API_BASE") or "https://api.xiaomimimo.com"
    ).rstrip("/")
    url = f"{base}/v1/chat/completions"

    sample_path = _mimo_resolve_voice_sample_path(refer_runtime)
    model_env = (os.getenv("MIMO_TTS_MODEL") or "").strip()
    clone_model_default = "mimo-v2.5-tts-voiceclone"
    if sample_path:
        # 参考音频必须用 voiceclone 系列；预置 TTS 模型（如 mimo-v2-tts）与 Data URL 样本不兼容
        if model_env and "voiceclone" in model_env.lower():
            model = model_env
        else:
            model = clone_model_default
            if model_env:
                logger.info(
                    "MiMo：已启用参考音频克隆，忽略预置 MIMO_TTS_MODEL=%s，使用 %s",
                    model_env,
                    clone_model_default,
                )
        voice_field: str = _encode_mimo_voice_sample_file_cached(sample_path)
        voice_log = f"clone:{sample_path}"
    else:
        model = model_env or "mimo-v2.5-tts"
        if "voiceclone" in model_env.lower():
            raise RuntimeError(
                "MIMO_TTS_MODEL 为音色克隆模型，但未找到本地参考音频："
                "请在上传页绑定 wav/mp3，或设置 MIMO_VOICE_SAMPLE_PATH"
            )
        voice_field = _mimo_preset_voice_id(text_language)
        voice_log = voice_field

    style = (os.getenv("MIMO_TTS_STYLE") or "").strip()
    text_one_line = _mimo_flatten_content_for_api(text)
    assistant_content = (
        f"({style}){text_one_line}" if style else text_one_line
    )

    ctx = (os.getenv("MIMO_TTS_CONTEXT") or "").strip()
    user_prompt_env = (os.getenv("MIMO_TTS_USER_PROMPT") or "").strip()
    if speech_assistant_only:
        ctx = ""
        user_prompt_env = ""
        user_director_prompt = None
    elif not merge_env_user_prompts:
        ctx = ""
        user_prompt_env = ""

    if speech_assistant_only:
        ws_hint = (os.getenv("MIMO_TTS_WS_USER_HINT") or "").strip()
        user_content = (
            _mimo_flatten_content_for_api(ws_hint) if ws_hint else ""
        )
    else:
        user_content = _mimo_merge_user_content(
            sample_path=bool(sample_path),
            user_director_prompt=user_director_prompt,
            ctx=ctx,
            user_prompt_env=user_prompt_env,
        )

    # MiMo TTS chat/completions：服务端拒绝 messages 中含 system（见 error param messages[0] system role...）
    if persona_director_saved and not speech_assistant_only:
        sys_fixed = (os.getenv("MIMO_TTS_SYSTEM_PROMPT") or "").strip()
        if not sys_fixed:
            sys_fixed = "你是语音合成助手，请按照【人设】与【语气】合成语音。"
        uc_rest = (user_content or "").strip()
        if uc_rest:
            user_content = _mimo_flatten_content_for_api(f"{sys_fixed}\n\n{uc_rest}")
        else:
            user_content = _mimo_flatten_content_for_api(sys_fixed)

    messages: list[dict[str, str]] = [
        {"role": "user", "content": user_content},
        {"role": "assistant", "content": assistant_content},
    ]

    payload = {
        "model": model,
        "messages": messages,
        "audio": {"format": "wav", "voice": voice_field},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if retries is None:
        try:
            retries = int(os.getenv("MIMO_TTS_RETRIES") or "8")
        except ValueError:
            retries = 8
    n = max(1, min(15, int(retries)))
    dbg = tts_debug_enabled()
    t_all = time.perf_counter()
    last_err: BaseException | None = None
    for attempt in range(n):
        use_bearer = (os.getenv("MIMO_USE_BEARER_AUTH") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        hdrs: dict[str, str] = {
            "Content-Type": "application/json; charset=utf-8",
            "Connection": "close",
        }
        if use_bearer:
            hdrs["Authorization"] = f"Bearer {api_key}"
        else:
            hdrs["api-key"] = api_key
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers=hdrs,
        )
        t_req = time.perf_counter()
        if dbg:
            host = urllib.parse.urlparse(url).netloc
            logger.info(
                "[TTS_DEBUG] mimo_http req begin attempt=%d/%d host=%s "
                "payload_bytes=%d voice_field_chars=%d clone=%s model=%s "
                "text_chars=%d assistant_chars=%d user_chars=%d timeout_s=%.1f",
                attempt + 1,
                n,
                host,
                len(data),
                len(voice_field),
                bool(sample_path),
                model,
                len(text),
                len(assistant_content),
                len(user_content),
                req_timeout_s,
            )
        try:
            with urllib.request.urlopen(req, timeout=req_timeout_s) as resp:
                body = resp.read()
                try:
                    obj = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError as e:
                    raise RuntimeError(
                        f"MiMo 响应非 JSON: {body[:300]!r}"
                    ) from e
                choices = obj.get("choices")
                if not isinstance(choices, list) or not choices:
                    raise RuntimeError(
                        f"MiMo 响应缺少 choices: {obj!r}"[:800]
                    )
                msg = choices[0].get("message") or {}
                audio_obj = msg.get("audio") or {}
                b64 = audio_obj.get("data")
                if not b64 or not isinstance(b64, str):
                    raise RuntimeError(
                        f"MiMo 响应缺少 message.audio.data: {obj!r}"[:800]
                    )
                wav = base64.b64decode(b64)
                if dbg:
                    logger.info(
                        "[TTS_DEBUG] mimo_http ok total_ms=%.1f req_ms=%.1f "
                        "attempt=%d/%d payload_bytes=%d text_chars=%d wav_bytes=%d "
                        "model=%s voice=%s",
                        (time.perf_counter() - t_all) * 1000,
                        (time.perf_counter() - t_req) * 1000,
                        attempt + 1,
                        n,
                        len(data),
                        len(text),
                        len(wav),
                        model,
                        voice_log,
                    )
                return wav
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            last_err = e
            if dbg:
                logger.warning(
                    "[TTS_DEBUG] mimo_http HTTPError code=%s total_ms=%.1f "
                    "attempt=%d/%d payload_bytes=%d err=%s",
                    e.code,
                    (time.perf_counter() - t_all) * 1000,
                    attempt + 1,
                    n,
                    len(data),
                    err_body[:500],
                )
            sleep_retry = _mimo_retry_sleep_for_http_error(e, attempt)
            if sleep_retry is None or attempt + 1 >= n:
                raise RuntimeError(
                    f"MiMo API HTTP {e.code}: {err_body}"
                ) from e
            if dbg and e.code == 429:
                logger.info(
                    "[TTS_DEBUG] mimo_http 429 限流，%.1fs 后重试 (%d/%d)",
                    sleep_retry,
                    attempt + 1,
                    n,
                )
            time.sleep(sleep_retry)
        except (
            http.client.IncompleteRead,
            urllib.error.URLError,
            TimeoutError,
            ConnectionResetError,
            BrokenPipeError,
            OSError,
            RuntimeError,
        ) as e:
            last_err = e
            if dbg:
                _hint = _mimo_large_payload_timeout_hint(
                    len(data), req_timeout_s, e
                )
                logger.warning(
                    "[TTS_DEBUG] mimo_http retry total_ms=%.1f req_ms=%.1f "
                    "timeout_s=%.1f attempt=%d/%d payload_bytes=%d err=%s detail=%s%s",
                    (time.perf_counter() - t_all) * 1000,
                    (time.perf_counter() - t_req) * 1000,
                    req_timeout_s,
                    attempt + 1,
                    n,
                    len(data),
                    e,
                    _mimo_network_err_detail(e),
                    _hint,
                )
            if attempt + 1 >= n:
                if isinstance(e, RuntimeError):
                    raise
                _fail_hint = _mimo_large_payload_timeout_hint(
                    len(data), req_timeout_s, e
                )
                raise RuntimeError(
                    f"MiMo API 请求失败（已重试 {n} 次）: {e}{_fail_hint}"
                ) from e
            time.sleep(0.25 * (attempt + 1))
    raise RuntimeError("MiMo API 不可达") from last_err


def _iter_tokens(text: str) -> Generator[str, None, None]:
    """把文本切成近似 token：中文单字、英文单词、数字、标点。"""
    pattern = r"[\u4e00-\u9fff]|[A-Za-z]+(?:'[A-Za-z]+)?|\d+|[^\w\s]"
    for tk in re.findall(pattern, text):
        if tk:
            yield tk


_SENTENCE_PUNC = {"。", "！", "？", ".", "!", "?", ";", "；"}


def _iter_chunk_by_tokens(text: str, chunk_tokens: int = 5) -> Generator[str, None, None]:
    """按 token 数分块。"""
    if chunk_tokens <= 0:
        raise ValueError("chunk_tokens 必须 > 0")
    bucket: list[str] = []
    for tk in _iter_tokens(text):
        bucket.append(tk)
        if len(bucket) >= chunk_tokens:
            yield "".join(bucket)
            bucket.clear()
    if bucket:
        yield "".join(bucket)


def gpt_sovits_tts(
    text: str,
    *,
    text_language: str = "zh",
    speed: float | None = None,
    refer_wav_path: str | None = None,
    prompt_text: str | None = None,
    prompt_language: str | None = None,
    base: str | None = None,
    timeout_s: float = 300.0,
    retries: int = 3,
) -> bytes:
    """POST `/`，成功返回 WAV 二进制。"""
    base = (base or os.getenv("GPTSOVITS_API_BASE") or "http://127.0.0.1:9880").rstrip("/")
    url = base + "/"
    payload = {"text": text, "text_language": text_language}
    if speed is not None:
        payload["speed"] = speed
    if refer_wav_path:
        payload["refer_wav_path"] = refer_wav_path
    if prompt_text:
        payload["prompt_text"] = prompt_text
    if prompt_language:
        payload["prompt_language"] = prompt_language
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    n = max(1, min(8, int(retries)))
    last_conn: BaseException | None = None
    dbg = tts_debug_enabled()
    t_all = time.perf_counter()
    for attempt in range(n):
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Connection": "close",
            },
        )
        t_req = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = resp.read()
                if dbg:
                    logger.info(
                        "[TTS_DEBUG] gpt_sovits_http ok total_ms=%.1f req_ms=%.1f "
                        "attempt=%d/%d text_chars=%d json_bytes=%d wav_bytes=%d base=%s",
                        (time.perf_counter() - t_all) * 1000,
                        (time.perf_counter() - t_req) * 1000,
                        attempt + 1,
                        n,
                        len(text),
                        len(data),
                        len(body),
                        base,
                    )
                return body
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if dbg:
                logger.warning(
                    "[TTS_DEBUG] gpt_sovits_http HTTPError code=%s total_ms=%.1f attempt=%d/%d err=%s",
                    e.code,
                    (time.perf_counter() - t_all) * 1000,
                    attempt + 1,
                    n,
                    err_body[:500],
                )
            raise RuntimeError(f"GPT-SoVITS API HTTP {e.code}: {err_body}") from e
        except (
            http.client.IncompleteRead,
            urllib.error.URLError,
            TimeoutError,
            ConnectionResetError,
            BrokenPipeError,
            OSError,
        ) as e:
            last_conn = e
            if dbg:
                logger.warning(
                    "[TTS_DEBUG] gpt_sovits_http retry total_ms=%.1f req_ms=%.1f attempt=%d/%d err=%s",
                    (time.perf_counter() - t_all) * 1000,
                    (time.perf_counter() - t_req) * 1000,
                    attempt + 1,
                    n,
                    e,
                )
            if attempt + 1 >= n:
                raise RuntimeError(
                    f"GPT-SoVITS API 连接读取失败（已重试 {n} 次）: {e}"
                ) from e
            time.sleep(0.25 * (attempt + 1))
    raise RuntimeError("GPT-SoVITS API 不可达") from last_conn


def llm_to_tts_stream(
    prompt: str,
    *,
    model: str | None = None,
    ollama_host: str | None = None,
    text_language: str = "zh",
    chunk_tokens: int = 5,
    flush_mode: str = "punc",
    speed: float = 1.15,
    out_dir: Path | None = None,
    base: str | None = None,
) -> Generator[dict, None, None]:
    """
    流式输入 + 输出：
    1) 从 Ollama stream=True 流式读文本
    2) 按 flush_mode 切分后立刻调用 GPT-SoVITS
    3) 产出每段文本和对应 wav 文件路径
    """
    try:
        import ollama
    except ImportError as e:
        raise RuntimeError("未安装 ollama 包，请先 `pip install ollama`") from e

    model = model or os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    ollama_host = ollama_host or os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    out_dir = out_dir or (Path(__file__).resolve().parent / "tts_stream_chunks")
    out_dir.mkdir(parents=True, exist_ok=True)

    client = ollama.Client(host=ollama_host)
    stream = client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        stream=True,
    )

    token_bucket: list[str] = []
    idx = 0
    if flush_mode not in {"punc", "token", "mixed"}:
        raise ValueError("flush_mode 必须是 punc / token / mixed")

    def _flush_bucket() -> dict | None:
        nonlocal idx, token_bucket
        if not token_bucket:
            return None
        idx += 1
        text_part = "".join(token_bucket)
        token_bucket = []
        if normalized_tts_provider() == "mimo":
            wav = mimo_tts(text_part, text_language=text_language, refer_runtime=None)
        else:
            wav = gpt_sovits_tts(
                text_part, text_language=text_language, speed=speed, base=base
            )
        out_path = out_dir / f"chunk_{idx:04d}.wav"
        out_path.write_bytes(wav)
        return {"index": idx, "text": text_part, "wav_path": str(out_path), "bytes": len(wav)}

    for chunk in stream:
        content = chunk.get("message", {}).get("content", "")
        if not content:
            continue
        for tk in _iter_tokens(content):
            token_bucket.append(tk)
            if flush_mode == "punc":
                should_flush = tk in _SENTENCE_PUNC
            elif flush_mode == "token":
                should_flush = len(token_bucket) >= chunk_tokens
            else:
                # mixed: 达到 token 阈值或遇到断句标点任一即出块
                should_flush = len(token_bucket) >= chunk_tokens or tk in _SENTENCE_PUNC
            if should_flush:
                item = _flush_bucket()
                if item is not None:
                    yield item

    item = _flush_bucket()
    if item is not None:
        yield item


def _is_wav_header(blob: bytes) -> bool:
    return len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WAVE"


def run_tts_test(
    text: str | None = None,
    out_path: Path | None = None,
    speed: float = 1.95,
    base: str | None = None,
) -> Path:
    """请求一次合成并写入 WAV，供手动试听。"""
    text = text or "你好，这是 GPT-SoVITS 接口测试。"
    out_path = out_path or (Path(__file__).resolve().parent / "tts_gptsovits_test.wav")
    api_base = base or os.getenv("GPTSOVITS_API_BASE") or "http://127.0.0.1:9880"

    print(f"GPTSOVITS_API_BASE={api_base}")
    print(f"text={text!r}")
    print(f"speed={speed}")
    print(f"out={out_path}")

    wav = gpt_sovits_tts(text, speed=speed, base=api_base)
    if not _is_wav_header(wav):
        print("警告：返回体不是标准 WAV 头（RIFF/WAVE），仍已写入文件供排查。", file=sys.stderr)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(wav)
    print(f"已写入 {out_path.resolve()} ，大小 {len(wav)} 字节")
    return out_path


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description="TTS 连通性测试（GPT-SoVITS 或 MiMo）"
    )
    p.add_argument(
        "--provider",
        choices=["gpt_sovits", "mimo"],
        default=None,
        help="覆盖 TTS_PROVIDER：gpt_sovits | mimo",
    )
    p.add_argument("--text", default=None, help="合成文本，默认短句测试")
    p.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="输出 wav 路径，默认 utils/tts_gptsovits_test.wav",
    )
    p.add_argument(
        "--base",
        default=None,
        help="覆盖 GPTSOVITS_API_BASE，例如 http://127.0.0.1:9880",
    )
    p.add_argument(
        "--speed",
        type=float,
        default=1.15,
        help="语速倍率，1.0 为默认，>1 更快，<1 更慢",
    )
    p.add_argument("--stream", action="store_true", help="启用 LLM->TTS 流式模式")
    p.add_argument("--prompt", default="请用中文简短介绍一下你自己。", help="LLM 提示词")
    p.add_argument("--model", default=None, help="Ollama 模型名，默认读 OLLAMA_MODEL")
    p.add_argument("--ollama-host", default=None, help="Ollama 地址，默认读 OLLAMA_HOST")
    p.add_argument("--chunk-tokens", type=int, default=5, help="flush-mode=token/mixed 时每块 token 数")
    p.add_argument(
        "--flush-mode",
        choices=["punc", "token", "mixed"],
        default="punc",
        help="切分策略：punc=仅标点触发（默认），token=仅按token数，mixed=二者任一触发",
    )
    p.add_argument("--out-dir", type=Path, default=None, help="流式模式下每段 wav 的输出目录")
    args = p.parse_args()
    if args.provider:
        os.environ["TTS_PROVIDER"] = args.provider
    if args.stream:
        start = time.time()
        total = 0
        for item in llm_to_tts_stream(
            prompt=args.prompt,
            model=args.model,
            ollama_host=args.ollama_host,
            chunk_tokens=args.chunk_tokens,
            flush_mode=args.flush_mode,
            speed=args.speed,
            out_dir=args.out_dir,
            base=args.base,
        ):
            total += 1
            print(
                f"[chunk {item['index']}] text={item['text']!r} "
                f"bytes={item['bytes']} file={item['wav_path']}"
            )
        print(f"流式完成，共 {total} 段，耗时 {time.time() - start:.2f}s")
    else:
        if normalized_tts_provider() == "mimo":
            text = args.text or "你好，这是小米 MiMo 语音合成测试。"
            out_path = args.out or (
                Path(__file__).resolve().parent / "tts_mimo_test.wav"
            )
            print(f"MIMO_API_BASE={os.getenv('MIMO_API_BASE', 'https://api.xiaomimimo.com')}")
            print(f"text={text!r}")
            print(f"out={out_path}")
            wav = mimo_tts(text, refer_runtime=None)
            if not _is_wav_header(wav):
                print(
                    "警告：返回体不是标准 WAV 头（RIFF/WAVE），仍已写入文件供排查。",
                    file=sys.stderr,
                )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(wav)
            print(f"已写入 {out_path.resolve()} ，大小 {len(wav)} 字节")
        else:
            run_tts_test(
                text=args.text, out_path=args.out, speed=args.speed, base=args.base
            )
