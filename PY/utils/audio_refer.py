"""参考音频：标准 RIFF/WAVE 判断与 ffmpeg 转 PCM WAV（上传页与 MiMo 运行时共用）。

需在系统 PATH 中安装 ``ffmpeg``（如 ``winget install ffmpeg``）；非 Python pip 包。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile

logger = logging.getLogger(__name__)

_DEFAULT_MAX_OUT = 50 * 1024 * 1024


def is_standard_riff_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def ffmpeg_convert_file_to_wav(
    path: str,
    *,
    timeout_s: float = 120.0,
    max_out_bytes: int | None = None,
) -> bytes | None:
    """本地文件 → PCM s16le mono 44.1kHz WAV；失败返回 None。"""
    if not ffmpeg_available():
        logger.warning("未找到 ffmpeg（PATH）")
        return None
    cap = max_out_bytes if max_out_bytes is not None else _DEFAULT_MAX_OUT
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                path,
                "-acodec",
                "pcm_s16le",
                "-ar",
                "44100",
                "-ac",
                "1",
                "-f",
                "wav",
                "pipe:1",
            ],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            if err:
                logger.warning("ffmpeg 转 wav 失败: %s", err[:500])
            return None
        out = proc.stdout or b""
        if not is_standard_riff_wav(out):
            return None
        if len(out) > cap:
            logger.warning("ffmpeg 输出超过上限 %s 字节", cap)
            return None
        return out
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg 转码超时 (%ss)", timeout_s)
        return None
    except OSError as e:
        logger.warning("ffmpeg 调用异常: %s", e)
        return None


def ffmpeg_convert_bytes_to_wav(
    blob: bytes,
    source_suffix: str,
    *,
    timeout_s: float = 120.0,
    max_out_bytes: int | None = None,
) -> bytes | None:
    """任意 ffmpeg 可解码的字节 → WAV（先写入临时文件再转码）。"""
    if not blob:
        return None
    suf = (source_suffix or ".bin").strip().lower()
    if not suf.startswith("."):
        suf = "." + suf
    fd, tmp = tempfile.mkstemp(prefix="tts_upload_", suffix=suf)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        return ffmpeg_convert_file_to_wav(
            tmp, timeout_s=timeout_s, max_out_bytes=max_out_bytes
        )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
