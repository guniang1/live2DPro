"""
实时语音识别：浏览器 PCM → 阿里云 DashScope Fun-ASR（通义/百炼）。
需设置环境变量 DASHSCOPE_API_KEY，勿将密钥写入代码仓库。

依赖: pip install dashscope
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

logger = logging.getLogger(__name__)
router = APIRouter()

try:
    import dashscope
    from dashscope.audio.asr import (
        Recognition,
        RecognitionCallback,
        RecognitionResult,
    )

    _DASHSCOPE_ASR_AVAILABLE = True
except ImportError:
    dashscope = None  # type: ignore
    Recognition = None  # type: ignore
    RecognitionCallback = object  # type: ignore
    RecognitionResult = None  # type: ignore
    _DASHSCOPE_ASR_AVAILABLE = False


def _dashscope_api_key() -> str | None:
    """百炼 API Key：环境变量 DASHSCOPE_API_KEY（由 main.py 先 load_dotenv）。"""
    return os.environ.get("DASHSCOPE_API_KEY")

class _DashScopeAsrCallback(RecognitionCallback):
    """将 DashScope 回调转到前端 WebSocket（线程安全）。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, websocket: WebSocket) -> None:
        self._loop = loop
        self._ws = websocket

    def _schedule_send(self, partial: bool, text: str) -> None:
        async def _send() -> None:
            if self._ws.client_state != WebSocketState.CONNECTED:
                return
            try:
                await self._ws.send_json({"partial": partial, "text": text})
            except Exception as e:
                logger.warning("向客户端发送识别结果失败: %s", e)

        asyncio.run_coroutine_threadsafe(_send(), self._loop)

    def on_event(self, result: RecognitionResult) -> None:
        sentence = result.get_sentence()
        if sentence is None:
            return
        if isinstance(sentence, list):
            if not sentence:
                return
            sentence = sentence[-1]
        if not isinstance(sentence, dict):
            return
        text = (sentence.get("text") or "").strip()
        try:
            is_sentence_end = RecognitionResult.is_sentence_end(sentence)
        except Exception:
            is_sentence_end = False
        # 句末为最终结果；否则为中间结果
        self._schedule_send(not is_sentence_end, text)

    def on_error(self, result: RecognitionResult) -> None:
        msg = getattr(result, "message", str(result))
        logger.error("DashScope ASR 错误: %s", msg)

        async def _send_err() -> None:
            if self._ws.client_state == WebSocketState.CONNECTED:
                try:
                    await self._ws.send_json({"error": str(msg)})
                except Exception:
                    pass

        asyncio.run_coroutine_threadsafe(_send_err(), self._loop)

    def on_complete(self) -> None:
        logger.info("DashScope ASR 会话 on_complete")

    def on_open(self) -> None:
        logger.info("DashScope ASR 连接已建立")


def _run_recognition_worker(
    audio_q: queue.Queue,
    loop: asyncio.AbstractEventLoop,
    websocket: WebSocket,
    api_key: str,
) -> None:
    dashscope.api_key = api_key
    dashscope.base_websocket_api_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

    cb = _DashScopeAsrCallback(loop, websocket)
    recognition = Recognition(
        model="fun-asr-realtime",
        format="pcm",
        sample_rate=16000,
        semantic_punctuation_enabled=False,
        callback=cb,
    )
    try:
        recognition.start()
    except Exception as e:
        logger.exception("DashScope recognition.start 失败: %s", e)
        asyncio.run_coroutine_threadsafe(
            _send_error_safe(websocket, str(e)),
            loop,
        )
        return
    try:
        while True:
            chunk = audio_q.get()
            if chunk is None:
                break
            recognition.send_audio_frame(chunk)
    except Exception as e:
        logger.exception("发送音频帧失败: %s", e)
        try:
            asyncio.run_coroutine_threadsafe(
                _send_error_safe(websocket, str(e)),
                loop,
            )
        except Exception:
            pass
    finally:
        try:
            recognition.stop()
        except Exception as e:
            logger.warning("recognition.stop() 异常: %s", e)


async def _send_error_safe(websocket: WebSocket, message: str) -> None:
    if websocket.client_state == WebSocketState.CONNECTED:
        await websocket.send_json({"error": message})


@router.websocket("/ws/asr")
async def dashscope_asr_websocket(websocket: WebSocket) -> None:
    """接收 16kHz 16bit 单声道 PCM，经 DashScope 实时识别，返回 JSON {partial,text}。"""
    await websocket.accept()

    if not _DASHSCOPE_ASR_AVAILABLE:
        await websocket.send_json(
            {"error": "未安装 dashscope，请执行: pip install dashscope"}
        )
        await websocket.close()
        return

    api_key = _dashscope_api_key()
    if not api_key:
        await websocket.send_json(
            {
                "error": "未设置 DASHSCOPE_API_KEY。请在系统环境变量或 .env 中配置百炼 API Key。"
            }
        )
        await websocket.close()
        return

    loop = asyncio.get_running_loop()
    audio_q: queue.Queue = queue.Queue()

    worker = threading.Thread(
        target=_run_recognition_worker,
        args=(audio_q, loop, websocket, api_key),
        daemon=True,
    )
    worker.start()

    try:
        while True:
            if websocket.client_state != WebSocketState.CONNECTED:
                break
            raw = await websocket.receive()
            if raw["type"] == "websocket.disconnect":
                break
            data = raw.get("bytes")
            if data:
                audio_q.put(data)
                continue
            txt = raw.get("text")
            if txt is not None:
                try:
                    cmd = json.loads(txt)
                except json.JSONDecodeError:
                    continue
                if cmd.get("cmd") == "flush":
                    audio_q.put(None)
                    break
    except WebSocketDisconnect as e:
        logger.info("ASR WebSocket 断开: %s", e.code)
    except Exception as e:
        logger.error("ASR WebSocket 异常: %s", e, exc_info=True)
    finally:
        audio_q.put(None)
        worker.join(timeout=30.0)
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()
        except Exception:
            pass
