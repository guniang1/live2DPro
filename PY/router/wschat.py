from fastapi import APIRouter, WebSocket
# 
from starlette.websockets import WebSocketDisconnect, WebSocketState  # 新增：导入WebSocketState枚举
import pymysql.err
import ollama
import os
import time
import asyncio
import json
import logging
from datetime import datetime, timezone
import re
import tempfile
import threading
import urllib.parse
import urllib.request
from typing import Optional, Tuple

from utils.tts import (
    gpt_sovits_tts,
    mimo_tts,
    mimo_tts_configured,
    normalized_tts_provider,
    tts_debug_enabled,
)
from live2d_db.connection import connection_ctx
from live2d_db.package_key_util import normalize_package_key as _normalize_package_key_for_cache
from live2d_db.db_config import DbConfig
from live2d_db.entities import ChatSession, RemindTrigger
from live2d_db.long_memory_fields import (
    long_memory_has_any_content,
    merge_long_memory_record_for_prompt,
)
from live2d_db.minio_redis_cache import get_object_bytes_cached
from live2d_db import memory_layers as _memory_layers
from live2d_db.redis_factory import get_redis_client as _redis_factory_get_client
from live2d_db.repositories import (
    ChatSessionRepository,
    LongMemoryRepository,
    PersonaRepository,
    Live2dTtsReferRepository,
    RemindTriggerRepository,
    UserProfileRepository,
)
from utils.user_profile_refresh import (
    chat_inject_enabled,
    format_profile_for_chat_system,
    maybe_refresh_user_profile_after_turn,
    refresh_user_profile_on_disconnect,
)
from utils.live2d_catalog import (
    get_catalog_for_package,
    normalize_expression_pick,
    normalize_motion_pick,
    resolve_expression_id,
    resolve_motion_id,
)

# 配置日志（方便排查问题）
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()


async def _try_send_json(websocket: WebSocket, payload: dict) -> bool:
    """发送 JSON；客户端已断开（含 WinError 10053 类中止）时返回 False，不向外抛。"""
    try:
        await websocket.send_json(payload)
        return True
    except WebSocketDisconnect:
        return False
    except RuntimeError as e:
        if "close message" in str(e).lower():
            return False
        raise
    except OSError:
        return False


async def _try_send_bytes(websocket: WebSocket, data: bytes) -> bool:
    try:
        await websocket.send_bytes(data)
        return True
    except WebSocketDisconnect:
        return False
    except RuntimeError as e:
        if "close message" in str(e).lower():
            return False
        raise
    except OSError:
        return False

# ========== 环境变量（由 main.py 先 load_dotenv(PY/.env)） ==========
ollama_host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
no_proxy = os.getenv("NO_PROXY", "127.0.0.1,localhost")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
# 与聊天分离：专门从用户话中决策表情/动作标识名（可设为更小或更快的模型）
OLLAMA_ACTION_MODEL = os.getenv("OLLAMA_ACTION_MODEL", OLLAMA_MODEL)

os.environ["OLLAMA_HOST"] = ollama_host
os.environ["NO_PROXY"] = no_proxy
client = ollama.Client(host=ollama_host)
logger.info(
    f"✅ Ollama 客户端初始化成功，地址：{ollama_host}，聊天模型：{OLLAMA_MODEL}，动作/表情模型：{OLLAMA_ACTION_MODEL}"
)

# 与 query ?session= 配对：TTS 与文本在 /ws/chat 同连接按序下发（见 chunk_audio）；/ws/tts 仅保留兼容
_session_lock = asyncio.Lock()
_session_tts_ws: dict[str, WebSocket] = {}
_session_chat_ws: dict[str, WebSocket] = {}
# user_id -> 当前活跃的 /ws/chat 连接（同一用户多标签页各一条）
_chat_ws_by_user: dict[int, list[WebSocket]] = {}


def _register_chat_ws_for_user(user_id: int, websocket: WebSocket) -> None:
    lst = _chat_ws_by_user.setdefault(user_id, [])
    if websocket not in lst:
        lst.append(websocket)


def _unregister_chat_ws_for_user(user_id: int, websocket: WebSocket) -> None:
    lst = _chat_ws_by_user.get(user_id)
    if not lst:
        return
    try:
        lst.remove(websocket)
    except ValueError:
        pass
    if not lst:
        _chat_ws_by_user.pop(user_id, None)


def _remind_trigger_ws_payload(t: RemindTrigger, *, display_content: str) -> dict:
    """``display_content`` 为触发时点生成的对用户话术；库表 ``trigger_content`` 仍为情景描述。"""
    tt = t.trigger_time
    ts = None
    if isinstance(tt, datetime):
        ts = tt.isoformat(sep=" ", timespec="seconds")
    return {
        "type": "remind_trigger",
        "trigger_id": t.trigger_id,
        "trigger_type": t.trigger_type,
        "trigger_content": (display_content or "").strip(),
        "trigger_time": ts,
    }


def _remind_trigger_use_mimo_tts() -> bool:
    raw = os.getenv("REMIND_TRIGGER_USE_MIMO", "1")
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


async def _deliver_remind_trigger_on_websocket(
    websocket: WebSocket,
    user_id: int,
    t: RemindTrigger,
) -> bool:
    """先按语境生成话术并下发 ``remind_trigger`` JSON；若 MiMo 可用则再下发 ``chunk_audio``（``remind_audio``）+ WAV。

    话术由 ``session_id`` 关联的单轮对话 + 库中情景描述（``trigger_content``）经 LLM 生成；MiMo 朗读该生成稿。
    """
    pkg = _live2d_package_from_websocket(websocket)
    from utils.remind_delivery import generate_remind_delivery_message

    display_line = await asyncio.to_thread(generate_remind_delivery_message, t, pkg)
    if not await _try_send_json(
        websocket, _remind_trigger_ws_payload(t, display_content=display_line)
    ):
        return False

    if not _remind_trigger_use_mimo_tts():
        return True
    if normalized_tts_provider() != "mimo" or not mimo_tts_configured():
        return True

    text = (display_line or "").strip()
    if not text:
        return True

    refer = await asyncio.to_thread(_load_tts_refer_runtime, user_id, pkg)
    wav: bytes = b""
    try:
        _inc_dir = _mimo_ws_include_director_enabled()
        mimo_director_user_prompt = ""
        if _inc_dir:
            mimo_director_user_prompt = await asyncio.to_thread(
                _mimo_director_user_prompt_sync,
                user_id,
                pkg,
                "",
            )
        tts_lang = os.getenv("TTS_TEXT_LANGUAGE", "zh")
        wav = await asyncio.to_thread(
            mimo_tts,
            text,
            text_language=tts_lang,
            refer_runtime=refer,
            user_director_prompt=mimo_director_user_prompt or None,
            speech_assistant_only=not _inc_dir,
            merge_env_user_prompts=not _inc_dir,
        )
    except Exception:
        logger.exception(
            "定时关怀 MiMo 合成失败 trigger_id=%s user_id=%s package=%s",
            t.trigger_id,
            user_id,
            pkg,
        )
        return True
    finally:
        _cleanup_tts_refer_runtime(refer)

    if not wav:
        return True

    chunk_meta = {
        "type": "chunk_audio",
        "index": 1,
        "size": len(wav),
        "content": "",
        "remind_audio": True,
    }
    if not await _try_send_json(websocket, chunk_meta):
        return True
    await _try_send_bytes(websocket, wav)
    return True


async def broadcast_remind_trigger_to_user(user_id: int, t: RemindTrigger) -> bool:
    """向该用户所有在线聊天连接广播定时场景推送（含可选 MiMo 朗读）；无在线连接则返回 False。"""
    async with _session_lock:
        conns = list(_chat_ws_by_user.get(user_id, []))
    any_ok = False
    for ws in conns:
        if ws.client_state != WebSocketState.CONNECTED:
            continue
        ok = await _deliver_remind_trigger_on_websocket(ws, user_id, t)
        any_ok = any_ok or ok
    return any_ok


async def flush_pending_reminders_for_connection(websocket: WebSocket, user_id: int) -> None:
    """连接建立后补发该用户已到期且尚未成功投递的提醒（与后台扫描共用认领语义）。"""
    before = datetime.now()

    def _list_u():
        with connection_ctx(DbConfig.from_env()) as conn:
            return RemindTriggerRepository.list_pending_for_user_before(conn, user_id, before, limit=100)

    rows = await asyncio.to_thread(_list_u)
    for t in rows:
        tid = t.trigger_id
        if tid is None:
            continue
        if websocket.client_state != WebSocketState.CONNECTED:
            break

        def _claim():
            with connection_ctx(DbConfig.from_env()) as conn:
                return RemindTriggerRepository.claim_pending_trigger(conn, tid)

        claimed = await asyncio.to_thread(_claim)
        if not claimed:
            continue
        ok = await _deliver_remind_trigger_on_websocket(websocket, user_id, t)
        if not ok:

            def _release():
                with connection_ctx(DbConfig.from_env()) as conn:
                    RemindTriggerRepository.release_trigger_claim(conn, tid)

            await asyncio.to_thread(_release)


def _session_id_from_websocket(websocket: WebSocket) -> str:
    q = websocket.query_params.get("session") or websocket.query_params.get("sid")
    return (q or "").strip() or "default"


def _safe_live2d_package_key(raw: str | None) -> str:
    """与前端 Resources 下目录名一致，禁止路径穿越。"""
    s = (raw or "").strip()
    if not s:
        return os.getenv("LIVE2D_PACKAGE", "Xiaozi")
    if ".." in s or "/" in s or "\\" in s:
        logger.warning("非法 Live2D package 参数已忽略: %r，使用默认包", raw)
        return os.getenv("LIVE2D_PACKAGE", "Xiaozi")
    return s


def _live2d_package_from_websocket(websocket: WebSocket) -> str:
    q = (
        websocket.query_params.get("package")
        or websocket.query_params.get("live2d_package")
        or websocket.query_params.get("model")
    )
    return _safe_live2d_package_key(q)


def _user_id_from_websocket(websocket: WebSocket) -> int:
    raw = websocket.query_params.get("user_id") or websocket.query_params.get("uid")
    if raw:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
    try:
        return max(1, int(os.getenv("LIVE2D_DEFAULT_USER_ID", "1")))
    except ValueError:
        return 1


def _load_tts_refer_runtime(user_id: int, package_key: str) -> dict | None:
    """读取模型包参考音频配置，并返回可直接给 GPT-SoVITS 的参数。"""
    try:
        with connection_ctx(DbConfig.from_env()) as conn:
            row = Live2dTtsReferRepository.get_by_user_and_package(conn, user_id, package_key)
        if row is None:
            return None
        if not row.prompt_text or not row.prompt_language:
            return None

        refer_wav_path = None
        tmp_refer_wav_path = None
        if row.audio_object_key:
            try:
                refer_wav_path, tmp_refer_wav_path = _materialize_refer_from_minio_object_key(
                    row.audio_object_key
                )
            except Exception:
                logger.exception(
                    "从 MinIO 读取参考音频失败 user_id=%s package=%s key=%s",
                    user_id,
                    package_key,
                    row.audio_object_key,
                )
                return None
        elif row.audio_url:
            refer_wav_path, tmp_refer_wav_path = _materialize_refer_wav_path(row.audio_url)
        else:
            return None

        if not refer_wav_path:
            return None
        return {
            "refer_wav_path": refer_wav_path,
            "prompt_text": row.prompt_text,
            "prompt_language": row.prompt_language,
            "_tmp_refer_wav_path": tmp_refer_wav_path,
        }
    except Exception:
        logger.exception("加载模型参考音频失败 user_id=%s package=%s", user_id, package_key)
        return None


def _materialize_refer_from_minio_object_key(object_key: str) -> tuple[str, str | None]:
    """经 MinIO SDK（可选 Redis 字节缓存）拉取参考音频到临时文件。"""
    k = (object_key or "").strip()
    if not k:
        raise ValueError("audio_object_key 为空")
    blob = get_object_bytes_cached(k)
    if not blob:
        raise ValueError("参考音频为空")
    ext = os.path.splitext(k)[1].lower() or ".wav"
    fd, tmp_path = tempfile.mkstemp(prefix="tts_refer_", suffix=ext)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
    return tmp_path, tmp_path


def _materialize_refer_wav_path(path_or_url: str) -> tuple[str, str | None]:
    """把远程 URL 下载为本地临时文件，避免下游把 URL 当本地路径 open()."""
    s = (path_or_url or "").strip()
    if not s:
        raise ValueError("refer_wav_path 为空")
    if not re.match(r"^https?://", s, flags=re.I):
        return s, None

    req = urllib.request.Request(s, headers={"User-Agent": "CubismDemo/tts-ref-fetch"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        blob = resp.read()
    if not blob:
        raise ValueError("参考音频下载为空")

    ext = os.path.splitext(urllib.parse.urlparse(s).path)[1].lower() or ".wav"
    fd, tmp_path = tempfile.mkstemp(prefix="tts_refer_", suffix=ext)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
    return tmp_path, tmp_path


def _cleanup_tts_refer_runtime(runtime: dict | None) -> None:
    if not runtime:
        return
    p = runtime.get("_tmp_refer_wav_path")
    if not p:
        return
    try:
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        logger.warning("清理临时参考音频失败: %s", p, exc_info=True)


def _truncate_mysql_text(raw: str, limit: int = 60000) -> str:
    """TEXT 列最大约 64KB，预留余量避免极端长文本入库失败。"""
    if len(raw) <= limit:
        return raw
    return raw[:limit]


def _persist_raw_memory(
    user_id: int,
    session_key: str,
    package_key: str,
    user_input: str,
    ai_reply: str,
    emotion_tag: str | None = None,
) -> int | None:
    """把本轮原始对话写入 chat_session；返回新行的 ``session_id``，未写入则 ``None``。"""
    user_input = (user_input or "").strip()
    ai_reply = (ai_reply or "").strip()
    if not user_input and not ai_reply:
        return None
    chat_row = ChatSession(
        user_id=user_id,
        package_key=(package_key or "").strip()[:64] or "default",
        user_input=_truncate_mysql_text(user_input),
        ai_reply=_truncate_mysql_text(ai_reply),
        emotion_tag=(((emotion_tag or "").strip()[:30]) or None),
        session_key=(session_key or "").strip()[:64] or "default",
    )
    with connection_ctx(DbConfig.from_env()) as conn:
        return ChatSessionRepository.insert(conn, chat_row)


def _tts_speed_default() -> float:
    try:
        return float(os.getenv("TTS_SPEED", "1.25"))
    except ValueError:
        return 1.25


def _mimo_ws_include_director_enabled() -> bool:
    """MiMo 合并流默认附带导演（仅人设 ``character_desc`` + ``tone_style``）。仅当 ``MIMO_TTS_WS_INCLUDE_DIRECTOR`` 显式为 0/false/off 时关闭。"""
    raw = os.getenv("MIMO_TTS_WS_INCLUDE_DIRECTOR")
    if raw is None:
        return True
    s = str(raw).strip().lower()
    if s == "":
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return s in ("1", "true", "yes", "on")


def _mimo_ws_tts_single_shot_enabled() -> bool:
    """合并朗读模式下，若开启则等 LLM 全文结束后再调一次 MiMo（默认关闭，仍按句分段）。

    音色克隆每次请求都会带上大块参考音频；分段合成会重复上传多次，总耗时与超时概率明显上升。
    开关：``MIMO_TTS_WS_SINGLE_SHOT`` = 1/true/on。
    """
    raw = os.getenv("MIMO_TTS_WS_SINGLE_SHOT")
    if raw is None:
        return False
    s = str(raw).strip().lower()
    return s in ("1", "true", "yes", "on")


_SENTENCE_PUNC = {"。", "！", "？", ".", "!", "?", ";", "；", "，", ","}


def _is_wav_header(blob: bytes) -> bool:
    return len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WAVE"


def _tts_min_chars_before_flush() -> int:
    """遇句末标点时若本段仍短于此字数，暂不送 TTS，继续向 buffer 攒句，减轻「刚播极短一句后长时间等下一段」。"""
    raw = (os.getenv("TTS_MIN_CHARS_PER_CHUNK") or "").strip()
    if raw == "":
        return 8
    try:
        n = int(raw)
    except ValueError:
        return 8
    return max(1, min(200, n))


def _tts_flush_every_n_sentence_end() -> int:
    """累计多少个标点（见 _SENTENCE_PUNC）后，将当前 buffer 整段送 TTS，并清空 buffer / 计数。

    环境变量 ``TTS_FLUSH_EVERY_N_SENTENCE_END``：未设置时 **默认 4**（每 4 个标点一次合成请求）；
    设为 ``1`` 则恢复「每遇到一个标点即一切」（仍受 ``TTS_MIN_CHARS_PER_CHUNK``）；``≥2`` 则按次数攒批
    （攒批路径不再套用最短字数门槛）。
    """
    raw = (os.getenv("TTS_FLUSH_EVERY_N_SENTENCE_END") or "").strip()
    if raw == "":
        return 4
    try:
        n = int(raw)
    except ValueError:
        return 4
    return max(1, min(200, n))


def _tts_parallel_workers() -> int:
    """同一会话内并行 TTS 协程数。MiMo 云端易 429，默认 1；本地 GPT-SoVITS 默认 2。"""
    if normalized_tts_provider() == "mimo":
        raw = (
            os.getenv("TTS_PARALLEL_WORKERS_MIMO")
            or os.getenv("TTS_PARALLEL_WORKERS")
            or ""
        ).strip()
        default_n = 1
    else:
        raw = (os.getenv("TTS_PARALLEL_WORKERS") or "").strip()
        default_n = 2
    if raw == "":
        return default_n
    try:
        n = int(raw)
    except ValueError:
        return default_n
    return max(1, min(8, n))


def _tts_stream_pipeline_slots() -> int | None:
    """流式朗读流水线并行度（可选）。

    文本侧始终 **顺序** 从 LLM 流取 token 写入同一个 ``text_buffer``；攒满切断条件后 **立即**
    ``sentence_queue.put((index, segment))``，**不** ``await`` 合成完成。若 ``TTS_STREAM_PIPELINE_SLOTS=N``，
    则启动 **N** 个 ``tts_worker`` 协程并行 ``to_thread(mimo_tts/…)``；完成顺序任意，出站仍靠
    ``tts_completed`` + ``_tts_flush_ordered`` **严格按 segment index** 推 ``chunk_audio`` + WAV。

    未设置本变量时返回 ``None``，由 ``_tts_parallel_workers()`` 决定路数。
    设置时覆盖 ``TTS_PARALLEL_WORKERS`` / ``TTS_PARALLEL_WORKERS_MIMO``（1～8）。
    """
    raw = (os.getenv("TTS_STREAM_PIPELINE_SLOTS") or "").strip()
    if raw == "":
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return max(1, min(8, n))


def _effective_tts_pipeline_workers() -> int:
    slots = _tts_stream_pipeline_slots()
    if slots is not None:
        return slots
    return _tts_parallel_workers()


def _get_redis_client():
    """懒加载 Redis 客户端；不可用时返回 None（不影响主链路）。"""
    return _redis_factory_get_client(logger)


def _redis_chat_session_cache_key(user_id: int, package_key: str) -> str:
    key_prefix = (
        os.getenv("REDIS_CHAT_SESSION_KEY_PREFIX", "chat_session:recent24h:user").strip()
        or "chat_session:recent24h:user"
    )
    pkg = _normalize_package_key_for_cache(package_key, fallback="default")
    return f"{key_prefix}:{user_id}:{pkg}"


def _redis_short_memory_max_rows() -> int:
    raw = (os.getenv("REDIS_CHAT_LOGIN_MAX_ROWS") or "").strip()
    if not raw:
        return 1000
    try:
        n = int(raw)
    except ValueError:
        return 1000
    return max(1, min(10000, n))


def _redis_short_memory_ttl_seconds() -> int:
    raw = (os.getenv("REDIS_CHAT_LOGIN_TTL_SECONDS") or "").strip()
    if not raw:
        return 86400
    try:
        n = int(raw)
    except ValueError:
        return 86400
    return max(60, n)


def _redis_chat_history_max_messages() -> int:
    raw = (os.getenv("REDIS_CHAT_HISTORY_MAX_MESSAGES") or "").strip()
    if not raw:
        raw = (os.getenv("REDIS_CHAT_LOGIN_MAX_ROWS") or "").strip()
    if not raw:
        return 1000
    try:
        n = int(raw)
    except ValueError:
        return 1000
    return max(20, min(10000, n))


def _chat_system_prompt() -> str:
    base = os.getenv(
        "OLLAMA_CHAT_SYSTEM",
        "你是友好的虚拟主播助手，用自然、口语化的中文与用户对话。",
    )
    guard = os.getenv(
        "OLLAMA_CHAT_OUTPUT_GUARD",
        "输出要求：只回复对用户有帮助的正文内容，不要输出“情绪：xxx”或任何情绪标签行，不要自报系统设定。",
    )
    return f"{base}\n{guard}"


def _package_persona_chat_extra_sync(user_id: int, package_key: str) -> str:
    """从 MySQL 读取当前用户下该模型包的语气风格与角色描述，拼入 system prompt。"""
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
            return "\n\n".join(parts)
    except pymysql.err.ProgrammingError as e:
        code = e.args[0] if e.args else None
        # 1146: 表不存在；1054: Unknown column（persona 未执行 alter_persona_user_package.sql）
        if code in (1146, 1054):
            logger.warning(
                "人设表 persona 缺少包级绑定列或表不可用（errno=%s），已跳过角色定义；请执行 "
                "live2d_db/migrations/alter_persona_user_package.sql",
                code,
            )
            return ""
        logger.exception("读取模型包人设失败 user_id=%s package=%s", user_id, pkg)
        return ""
    except Exception:
        logger.exception("读取模型包人设失败 user_id=%s package=%s", user_id, pkg)
        return ""


def _user_profile_prompt_extra_sync(user_id: int) -> str:
    """MySQL ``user_profile`` 摘要块，供主对话 system 使用。"""
    if user_id < 1 or not chat_inject_enabled():
        return ""
    try:
        with connection_ctx(DbConfig.from_env()) as conn:
            p = UserProfileRepository.get_by_user_id(conn, user_id)
        return format_profile_for_chat_system(p)
    except Exception:
        logger.exception("读取用户画像用于 system 失败 user_id=%s", user_id)
        return ""


def _scene_context_system_block(*, scene_location: str, scene_time: str) -> str:
    """随机背景名（角色所处叙事世界）+ 用户设备真实时间（现实侧），写入主对话 system（空则返回空串）。"""
    loc = (scene_location or "").strip()
    tim = (scene_time or "").strip()
    if not loc and not tim:
        return ""
    parts: list[str] = []
    if loc:
        parts.append(f"角色所在场景（画面背景名，叙事中的世界）：{loc}")
    if tim:
        parts.append(f"用户侧真实时间（发言时刻，现实时间）：{tim}")
    summary = "；".join(parts)
    return (
        "【叙事与现实语境】\n"
        f"{summary}\n"
        "说明：前者对应角色所处虚拟场景；后者为对谈发生时用户设备的本地时刻。"
        "回复时请自然融入情境与时间感，不要生硬复述以上标签句。"
    )


def _chat_system_prompt_for_session(
    user_id: int,
    package_key: str,
    *,
    scene_location: str = "",
    scene_time: str = "",
) -> str:
    """全局聊天设定 + 可选的「当前模型」人设（语气风格 + 角色设定）+ 用户画像 + 场景/时间。"""
    base = _chat_system_prompt()
    if user_id < 1:
        return base
    chunks: list[str] = [base]
    scene_blk = _scene_context_system_block(scene_location=scene_location, scene_time=scene_time)
    if scene_blk:
        chunks.append(scene_blk)
    extra = _package_persona_chat_extra_sync(user_id, package_key)
    if extra:
        chunks.append("【当前模型人设】\n" + extra)
    prof = _user_profile_prompt_extra_sync(user_id)
    if prof:
        chunks.append(prof)
    return "\n\n".join(chunks)


def _mimo_director_role_guide_text(persona_role: str, persona_tone: str) -> str:
    """MiMo user 侧：【人设】【语气】对应库表 character_desc / tone_style（由 system 句约束合成行为）。"""
    role = (persona_role or "").strip()
    tone = (persona_tone or "").strip()
    blocks: list[str] = []
    if role:
        blocks.append("【人设】\n" + role)
    if tone:
        blocks.append("【语气】\n" + tone)
    if not blocks:
        blocks.append(
            "【人设】\n（未配置人设与语气；请自然朗读 assistant 中的台词。）"
        )
    return "\n\n".join(blocks).strip()


def _mimo_director_user_prompt_sync(
    user_id: int,
    package_key: str,
    _current_user_message: str,
    *,
    scene_location: str = "",
    scene_time: str = "",
) -> str:
    """MiMo：user 侧「导演」——【人设】【语气】+ 可选【场景】（与主对话一致）。

    ``_current_user_message`` 保留为调用签名兼容，当前不参与拼接。"""
    persona_role = ""
    persona_tone = ""
    cli = _get_redis_client()
    cached: Optional[Tuple[str, str]] = None
    if cli is not None and user_id >= 1:
        cached = _memory_layers.get_mimo_director_persona_cached(
            cli, user_id, package_key
        )
    if cached is not None:
        persona_role, persona_tone = cached
    else:
        try:
            with connection_ctx(DbConfig.from_env()) as conn:
                row = PersonaRepository.resolve_persona_for_package(
                    conn, user_id, package_key
                )
            if row is not None:
                persona_role = (row.character_desc or "").strip()
                persona_tone = (row.tone_style or "").strip()
            if cli is not None and user_id >= 1:
                _memory_layers.set_mimo_director_persona_cached(
                    cli, user_id, package_key, persona_role, persona_tone
                )
        except Exception:
            logger.exception(
                "读取人设用于 MiMo 导演指令失败 user_id=%s package=%s",
                user_id,
                package_key,
            )

    out = _mimo_director_role_guide_text(persona_role, persona_tone)
    loc = (scene_location or "").strip()
    tim = (scene_time or "").strip()
    if loc or tim:
        bits: list[str] = []
        if loc:
            bits.append(f"角色场景（背景）：{loc}")
        if tim:
            bits.append(f"用户真实时间：{tim}")
        out = "【叙事与现实】" + "；".join(bits) + "\n\n" + out
    try:
        max_total = int(os.getenv("MIMO_TTS_DIRECTOR_MAX_CHARS", "4500"))
    except ValueError:
        max_total = 4500
    max_total = max(800, min(12000, max_total))
    if len(out) > max_total:
        out = out[: max_total - 24].rstrip() + "\n…（导演指令已截断）"
    return out


def _coerce_redis_history_messages(
    payload: object, user_id: int, package_key: str
) -> list[dict]:
    """兼容新格式(role/content)与旧格式(user_input/ai_reply)缓存。"""
    sys_content = (
        _chat_system_prompt_for_session(user_id, package_key, scene_location="", scene_time="")
        if user_id >= 1
        else _chat_system_prompt()
    )
    out: list[dict] = [{"role": "system", "content": sys_content}]
    if not isinstance(payload, list):
        return out
    for item in payload:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role in ("system", "user", "assistant") and content:
            if role == "system" and out:
                # system 固定使用当前后端配置，不用历史中的 system 覆盖
                continue
            out.append({"role": role, "content": content})
            continue
        # 兼容旧缓存结构
        ui = str(item.get("user_input") or "").strip()
        ar = str(item.get("ai_reply") or "").strip()
        if ui:
            out.append({"role": "user", "content": ui})
        if ar:
            out.append({"role": "assistant", "content": ar})
    return out


def _legacy_flat_messages_to_turns(messages: list[dict]) -> list[tuple[str, str, str]]:
    """将 user/assistant 扁平列表转为 (user, assistant, ts) 轮次（尽力合并）。"""
    non_system = [
        m
        for m in messages
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    ]
    ts = datetime.now(timezone.utc).isoformat()
    turns: list[tuple[str, str, str]] = []
    i = 0
    while i < len(non_system):
        if non_system[i].get("role") != "user":
            i += 1
            continue
        u = str(non_system[i].get("content") or "").strip()
        a = ""
        if i + 1 < len(non_system) and non_system[i + 1].get("role") == "assistant":
            a = str(non_system[i + 1].get("content") or "").strip()
            i += 2
        else:
            i += 1
        if u or a:
            turns.append((u, a, ts))
    return turns


def _legacy_try_import_old_redis_string_cache(
    cli: object,
    user_id: int,
    package_key: str,
) -> bool:
    """读取旧版 SET JSON 缓存，迁移到瞬时 List 后删除旧 key。"""
    cache_key = _redis_chat_session_cache_key(user_id, package_key)
    try:
        raw = cli.get(cache_key)
    except Exception:
        logger.exception("读取旧版 Redis 会话缓存失败 key=%s", cache_key)
        return False
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except Exception:
        return False
    msgs = _coerce_redis_history_messages(payload, user_id, package_key)
    flat = [m for m in msgs if isinstance(m, dict)]
    turns = _legacy_flat_messages_to_turns(flat)
    if not turns:
        try:
            cli.delete(cache_key)
        except Exception:
            pass
        return False
    max_turns = _memory_layers.instant_memory_max_turns()
    tail = turns[-max_turns:]
    try:
        _memory_layers.replace_instant_turns(cli, user_id, package_key, tail)
        cli.delete(cache_key)
    except Exception:
        logger.exception("迁移旧版 Redis 会话到瞬时 List 失败 key=%s", cache_key)
        return False
    logger.info("已迁移旧版 Redis 会话缓存至瞬时 List user_id=%s pkg=%s", user_id, package_key)
    return True


def _log_wschat_memory_snapshot(
    *,
    user_id: int,
    package_key: str,
    pkg_norm: str,
    redis_on: bool,
    instant_turns: list[dict[str, str]],
    short_plain: str,
    long_block: str,
    non_system_messages: int,
) -> None:
    """打印本轮送入模型的「可见历史」来源（瞬时/短期/长期），便于对照 MySQL chat_session。"""
    max_turns_cfg = _memory_layers.instant_memory_max_turns()
    logger.info(
        "【/ws/chat 记忆装配】user_id=%s ws_package=%s pkg_norm=%s redis=%s | "
        "瞬时轮数=%s（配置 INSTANT_MEMORY_MAX_TURNS=%s）| 短期块≈%s 字 | 长期块≈%s 字 | "
        "送入模型的非 system 消息条数=%s。"
        "说明：此处不按请求扫描 MySQL；历史来自 Redis（登录时用 chat_session 预热）。",
        user_id,
        package_key,
        pkg_norm,
        "可用" if redis_on else "不可用",
        len(instant_turns),
        max_turns_cfg,
        len(short_plain or ""),
        len(long_block or ""),
        non_system_messages,
    )
    for idx, t in enumerate(instant_turns):
        u = (t.get("u") or "").strip()
        a = (t.get("a") or "").strip()
        logger.info(
            "  瞬时 #%s | user(%s字): %s",
            idx + 1,
            len(u),
            _truncate_mysql_text(u, 400) if u else "(空)",
        )
        logger.info(
            "  瞬时 #%s | assistant(%s字): %s",
            idx + 1,
            len(a),
            _truncate_mysql_text(a, 400) if a else "(空)",
        )


def _build_memory_for_model(
    user_id: int,
    package_key: str,
    *,
    scene_location: str = "",
    scene_time: str = "",
) -> tuple[list[dict], str]:
    """双层记忆 + 长期：system 含长期（若有）、短期缩减块；messages 仅含最近 N 轮瞬时对话。返回 (messages, short_term_plain)。"""
    sys_base = (
        _chat_system_prompt_for_session(
            user_id,
            package_key,
            scene_location=scene_location,
            scene_time=scene_time,
        )
        if user_id >= 1
        else _chat_system_prompt()
    )
    short_plain = ""
    if user_id <= 0:
        return ([{"role": "system", "content": sys_base}], short_plain)

    pkg_norm = _normalize_package_key_for_cache(package_key, fallback="default")
    long_plain = ""
    cli = _get_redis_client()
    if cli is not None:
        long_plain = _memory_layers.read_long_memory_text(cli, user_id, pkg_norm)
    if not long_plain:
        try:
            with connection_ctx() as conn:
                lm = LongMemoryRepository.get_by_user_pkg(conn, user_id, pkg_norm)
            if lm and long_memory_has_any_content(lm):
                long_plain = merge_long_memory_record_for_prompt(lm)
                if cli is not None:
                    _memory_layers.write_long_memory_text(cli, user_id, pkg_norm, long_plain)
        except Exception:
            logger.exception("读取长期记忆失败 user_id=%s pkg=%s", user_id, pkg_norm)
    long_block = _memory_layers.format_long_memory_block(long_plain)

    if cli is None:
        sys_content = sys_base
        if long_block:
            sys_content = (
                f"{sys_base}\n\n【长期记忆（跨日浓缩，跨会话保留）】\n{long_block}"
            )
        logger.info(
            "【/ws/chat 记忆装配】user_id=%s ws_package=%s pkg_norm=%s Redis=不可用 → "
            "无瞬时/短期；若上方长期块非空则仅来自 MySQL→本次读库写入 attempt。",
            user_id,
            package_key,
            pkg_norm,
        )
        return ([{"role": "system", "content": sys_content}], short_plain)

    short_entries = _memory_layers.read_short_entries_newest_first(cli, user_id, package_key)
    short_plain = _memory_layers.format_short_term_block(short_entries)
    instant_turns = _memory_layers.read_instant_turns_chronological(cli, user_id, package_key)

    if not instant_turns and not short_plain:
        _legacy_try_import_old_redis_string_cache(cli, user_id, package_key)
        short_entries = _memory_layers.read_short_entries_newest_first(cli, user_id, package_key)
        short_plain = _memory_layers.format_short_term_block(short_entries)
        instant_turns = _memory_layers.read_instant_turns_chronological(cli, user_id, package_key)

    sys_content = sys_base
    if long_block:
        sys_content = (
            f"{sys_base}\n\n【长期记忆（跨日浓缩，跨会话保留）】\n{long_block}"
        )
    if short_plain:
        sys_content = (
            f"{sys_content}\n\n【短期记忆（近24小时内已缩减摘要）】\n{short_plain}"
        )

    out: list[dict] = [{"role": "system", "content": sys_content}]
    for t in instant_turns:
        u = (t.get("u") or "").strip()
        a = (t.get("a") or "").strip()
        if u:
            out.append({"role": "user", "content": _truncate_mysql_text(u)})
        if a:
            out.append({"role": "assistant", "content": _truncate_mysql_text(a)})
    _log_wschat_memory_snapshot(
        user_id=user_id,
        package_key=package_key,
        pkg_norm=pkg_norm,
        redis_on=True,
        instant_turns=instant_turns,
        short_plain=short_plain,
        long_block=long_block,
        non_system_messages=max(0, len(out) - 1),
    )
    return (out, short_plain)


def _append_turn_memory_layers(
    user_id: int,
    package_key: str,
    user_input: str,
    ai_reply: str,
) -> None:
    """瞬时 List + 挤出轮写入短期规则条。"""
    if user_id <= 0:
        return
    ui = (user_input or "").strip()
    ar = (ai_reply or "").strip()
    if not ui and not ar:
        return
    cli = _get_redis_client()
    if cli is None:
        return
    ts = datetime.now(timezone.utc).isoformat()
    try:
        _memory_layers.append_instant_evict_to_short(cli, user_id, package_key, ui, ar, ts)
    except Exception:
        logger.exception(
            "写入双层记忆失败 user_id=%s package=%s",
            user_id,
            package_key,
        )


def _append_turn_to_redis_history(
    user_id: int,
    package_key: str,
    user_input: str,
    ai_reply: str,
) -> None:
    """兼容旧函数名：每轮结束后更新瞬时 + 短期记忆。"""
    _append_turn_memory_layers(user_id, package_key, user_input, ai_reply)
    if user_id >= 1:
        cli = _get_redis_client()
        if cli is not None:
            try:
                maybe_refresh_user_profile_after_turn(cli, user_id, package_key)
            except Exception:
                logger.exception(
                    "用户画像周期刷新调度异常 user_id=%s package=%s",
                    user_id,
                    package_key,
                )


def ollama_chat_stream_options() -> dict:
    """默认不限制生成长度（不传 num_predict）；仅当设置 OLLAMA_NUM_PREDICT 时作为上限。"""
    raw = (os.getenv("OLLAMA_NUM_PREDICT") or "").strip()
    if raw == "":
        return {}
    try:
        n = int(raw)
    except ValueError:
        return {}
    if n <= 0:
        return {}
    return {"num_predict": max(64, min(32768, n))}


def iter_tokens(text: str):
    pattern = r"[\u4e00-\u9fff]|[A-Za-z]+(?:'[A-Za-z]+)?|\d+|[^\w\s]"
    return re.findall(pattern, text)


def ollama_chat_messages(
    user_message: str,
    history_messages: list[dict] | None = None,
    *,
    user_id: int = 0,
    package_key: str = "",
    scene_location: str = "",
    scene_time: str = "",
) -> list[dict]:
    """聊天消息：system 固定首条，历史按 user/assistant 交替，末尾追加本轮 user。"""
    sys_content = (
        _chat_system_prompt_for_session(
            user_id,
            package_key,
            scene_location=scene_location,
            scene_time=scene_time,
        )
        if user_id >= 1
        else _chat_system_prompt()
    )
    msgs = history_messages[:] if history_messages else [{"role": "system", "content": sys_content}]
    if not msgs or msgs[0].get("role") != "system":
        msgs = [{"role": "system", "content": sys_content}, *msgs]
    msgs.append({"role": "user", "content": user_message})
    return msgs


def ollama_message_content(resp: object) -> str:
    """兼容 ollama 返回 dict 或带 message/content 的对象。"""
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


def fallback_expression_if_empty(ne: list[str]) -> str:
    """单侧缺表情时回退：优先 LIVE2D_FALLBACK_EXPRESSION，否则优先首字符非数字的标识（常见配件层以 1/2 开头）。"""
    if not ne:
        return ""
    env = os.getenv("LIVE2D_FALLBACK_EXPRESSION", "").strip()
    if env:
        if env in ne:
            return env
        got = resolve_expression_id(
            normalize_expression_pick(env), frozenset(ne)
        )
        if got:
            return got
    for n in ne:
        if n and not n[0].isdigit():
            return n
    return ne[0]


def fallback_motion_if_empty(nm: list[str]) -> str:
    """单侧缺动作时回退：优先 LIVE2D_FALLBACK_MOTION，否则优先名称含待机/待/idle 的项，再 catalog 首项。"""
    if not nm:
        return ""
    env = os.getenv("LIVE2D_FALLBACK_MOTION", "").strip()
    if env:
        if env in nm:
            return env
        got = resolve_motion_id(normalize_motion_pick(env), frozenset(nm))
        if got:
            return got
    for n in nm:
        low = n.lower()
        if "待机" in n or "待机动" in n or "idle" in low or "stand" in low:
            return n
    return nm[0]


def parse_action_json(text: str, cat) -> tuple[str, str, str | None]:
    """解析动作 LLM 返回的 JSON；返回 (expression, motion, reason)。"""
    text = (text or "").strip()
    if not text:
        return "", "", None
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
    obj: dict | None = None
    if blob:
        try:
            parsed = json.loads(blob)
            obj = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            obj = None
    if obj is None:
        m_exp = re.search(r'"expression"\s*:\s*"([^"]*)"', text)
        m_mot = re.search(r'"motion"\s*:\s*"([^"]*)"', text)
        m_rea = re.search(r'"reason"\s*:\s*"([^"]*)"', text)
        if m_exp or m_mot:
            obj = {
                "expression": m_exp.group(1) if m_exp else "",
                "motion": m_mot.group(1) if m_mot else "",
            }
            if m_rea:
                obj["reason"] = m_rea.group(1)
    if obj is None:
        return "", "", None

    def _get_reason(o: dict) -> str | None:
        for k in ("reason", "理由", "说明", "分析"):
            if k in o and o[k] is not None:
                s = str(o[k]).strip()
                if s:
                    return s[:1200]
        return None

    reason = _get_reason(obj)

    exp_set = frozenset(cat.expression_names)
    mot_set = frozenset(cat.motion_names)

    def _get_expr(o: dict) -> object:
        for k in ("expression", "表情", "exp", "face"):
            if k in o and o[k] is not None:
                return o[k]
        return None

    def _get_mot(o: dict) -> object:
        for k in ("motion", "动作", "mtn", "anim"):
            if k in o and o[k] is not None:
                return o[k]
        return None

    def _pick_expr(val) -> str:
        s = normalize_expression_pick(val)
        return resolve_expression_id(s, exp_set)

    def _pick_mot(val) -> str:
        s = normalize_motion_pick(val)
        return resolve_motion_id(s, mot_set)

    expr_raw = _get_expr(obj)
    mot_raw = _get_mot(obj)
    ex = _pick_expr(expr_raw)
    mo = _pick_mot(mot_raw)
    if ex or mo:
        return ex, mo, reason
    # 模型常把「动作名」填进 expression、或两键整体对调：用另一键再解一次
    ex_sw = _pick_expr(mot_raw)
    mo_sw = _pick_mot(expr_raw)
    return ex_sw, mo_sw, reason


def _format_instant_turns_for_action_llm(
    turns: list[dict[str, str]],
    *,
    per_line_max: int = 420,
    total_max: int = 2600,
) -> str:
    """将瞬时记忆轮次格式化为动作模型的可读上下文（时间正序）。"""
    if not turns:
        return ""
    lines: list[str] = []
    for i, t in enumerate(turns, start=1):
        u = _truncate_mysql_text((t.get("u") or "").strip(), per_line_max)
        a = _truncate_mysql_text((t.get("a") or "").strip(), per_line_max)
        if u:
            lines.append(f"第{i}轮 用户：{u}")
        if a:
            lines.append(f"第{i}轮 助手：{a}")
    blob = "\n".join(lines).strip()
    if len(blob) > total_max:
        blob = blob[-total_max:].lstrip()
        blob = "…（更早对话已省略）\n" + blob
    return blob


def _action_llm_user_content_sync(user_id: int, package_key: str, user_message: str) -> str:
    """为人设 + 瞬时记忆 + 本轮输入；未登录或与主对话一致地退化为仅本轮句子。"""
    um = (user_message or "").strip()
    if user_id < 1:
        return um
    pkg = (package_key or "").strip()
    if not pkg:
        return um
    persona = _package_persona_chat_extra_sync(user_id, pkg)
    instant_blob = ""
    cli = _get_redis_client()
    if cli is not None:
        instant_blob = _format_instant_turns_for_action_llm(
            _memory_layers.read_instant_turns_chronological(cli, user_id, pkg)
        )
    blocks: list[str] = []
    if persona.strip():
        blocks.append("【人设参考】\n" + persona.strip())
    if instant_blob:
        blocks.append("【最近对话（瞬时记忆）】\n" + instant_blob)
    blocks.append("【本轮用户输入】\n" + um)
    body = "\n\n".join(blocks)
    # 与仅单句输入区分：多段上下文下强调选型对象是 Live2D 角色，不是用户
    if persona.strip() or instant_blob:
        return (
            "【选题对象】以下为会话上下文；你要输出的是 **Live2D 虚拟角色（助手）** 应切换到的表情与动作资源，"
            "用于表现该**角色**对谈话的反应；不要当成用户本人的表情或动作。\n\n"
            + body
        )
    return body


def _infer_expression_motion(
    user_message: str,
    cat,
    *,
    user_id: int = 0,
    package_key: str = "",
) -> tuple[str, str]:
    """动作 LLM（非流式）：返回 (expression, motion)。

    已登录且带上 package_key 时，user 侧会注入 MySQL 人设与 Redis 瞬时对话，与主对话模型一致。
    """
    action_user_content = _action_llm_user_content_sync(user_id, package_key, user_message)
    action_system_text = (
        cat.action_llm_system_text
        + "你要为 **Live2D 虚拟角色（助手）** 选型：依据人设、瞬时记忆中的情境与用户本轮输入，"
        "结合该**角色**的性格与会话氛围，选出角色此刻应表现的表情与动作；勿替真人用户选型。"
        + "表情只能从给定表情列表里选，动作只能从给定动作列表里选。"
        + "输出严格JSON，只返回 expression、motion、reason。"
    )
    action_opts: dict = {
        "num_predict": 512,
        "temperature": 0.1,
        "format": "json",
    }
    try:
        r = client.chat(
            model=OLLAMA_ACTION_MODEL,
            messages=[
                {"role": "system", "content": action_system_text},
                {"role": "user", "content": action_user_content},
            ],
            options=action_opts,
        )
    except Exception as e1:
        logger.warning("动作/表情 LLM（format=json）失败，将重试无 format: %s", e1)
        try:
            r = client.chat(
                model=OLLAMA_ACTION_MODEL,
                messages=[
                    {"role": "system", "content": action_system_text},
                    {"role": "user", "content": action_user_content},
                ],
                options={"num_predict": 512, "temperature": 0.1},
            )
        except Exception as e2:
            logger.warning("动作/表情 LLM 调用失败: %s", e2)
            return "", ""
    try:
        raw = ollama_message_content(r)
    except Exception as e:
        logger.warning("动作/表情 LLM 响应解析失败: %s", e)
        return "", ""
    if not raw:
        logger.warning("动作/表情 LLM 返回空文本，请检查 Ollama 与模型 %s", OLLAMA_ACTION_MODEL)
        return "", ""
    ex, mo, llm_reason = parse_action_json(raw, cat)
    parsed_ex, parsed_mo = ex, mo
    ne, nm = cat.expression_names, cat.motion_names
    fb_on = os.getenv("LIVE2D_ACTION_FALLBACK_IF_EMPTY", "1").lower() in ("1", "true", "yes", "")
    before = (ex, mo)

    if fb_on:
        if ex == "" and mo == "":
            if ne:
                ex = fallback_expression_if_empty(ne)
            if nm:
                mo = fallback_motion_if_empty(nm)
        else:
            # 仅一侧有值时补全另一侧（仍须两边都有有效标识）
            if ex == "" and ne:
                ex = fallback_expression_if_empty(ne)
            if mo == "" and nm:
                mo = fallback_motion_if_empty(nm)

    if ex == "" and mo == "":
        logger.warning(
            "动作/表情 解析后均为空（回退后仍无可用项或 catalog 为空），原始输出(前600字): %s",
            raw[:600].replace("\n", "\\n"),
        )
    fb_applied = (ex, mo) != before and (ex or mo)
    fb_note = f"是 前态={before!r}" if fb_applied else "否"
    reason_snip = (llm_reason or "(JSON 中无 reason/理由 等字段或为空)")[:600]
    logger.info(
        "动作/表情 模型=%s | 原始=%r | 理由=%r | 解析 exp=%r mot=%r | 最终 exp=%r mot=%r | 单侧补全回退=%s",
        OLLAMA_ACTION_MODEL,
        raw.strip().replace("\n", " ")[:500],
        reason_snip,
        parsed_ex,
        parsed_mo,
        ex,
        mo,
        fb_note,
    )
    return ex, mo


def _chunk_json(
    content: str,
    *,
    expression: str | None = None,
    motion: str | None = None,
) -> dict:
    """流式 chunk。动作 LLM 非流式生成的 expression/motion 只拼入第一条聊天 chunk，之后仅 content。"""
    out: dict = {"type": "chunk", "content": content}
    if expression is not None and motion is not None:
        out["expression"] = expression
        out["motion"] = motion
    return out


async def _send_catalog(websocket: WebSocket, cat) -> None:
    """accept 之后立即下发一次，避免每条 chunk 重复携带大列表。"""
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    await _try_send_json(websocket, cat.ws_catalog_message())


"""
WebSocket 聊天接口

请求格式：
    {"message": "你好"}

响应格式：
    - 连接后首条：{"type": "catalog", "package_key": "...", "expression": [...], "motion": [...],
      "expression_paths": [...], "motion_paths": [...]}（各一条，与 LLM 可选范围一致）
    - 流式内容（未配置音色参考时）：首条 chunk 可带 ``expression``/``motion``，后续 chunk 仅 ``content`` 文本增量。
    - 流式内容（``TTS_PROVIDER=mimo`` 且已配置 ``MIMO_API_KEY``，或已配置 GPT-SoVITS 参考音频时）：默认按 **标点攒批**（``TTS_FLUSH_EVERY_N_SENTENCE_END``，默认每 4 个标点一次合成）下发 ``{"type":"chunk_audio",...}``（首条可带 ``expression``/``motion``），**紧接**一条二进制 WAV；合成完成后才推送该段文本与音频。**流水线**：攒满一段即入队，**不等待**该段合成结束即可继续收 token 攒下一段；并行合成路数由 ``TTS_STREAM_PIPELINE_SLOTS``（优先）或 ``TTS_PARALLEL_WORKERS`` / ``TTS_PARALLEL_WORKERS_MIMO`` 决定，前端仍 **按 segment index 递增** 收音频。MiMo 音色克隆可按批重复上传参考音频；若希望整轮只请求一次云端合成，设 ``MIMO_TTS_WS_SINGLE_SHOT=1``（全文结束后单次 ``chunk_audio``）。MiMo 默认附带 **导演指令**（仅 MySQL 人设：``character_desc`` / ``tone_style``，对应【人设】【语气】）；``MIMO_TTS_WS_INCLUDE_DIRECTOR=0`` 可关。
    - 完成标记：{"type": "done"}
    - 错误信息：{"type": "error", "message": "错误描述"}
    - 定时场景（生日、纪念日、考试等）：{"type": "remind_trigger", "trigger_content": …} 中的 ``trigger_content``
      为**触发时点生成**的台词（服务端按 ``session_id``→``chat_session`` 单轮原文与情景描述生成）；库表同名列存情景描述。随后若 MiMo 已配置且未关闭 ``REMIND_TRIGGER_USE_MIMO``，
      可再跟 ``chunk_audio``（``remind_audio``）+ WAV（朗读该生成稿）。

    可选遗留：``/ws/tts`` 仍可容纳连接，但默认前端已不再使用；朗读数据走 ``/ws/chat``。
    客户端切换 Resources 下模型目录时，应对 ``/ws/chat`` 重连并带上 ``?package=<目录名>``，使首条 catalog 与动作 LLM 扫描该包。

流式 Ollama（线程 A 与线程 B）：
    线程 A（事件循环线程）
        - 跑 FastAPI / asyncio：WebSocket 协程、await chunk_queue.get()、
          await websocket.send_json(...) 均在此线程。
        - 阶段 1：创建 loop、asyncio.Queue、pump_error。
        - 阶段 3：从队列异步取文本，连接正常则推给浏览器。
        - 阶段 4：await producer 等待线程 B 结束；若有 pump_error 则抛出；最后发 type=done。

    线程 B（线程池工作线程，asyncio.to_thread(_pump_stream_to_queue)）
        - 阶段 2：同步 client.chat(..., stream=True) 与 for chunk in stream（阻塞读 Ollama）。
        - 每段 content 经 run_coroutine_threadsafe(chunk_queue.put(content), loop).result()
          投递到线程 A 执行 put；线程 B 阻塞到 put 完成后再读下一 chunk。
        - finally 中 put(None)，通知线程 A 消费循环「流结束」。

    二者配合
        - B → A：阻塞读模型只在 B；往 asyncio.Queue 写必须在 A 的 loop 上（协程 put）。
        - A：await get() 取文后 await send_json，避免在循环里长时间同步读 Ollama。

    一句话：线程 B = 同步拉 Ollama 流并写入队列；线程 A = 异步从队列取出并推到 WebSocket。
"""


@router.websocket("/ws/chat")
async def chat_websocket(websocket: WebSocket):
    """处理 WebSocket 连接，接收用户消息并流式返回 AI 回复
    
    :param websocket: WebSocket 连接
        websocket 是 FastAPI 注入的参数
        它的类型是 WebSocket（来自 Starlette）
        每次有客户端连接 /ws/chat，都会新建一个 WebSocket 实例并传入该函数
    """
    await websocket.accept()
    chat_session = _session_id_from_websocket(websocket)
    chat_user_id = _user_id_from_websocket(websocket)
    async with _session_lock:
        _session_chat_ws[chat_session] = websocket
        _register_chat_ws_for_user(chat_user_id, websocket)
    live_pkg = _live2d_package_from_websocket(websocket)
    session_catalog = get_catalog_for_package(live_pkg, user_id=chat_user_id)
    tts_refer_runtime = None
    tts_refer_runtime = await asyncio.to_thread(
        _load_tts_refer_runtime, chat_user_id, session_catalog.package_key
    )
    _ttp = normalized_tts_provider()
    if _ttp == "mimo":
        _tts_hint = (
            " MiMo 已配置（预置音色或参考音频样本克隆，见 utils/tts.py / .env）"
            if mimo_tts_configured()
            else " MiMo 未配置 MIMO_API_KEY，本轮无朗读"
        )
    else:
        _tts_hint = (
            ""
            if tts_refer_runtime
            else "；未配置音色参考，本轮将不调 GPT-SoVITS（可在上传页绑定参考音频，或为 api.py 指定 -dr/-dt/-dl）"
        )
    logger.info(
        "✅ 客户端建立 WebSocket 连接，session=%s user_id=%s package=%s（表情=%d 动作=%d refer=%s tts=%s）%s",
        chat_session,
        chat_user_id,
        session_catalog.package_key,
        len(session_catalog.expression_paths),
        len(session_catalog.motion_paths),
        "on" if tts_refer_runtime else "off",
        _ttp,
        _tts_hint,
    )
    await _send_catalog(websocket, session_catalog)
    await flush_pending_reminders_for_connection(websocket, chat_user_id)

    async def _chat_stream_invalid() -> bool:
        if websocket.client_state != WebSocketState.CONNECTED:
            return True
        async with _session_lock:
            return _session_chat_ws.get(chat_session) is not websocket

    try:
        while True:
            # 修复：用枚举值判断连接状态（替代 disconnected 属性）
            if await _chat_stream_invalid():
                break
            
            # 接收用户消息（文本与朗读均在 /ws/chat）
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                logger.info("ℹ️ /ws/chat receive_json 期间客户端断开，结束会话循环")
                break
            except RuntimeError as e:
                msg = str(e).lower()
                if "not connected" in msg or "need to call \"accept\" first" in msg:
                    logger.info("ℹ️ /ws/chat 已非连接态，结束会话循环")
                    break
                raise
            user_message = data.get("message", "").strip()
            scene_location = str(
                data.get("scene_location") or data.get("scene_label") or ""
            ).strip()
            scene_time = str(data.get("scene_time") or "").strip()

            # 空消息过滤
            if not user_message:
                ok = await _try_send_json(websocket, {
                    "type": "error", 
                    "message": "消息内容不能为空"
                })
                if not ok:
                    logger.info("ℹ️ 客户端已断开（空消息提示未发送）")
                    break
                continue

            # -------------------------------------------------------------------------
            # Ollama 流式推送：同步阻塞的模型调用 → 异步 WebSocket 推送
            #
            # 设计目标：不阻塞 asyncio 事件循环（线程 A），把 client.chat(stream=True) 的同步
            # 迭代放到线程 B；两者用 asyncio.Queue 桥接；跨线程写队列用 run_coroutine_threadsafe。
            #
            # 整体流程（初始化 → 生产 → 消费 → 收尾）：
            #   初始化：事件循环 loop / 队列 chunk_queue / 异常列表 pump_error
            #   生产：to_thread 在线程 B 跑 _pump_stream_to_queue（Ollama 同步流）
            #   消费：线程 A 上协程 await chunk_queue.get() → await send_json
            #   收尾：await producer；检查 pump_error；发 type=done
            # -------------------------------------------------------------------------
            try:
                history_messages, _ = await asyncio.to_thread(
                    _build_memory_for_model,
                    chat_user_id,
                    session_catalog.package_key,
                    scene_location=scene_location,
                    scene_time=scene_time,
                )
                # MiMo 合并流默认附带导演：【人设】【语气】仅人设字段（Redis/MySQL），不含【场景】与通用说明。
                # 显式 ``MIMO_TTS_WS_INCLUDE_DIRECTOR=0`` 可关闭。
                _mimo_ws_include_director = _mimo_ws_include_director_enabled()
                mimo_director_user_prompt = ""
                if (
                    _mimo_ws_include_director
                    and normalized_tts_provider() == "mimo"
                    and mimo_tts_configured()
                ):
                    mimo_director_user_prompt = await asyncio.to_thread(
                        _mimo_director_user_prompt_sync,
                        chat_user_id,
                        session_catalog.package_key,
                        user_message,
                        scene_location=scene_location,
                        scene_time=scene_time,
                    )
                expr, mot = await asyncio.to_thread(
                    _infer_expression_motion,
                    user_message,
                    session_catalog,
                    user_id=chat_user_id,
                    package_key=session_catalog.package_key,
                )
                _ttp = normalized_tts_provider()
                if _ttp == "mimo":
                    merged_stream = mimo_tts_configured()
                else:
                    merged_stream = bool(
                        tts_refer_runtime
                        and tts_refer_runtime.get("refer_wav_path")
                        and tts_refer_runtime.get("prompt_text")
                        and tts_refer_runtime.get("prompt_language")
                    )
                _mimo_ws_single_shot = (
                    merged_stream
                    and _ttp == "mimo"
                    and _mimo_ws_tts_single_shot_enabled()
                )
                first_visible_actions_sent = False
                ai_reply_chunks: list[str] = []
                # ----- 阶段 1：初始化（均在事件循环所在线程 A，异步上下文） -----
                # 当前运行中的事件循环，供线程 B 通过 run_coroutine_threadsafe 把 put 投递回 A。
                loop = asyncio.get_running_loop()
                # 线程 B 生产 chunk、线程 A 消费 chunk 的异步队列（put/get 必须在 loop 上 await）。
                chunk_queue: asyncio.Queue = asyncio.Queue()
                # 线程 B 内若抛错，写入此列表；列表引用在闭包间共享，由阶段 4 在线程 A 读取并抛出。
                pump_error: list[BaseException] = []
                stop_requested = threading.Event()

                def _pump_stream_to_queue() -> None:
                    """阶段 2：生产者（在线程 B 同步执行）。

                    client.chat(..., stream=True) 与 for chunk in stream 均为阻塞调用，必须放在
                    线程池线程中，避免占满事件循环。每拿到一段 content，通过 run_coroutine_threadsafe
                    把 chunk_queue.put 调度到线程 A 执行；.result() 阻塞线程 B 直至 put 完成，避免丢数据。
                    """
                    try:
                        # 线程 B：同步阻塞，等待 Ollama 建立流并开始推理。
                        _chat_kw: dict = {
                            "model": OLLAMA_MODEL,
                            "messages": ollama_chat_messages(
                                user_message,
                                history_messages=history_messages,
                                user_id=chat_user_id,
                                package_key=session_catalog.package_key,
                                scene_location=scene_location,
                                scene_time=scene_time,
                            ),
                            "stream": True,
                        }
                        _opts = ollama_chat_stream_options()
                        if _opts:
                            _chat_kw["options"] = _opts
                        stream = client.chat(**_chat_kw)
                        # 线程 B：同步遍历流；每步可能阻塞等待下一 token。
                        for chunk in stream:
                            if stop_requested.is_set():
                                break
                            content = chunk.get("message", {}).get("content", "")
                            if content:
                                # 线程 B 发起，put 实际在线程 A 的事件循环里执行。
                                asyncio.run_coroutine_threadsafe(
                                    chunk_queue.put(content), loop
                                ).result()
                    except BaseException as e:
                        pump_error.append(e)
                    finally:
                        # 无论成功或异常，向队列放入 None，供消费协程结束 while（阶段 3.2）。
                        asyncio.run_coroutine_threadsafe(
                            chunk_queue.put(None), loop
                        ).result()

                # create_task(to_thread(...))：在线程 B 执行 _pump_stream_to_queue；线程 A 不阻塞，
                # 可立即进入阶段 3；producer 用于阶段 4 await，确保线程 B 完全结束。
                producer = asyncio.create_task(asyncio.to_thread(_pump_stream_to_queue))

                tts_speed = _tts_speed_default()
                tts_lang = os.getenv("TTS_TEXT_LANGUAGE", "zh")
                tts_workers_n = _effective_tts_pipeline_workers()

                text_buffer: list[str] = []
                tts_sentence_end_punc_count = 0
                tts_sentence_index = 0  # 首个 flush 后变为 1（与 tts_next_send 对齐）
                sentence_queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
                # 切段序号（1..N）与合成完成结果；出站按 next_send 严格递增。
                # 文本协程仅 await put（非阻塞合成）；多 tts_worker 并行 to_thread，无需「等音频」再继续 token。
                tts_completed: dict[int, tuple[str, bytes]] = {}
                tts_completed_mono: dict[int, float] = {}
                tts_next_send = 1
                tts_order_lock = asyncio.Lock()
                tts_send_lock = asyncio.Lock()

                async def _tts_flush_ordered() -> None:
                    """按 index 递增：合并模式下经本连接发 chunk_audio+WAV 或仅 chunk；否则丢弃 TTS 结果（文本已流式发出）。"""
                    nonlocal tts_next_send, first_visible_actions_sent
                    async with tts_send_lock:
                        while True:
                            if await _chat_stream_invalid():
                                return
                            async with tts_order_lock:
                                if tts_next_send not in tts_completed:
                                    return
                                text, wav = tts_completed[tts_next_send]
                                cur_idx = tts_next_send
                            completed_mono = tts_completed_mono.pop(
                                cur_idx, None
                            )
                            if merged_stream:
                                if not wav or not _is_wav_header(wav):
                                    if wav:
                                        logger.warning(
                                            "TTS 分段非有效 WAV，仅推送文本 session=%s index=%s bytes=%s",
                                            chat_session,
                                            cur_idx,
                                            len(wav),
                                        )
                                    else:
                                        logger.warning(
                                            "TTS 分段为空，仅推送文本 session=%s index=%s",
                                            chat_session,
                                            cur_idx,
                                        )
                                    if tts_debug_enabled() and completed_mono is not None:
                                        logger.info(
                                            "[TTS_DEBUG] wschat_send session=%s index=%s "
                                            "wait_after_synth_ms=%.1f text_chars=%d wav_bytes=0 merged=text_only",
                                            chat_session,
                                            cur_idx,
                                            (time.perf_counter() - completed_mono)
                                            * 1000,
                                            len(text),
                                        )
                                    payload: dict = {
                                        "type": "chunk",
                                        "content": text,
                                    }
                                    if not first_visible_actions_sent:
                                        payload["expression"] = expr
                                        payload["motion"] = mot
                                        first_visible_actions_sent = True
                                    ok = await _try_send_json(websocket, payload)
                                    if not ok:
                                        return
                                    async with tts_order_lock:
                                        if (
                                            tts_next_send == cur_idx
                                            and cur_idx in tts_completed
                                        ):
                                            del tts_completed[cur_idx]
                                            tts_next_send = cur_idx + 1
                                    continue
                                if tts_debug_enabled() and completed_mono is not None:
                                    logger.info(
                                        "[TTS_DEBUG] wschat_send session=%s index=%s "
                                        "wait_after_synth_ms=%.1f text_chars=%d wav_bytes=%d",
                                        chat_session,
                                        cur_idx,
                                        (time.perf_counter() - completed_mono)
                                        * 1000,
                                        len(text),
                                        len(wav),
                                    )
                                audio_meta: dict = {
                                    "type": "chunk_audio",
                                    "content": text,
                                    "index": cur_idx,
                                    "size": len(wav),
                                }
                                if not first_visible_actions_sent:
                                    audio_meta["expression"] = expr
                                    audio_meta["motion"] = mot
                                    first_visible_actions_sent = True
                                ok = await _try_send_json(websocket, audio_meta)
                                if not ok:
                                    return
                                ok2 = await _try_send_bytes(websocket, wav)
                                if not ok2:
                                    return
                                async with tts_order_lock:
                                    if (
                                        tts_next_send == cur_idx
                                        and cur_idx in tts_completed
                                    ):
                                        del tts_completed[cur_idx]
                                        tts_next_send = cur_idx + 1
                                continue
                            if wav and not _is_wav_header(wav):
                                logger.warning(
                                    "TTS 分段无效（非合并流已发字，丢弃音频）session=%s index=%s bytes=%s",
                                    chat_session,
                                    cur_idx,
                                    len(wav),
                                )
                            if tts_debug_enabled() and completed_mono is not None:
                                logger.info(
                                    "[TTS_DEBUG] wschat_send session=%s index=%s non_merged_drop",
                                    chat_session,
                                    cur_idx,
                                )
                            async with tts_order_lock:
                                if (
                                    tts_next_send == cur_idx
                                    and cur_idx in tts_completed
                                ):
                                    del tts_completed[cur_idx]
                                    tts_next_send = cur_idx + 1

                async def tts_worker() -> None:
                    while True:
                        if await _chat_stream_invalid():
                            break
                        item = await sentence_queue.get()
                        if item is None:
                            break
                        idx, sentence = item
                        if await _chat_stream_invalid():
                            break
                        refer_ok = (
                            bool(tts_refer_runtime)
                            and bool(tts_refer_runtime.get("refer_wav_path"))
                            and bool(tts_refer_runtime.get("prompt_text"))
                            and bool(tts_refer_runtime.get("prompt_language"))
                        )
                        _wp = normalized_tts_provider()
                        if _wp == "mimo":
                            if not mimo_tts_configured():
                                wav = b""
                            else:
                                _mimo_wall_t0 = time.perf_counter()
                                if tts_debug_enabled():
                                    logger.info(
                                        "[TTS_DEBUG] wschat_mimo_tts_begin session=%s "
                                        "segment_index=%s sentence_chars=%s workers=%s",
                                        chat_session,
                                        idx,
                                        len(sentence),
                                        tts_workers_n,
                                    )
                                try:
                                    wav = await asyncio.to_thread(
                                        mimo_tts,
                                        sentence,
                                        text_language=tts_lang,
                                        refer_runtime=tts_refer_runtime,
                                        user_director_prompt=mimo_director_user_prompt
                                        or None,
                                        speech_assistant_only=not _mimo_ws_include_director,
                                        merge_env_user_prompts=not _mimo_ws_include_director,
                                    )
                                except ValueError as _mimo_ve:
                                    logger.warning(
                                        "MiMo 参考音频无效 session=%s index=%s: %s",
                                        chat_session,
                                        idx,
                                        _mimo_ve,
                                    )
                                    wav = b""
                                except RuntimeError as _mimo_ex:
                                    _ms = str(_mimo_ex)
                                    if "MiMo API HTTP 429" in _ms:
                                        logger.warning(
                                            "MiMo TTS 仍被限流(429)，已降级为仅文本 session=%s index=%s",
                                            chat_session,
                                            idx,
                                        )
                                    elif (
                                        "MiMo API HTTP 400" in _ms
                                        and "audio format" in _ms.lower()
                                    ):
                                        logger.warning(
                                            "MiMo 拒绝参考音频编码 session=%s index=%s（官方仅 wav/mp3）",
                                            chat_session,
                                            idx,
                                        )
                                    else:
                                        logger.exception(
                                            "MiMo TTS 合成失败 session=%s index=%s",
                                            chat_session,
                                            idx,
                                        )
                                    wav = b""
                                except Exception:
                                    logger.exception(
                                        "MiMo TTS 合成失败 session=%s index=%s",
                                        chat_session,
                                        idx,
                                    )
                                    wav = b""
                                if tts_debug_enabled():
                                    logger.info(
                                        "[TTS_DEBUG] wschat_mimo_tts_fin session=%s "
                                        "segment_index=%s wall_ms=%.1f wav_bytes=%d",
                                        chat_session,
                                        idx,
                                        (time.perf_counter() - _mimo_wall_t0)
                                        * 1000,
                                        len(wav),
                                    )
                        elif not refer_ok:
                            wav = b""
                        else:
                            try:
                                wav = await asyncio.to_thread(
                                    gpt_sovits_tts,
                                    sentence,
                                    text_language=tts_lang,
                                    speed=tts_speed,
                                    refer_wav_path=tts_refer_runtime.get(
                                        "refer_wav_path"
                                    ),
                                    prompt_text=tts_refer_runtime.get(
                                        "prompt_text"
                                    ),
                                    prompt_language=tts_refer_runtime.get(
                                        "prompt_language"
                                    ),
                                )
                            except Exception:
                                logger.exception(
                                    "TTS 合成失败 session=%s index=%s",
                                    chat_session,
                                    idx,
                                )
                                wav = b""
                        async with tts_order_lock:
                            tts_completed[idx] = (sentence, wav)
                        await _tts_flush_ordered()

                tts_tasks = [
                    asyncio.create_task(tts_worker()) for _ in range(tts_workers_n)
                ]

                try:
                    # ----- 阶段 3：消费队列并推送 WebSocket（线程 A，异步） -----
                    first_chunk = True
                    while True:
                        # 队列为空时挂起本协程，事件循环可处理其它连接；有数据时被唤醒。
                        content = await chunk_queue.get()
                        if content is None:
                            break
                        if await _chat_stream_invalid():
                            stop_requested.set()
                            logger.warning("⚠️ 当前 chat 会话已失效，停止旧轮推送")
                            break
                        ai_reply_chunks.append(content)
                        if websocket.client_state != WebSocketState.CONNECTED:
                            logger.warning("⚠️ 客户端已断开，停止推送消息")
                            stop_requested.set()
                            break
                        if not merged_stream:
                            if first_chunk:
                                ok = await _try_send_json(
                                    websocket,
                                    _chunk_json(
                                        content, expression=expr, motion=mot
                                    ),
                                )
                                if not ok:
                                    logger.info("ℹ️ 客户端已断开（首段 chunk 未发送）")
                                    break
                                first_chunk = False
                            else:
                                ok = await _try_send_json(
                                    websocket, _chunk_json(content)
                                )
                                if not ok:
                                    logger.info("ℹ️ 客户端已断开（chunk 未发送）")
                                    break

                        if _mimo_ws_single_shot:
                            continue

                        min_chars = _tts_min_chars_before_flush()
                        every_n_end = _tts_flush_every_n_sentence_end()
                        for tk in iter_tokens(content):
                            text_buffer.append(tk)
                            if tk in _SENTENCE_PUNC:
                                tts_sentence_end_punc_count += 1

                            if every_n_end <= 1:
                                flush_by_punc = tk in _SENTENCE_PUNC
                                if not flush_by_punc:
                                    continue
                                sentence = "".join(text_buffer).strip()
                                if not sentence:
                                    continue
                                if flush_by_punc and len(sentence) < min_chars:
                                    continue
                            else:
                                flush_by_batch = (
                                    tts_sentence_end_punc_count >= every_n_end
                                )
                                if not flush_by_batch:
                                    continue
                                sentence = "".join(text_buffer).strip()
                                if not sentence:
                                    continue

                            # 本段已送 TTS：清空缓冲与句末标点计数，后续 token 重新攒
                            text_buffer.clear()
                            tts_sentence_end_punc_count = 0
                            tts_sentence_index += 1
                            await sentence_queue.put(
                                (tts_sentence_index, sentence)
                            )

                    if not await _chat_stream_invalid() and text_buffer:
                        sentence = "".join(text_buffer).strip()
                        if sentence:
                            tts_sentence_index += 1
                            await sentence_queue.put((tts_sentence_index, sentence))
                        text_buffer.clear()
                        tts_sentence_end_punc_count = 0

                    if (
                        _mimo_ws_single_shot
                        and merged_stream
                        and not await _chat_stream_invalid()
                    ):
                        _full_for_tts = "".join(ai_reply_chunks).strip()
                        if _full_for_tts:
                            if tts_debug_enabled():
                                logger.info(
                                    "[TTS_DEBUG] wschat_mimo_tts_single_shot_enqueue "
                                    "session=%s text_chars=%d",
                                    chat_session,
                                    len(_full_for_tts),
                                )
                            await sentence_queue.put((1, _full_for_tts))
                finally:
                    for _ in range(tts_workers_n):
                        await sentence_queue.put(None)
                    try:
                        await asyncio.gather(*tts_tasks)
                    except WebSocketDisconnect:
                        pass
                    except RuntimeError as e:
                        if "close message" not in str(e).lower():
                            raise
                    # ----- 阶段 4：收尾（线程 A） -----
                    stop_requested.set()
                    await producer

                if pump_error:
                    raise pump_error[0]

                ai_reply_full = "".join(ai_reply_chunks).strip()
                redis_write_task = asyncio.create_task(
                    asyncio.to_thread(
                        _append_turn_to_redis_history,
                        chat_user_id,
                        session_catalog.package_key,
                        user_message,
                        ai_reply_full,
                    )
                )

                def _on_redis_write_done(task: asyncio.Task) -> None:
                    try:
                        task.result()
                    except Exception:
                        logger.exception(
                            "后台写 Redis 短期记忆任务异常 user_id=%s session=%s package=%s",
                            chat_user_id,
                            chat_session,
                            session_catalog.package_key,
                        )

                redis_write_task.add_done_callback(_on_redis_write_done)
                turn_session_id: int | None = None
                try:
                    turn_session_id = await asyncio.to_thread(
                        _persist_raw_memory,
                        chat_user_id,
                        chat_session,
                        session_catalog.package_key,
                        user_message,
                        ai_reply_full,
                        None,
                    )
                except Exception:
                    logger.exception(
                        "原始记忆入库失败 user_id=%s session=%s",
                        chat_user_id,
                        chat_session,
                    )
                else:
                    _um = (user_message or "").strip()
                    _arf = (ai_reply_full or "").strip()
                    if _um and _arf:

                        def _run_turn_remind_extract() -> None:
                            try:
                                from utils.remind_extract import extract_and_persist_reminders

                                extract_and_persist_reminders(
                                    chat_user_id,
                                    _um,
                                    _arf,
                                    package_key=session_catalog.package_key,
                                    session_id=turn_session_id,
                                )
                            except Exception:
                                logger.exception(
                                    "异步抽取定时关怀失败 user_id=%s session=%s",
                                    chat_user_id,
                                    chat_session,
                                )

                        _turn_extract_task = asyncio.create_task(
                            asyncio.to_thread(_run_turn_remind_extract),
                            name=f"remind_extract_u{chat_user_id}",
                        )

                        def _on_turn_extract_done(task: asyncio.Task) -> None:
                            try:
                                task.result()
                            except Exception:
                                logger.exception(
                                    "异步抽取定时关怀任务收尾异常 user_id=%s",
                                    chat_user_id,
                                )

                        _turn_extract_task.add_done_callback(_on_turn_extract_done)

                if not await _chat_stream_invalid():
                    ok = await _try_send_json(websocket, {"type": "done"})
                    if not ok:
                        logger.info("ℹ️ 客户端已断开（done 未发送）")

            except Exception as ollama_e:
                # Ollama 调用异常处理
                logger.error(f"❌ Ollama 调用失败：{str(ollama_e)}")
                if not await _chat_stream_invalid():
                    ok = await _try_send_json(websocket, {
                        "type": "error", 
                        "message": f"AI 服务调用失败：{str(ollama_e)}"
                    })
                    if not ok:
                        logger.info("ℹ️ 客户端已断开（错误消息未发送）")

    # 捕获客户端正常断开异常（预期内，无需报错）
    except WebSocketDisconnect as e:
        logger.info(f"ℹ️ 客户端正常断开 WebSocket 连接，断开码：{e.code}")
        return  # 直接返回，不执行后续的 send/close

    # 捕获其他业务异常
    except Exception as e:
        logger.error(f"❌ WebSocket 业务异常：{str(e)}", exc_info=True)
        # 仅当连接存活时，发送错误消息
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await _try_send_json(websocket, {
                    "type": "error", 
                    "message": f"服务端异常：{str(e)}"
                })
        except Exception:
            pass  # 发送失败忽略

    # 最终兜底（确保连接关闭，但先检查状态）
    finally:
        async with _session_lock:
            if _session_chat_ws.get(chat_session) is websocket:
                _session_chat_ws.pop(chat_session, None)
            _unregister_chat_ws_for_user(chat_user_id, websocket)
        _cleanup_tts_refer_runtime(tts_refer_runtime)
        if chat_user_id >= 1:
            _cli_pf = _get_redis_client()
            if _cli_pf is not None:
                _pkg_pf = session_catalog.package_key
                try:
                    await asyncio.to_thread(
                        refresh_user_profile_on_disconnect,
                        _cli_pf,
                        chat_user_id,
                        _pkg_pf,
                    )
                except Exception:
                    logger.exception(
                        "关页断开触发的用户画像刷新异常 user_id=%s package=%s",
                        chat_user_id,
                        _pkg_pf,
                    )
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()
                logger.info("🔌 主动关闭 WebSocket 连接")
            else:
                logger.info("🔌 WebSocket 连接已关闭")
        except Exception:
            pass


@router.websocket("/ws/tts")
async def chat_tts_websocket(websocket: WebSocket):
    """
    兼容旧客户端：曾用于单独接收音频。当前朗读已由 ``/ws/chat`` 以 ``chunk_audio`` + 二进制帧下发。
    """
    await websocket.accept()
    sid = _session_id_from_websocket(websocket)
    async with _session_lock:
        _session_tts_ws[sid] = websocket
    logger.info("✅ /ws/tts 已注册（仅收服务端推送音频），session=%s", sid)

    try:
        while True:
            if websocket.client_state != WebSocketState.CONNECTED:
                break
            # 保持连接；客户端不必发送任何内容（收到断开即退出）
            await websocket.receive()
    except WebSocketDisconnect as e:
        logger.info("ℹ️ /ws/tts 断开 session=%s 码=%s", sid, e.code)
    except Exception as e:
        logger.error("❌ /ws/tts 异常 session=%s：%s", sid, e, exc_info=True)
    finally:
        async with _session_lock:
            if _session_tts_ws.get(sid) is websocket:
                _session_tts_ws.pop(sid, None)
        logger.info("🔌 /ws/tts 已注销 session=%s", sid)
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()
        except Exception:
            pass
