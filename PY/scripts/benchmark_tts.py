#!/usr/bin/env python3
"""
TTS 耗时基准：约 200 字中文，统计冷启动与预热后的多次采样。
GPT-SoVITS 默认带「丢弃轮」以反映本机 GPU/推理服务稳定后的性能（首次调用常含加载与缓存）。

用法（在 PY 目录下）:
  python scripts/benchmark_tts.py --provider gpt_sovits
  python scripts/benchmark_tts.py --provider mimo
  python scripts/benchmark_tts.py --provider both --runs 5 --discard-gpt 2

MiMo 默认使用预置音色 ``mimo_default``、模型 ``mimo-v2.5-tts``，并临时清除 ``MIMO_VOICE_SAMPLE_PATH``，
不读本地参考音频（与 .env 里 voiceclone 配置隔离）。若要与线上一致测克隆，加 ``--mimo-use-env``。

结果追加写入 logs/tts_benchmark_results.log（CSV 风格单行摘要 + 多行明细）。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# 保证可从仓库根目录导入 utils
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from utils.tts import (  # noqa: E402
    gpt_sovits_tts,
    mimo_tts,
    normalized_tts_provider,
)


# 约 200 字（含标点），用于统一对比两端耗时；可按需替换
DEFAULT_TEXT_ZH = (
    "语音合成技术正在快速发展，深度学习模型能够根据文本生成自然流畅的语音波形。"
    "在本系统中，我们对比本地 GPT-SoVITS 与云端小米 MiMo 两种方案的实际耗时。"
    "测试使用约两百字的中文段落，分别记录冷启动与预热后的多次采样，"
    "以便反映本机显卡与推理服务在稳定状态下的性能水平。"
    "请注意首次推理往往包含模型与缓存加载时间，后续请求通常更为稳定；"
    "若参考音色或网络环境不同，数值会有明显差异。"
    "固定基准文本段。"
)


def _is_wav(blob: bytes) -> bool:
    return len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WAVE"


_MIMO_ISOLATE_KEYS = ("MIMO_TTS_VOICE", "MIMO_VOICE_SAMPLE_PATH", "MIMO_TTS_MODEL")


@contextmanager
def _isolated_mimo_preset(voice: str, model: str):
    """基准专用：强制预置音色 + 预置模型，去掉本地样本路径，避免误走 voiceclone。"""
    snap = {k: os.environ.get(k) for k in _MIMO_ISOLATE_KEYS}
    try:
        os.environ["MIMO_TTS_VOICE"] = voice
        os.environ["MIMO_TTS_MODEL"] = model
        os.environ.pop("MIMO_VOICE_SAMPLE_PATH", None)
        yield
    finally:
        for k, v in snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _gpu_line() -> str:
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().replace("\n", " | ")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return ""


@dataclass
class RunRecord:
    label: str
    elapsed_ms: float
    wav_bytes: int
    ok: bool
    error: str = ""


@dataclass
class ProviderReport:
    provider: str
    text_chars: int
    cold: RunRecord | None = None
    discarded: list[RunRecord] = field(default_factory=list)
    benchmark: list[RunRecord] = field(default_factory=list)
    system: str = ""
    gpu: str = ""
    mimo_voice: str | None = None
    mimo_model: str | None = None
    mimo_env_isolated: bool = False

    def benchmark_stats(self) -> dict[str, float | int]:
        times = [r.elapsed_ms for r in self.benchmark if r.ok]
        if not times:
            return {"n_ok": 0}
        out: dict[str, float | int] = {
            "n_ok": len(times),
            "mean_ms": statistics.mean(times),
            "median_ms": statistics.median(times),
            "min_ms": min(times),
            "max_ms": max(times),
        }
        if len(times) >= 2:
            out["stdev_ms"] = statistics.stdev(times)
        return out


def _one_call(
    provider: str,
    text: str,
    *,
    gpt_kw: dict | None = None,
    mimo_preset: tuple[str, str] | None = None,
) -> tuple[bytes, float, str | None]:
    gpt_kw = gpt_kw or {}
    t0 = time.perf_counter()
    try:
        if provider == "gpt_sovits":
            blob = gpt_sovits_tts(text, text_language="zh", **gpt_kw)
        elif provider == "mimo":
            if mimo_preset:
                v, m = mimo_preset
                with _isolated_mimo_preset(v, m):
                    blob = mimo_tts(text, text_language="zh", refer_runtime=None)
            else:
                blob = mimo_tts(text, text_language="zh", refer_runtime=None)
        else:
            raise ValueError(provider)
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return b"", elapsed_ms, f"{type(e).__name__}: {e}"
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return blob, elapsed_ms, None


def benchmark_provider(
    provider: str,
    text: str,
    *,
    runs: int,
    discard_after_cold: int,
    gpt_refer_wav_path: str | None,
    gpt_prompt_text: str | None,
    gpt_prompt_language: str | None,
    gpt_base: str | None,
    mimo_voice: str,
    mimo_model: str,
    mimo_use_env: bool,
) -> ProviderReport:
    rep = ProviderReport(
        provider=provider,
        text_chars=len(text),
        system=f"{platform.system()} {platform.release()} {platform.machine()}",
        gpu=_gpu_line(),
    )

    gpt_kw: dict = {}
    if provider == "gpt_sovits":
        if gpt_refer_wav_path:
            gpt_kw["refer_wav_path"] = gpt_refer_wav_path
        if gpt_prompt_text:
            gpt_kw["prompt_text"] = gpt_prompt_text
        if gpt_prompt_language:
            gpt_kw["prompt_language"] = gpt_prompt_language
        if gpt_base:
            gpt_kw["base"] = gpt_base

    mimo_preset: tuple[str, str] | None = None
    if provider == "mimo":
        if not mimo_use_env:
            mimo_preset = (mimo_voice.strip(), mimo_model.strip())
            rep.mimo_voice = mimo_preset[0]
            rep.mimo_model = mimo_preset[1]
            rep.mimo_env_isolated = True
        else:
            rep.mimo_env_isolated = False

    # 冷启动（计入本系统首次推理，含可能的模型/缓存加载）
    blob, ms, err = _one_call(
        provider, text, gpt_kw=gpt_kw, mimo_preset=mimo_preset
    )
    ok = bool(blob) and _is_wav(blob)
    rep.cold = RunRecord(
        "cold",
        ms,
        len(blob),
        ok,
        error=(err or ("" if ok else "非空响应但非标准 WAV 或空体")),
    )

    # 丢弃轮：稳定 GPU / 本地服务；不计入 benchmark 聚合
    for i in range(max(0, discard_after_cold)):
        blob, ms, err = _one_call(
            provider, text, gpt_kw=gpt_kw, mimo_preset=mimo_preset
        )
        ok = bool(blob) and _is_wav(blob)
        rep.discarded.append(
            RunRecord(
                f"discard_{i + 1}",
                ms,
                len(blob),
                ok,
                error=err or ("" if ok else "invalid wav"),
            )
        )

    for i in range(max(1, runs)):
        blob, ms, err = _one_call(
            provider, text, gpt_kw=gpt_kw, mimo_preset=mimo_preset
        )
        ok = bool(blob) and _is_wav(blob)
        rep.benchmark.append(
            RunRecord(
                f"bench_{i + 1}",
                ms,
                len(blob),
                ok,
                error=err or ("" if ok else "invalid wav"),
            )
        )

    return rep


def _print_report(rep: ProviderReport) -> None:
    print(f"\n=== {rep.provider} ===")
    if rep.provider == "mimo":
        if rep.mimo_env_isolated:
            print(
                f"mimo preset (isolated from .env sample): "
                f"voice={rep.mimo_voice!r} model={rep.mimo_model!r}"
            )
        else:
            print("mimo: 使用当前 .env（含本地样本 / voiceclone 等），未做隔离")
    print(f"text_chars={rep.text_chars} system={rep.system}")
    if rep.gpu:
        print(f"gpu={rep.gpu}")
    if rep.cold:
        c = rep.cold
        print(
            f"cold_start: {c.elapsed_ms:.1f} ms wav_bytes={c.wav_bytes} ok={c.ok} {c.error}"
        )
    for d in rep.discarded:
        print(
            f"  discard: {d.elapsed_ms:.1f} ms wav_bytes={d.wav_bytes} ok={d.ok} {d.error}"
        )
    st = rep.benchmark_stats()
    print(f"benchmark_runs={len(rep.benchmark)} stats={st}")
    for b in rep.benchmark:
        print(
            f"  {b.label}: {b.elapsed_ms:.1f} ms wav_bytes={b.wav_bytes} ok={b.ok} {b.error}"
        )


def _append_log(reports: list[ProviderReport], text_preview: str) -> Path:
    log_dir = _ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "tts_benchmark_results.log"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"\n{'=' * 80}", f"timestamp_utc={ts}", f"text_preview={text_preview[:80]!r}..."]

    for rep in reports:
        st = rep.benchmark_stats()
        cold_ms = rep.cold.elapsed_ms if rep.cold else -1
        cold_ok = rep.cold.ok if rep.cold else False
        summary = {
            "provider": rep.provider,
            "text_chars": rep.text_chars,
            "cold_ms": cold_ms,
            "cold_ok": cold_ok,
            "discard_n": len(rep.discarded),
            "benchmark": st,
            "system": rep.system,
            "gpu": rep.gpu or None,
        }
        if rep.provider == "mimo":
            summary["mimo_env_isolated"] = rep.mimo_env_isolated
            summary["mimo_voice"] = rep.mimo_voice
            summary["mimo_model"] = rep.mimo_model
        lines.append(json.dumps(summary, ensure_ascii=False))
        if rep.cold and not rep.cold.ok:
            lines.append(f"  cold_error={rep.cold.error}")
        for b in rep.benchmark:
            if not b.ok:
                lines.append(f"  {b.label}_error={b.error}")

    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return log_path


def main() -> int:
    p = argparse.ArgumentParser(description="TTS 耗时基准（约 200 字）")
    p.add_argument(
        "--provider",
        choices=["gpt_sovits", "mimo", "both"],
        default="both",
        help="测试哪一路（both 则顺序跑 GPT 再 MiMo）",
    )
    p.add_argument("--runs", type=int, default=5, help="预热丢弃后的正式计时次数（≥1）")
    p.add_argument(
        "--discard-gpt",
        type=int,
        default=1,
        help="GPT-SoVITS 在 cold 之后丢弃的额外合成轮数（稳定本机 GPU，不计入 benchmark 聚合）",
    )
    p.add_argument(
        "--discard-mimo",
        type=int,
        default=0,
        help="MiMo 在 cold 之后丢弃的额外合成轮数",
    )
    p.add_argument(
        "--text-file",
        type=Path,
        default=None,
        help="从文件读入 UTF-8 文本；不设则用内置约 200 字段落",
    )
    p.add_argument(
        "--gpt-base",
        default=None,
        help="覆盖 GPTSOVITS_API_BASE",
    )
    p.add_argument("--refer-wav-path", default=None, help="GPT-SoVITS refer_wav_path")
    p.add_argument("--prompt-text", default=None, help="GPT-SoVITS prompt_text")
    p.add_argument(
        "--prompt-language",
        default=None,
        help="GPT-SoVITS prompt_language",
    )
    p.add_argument(
        "--mimo-voice",
        default="mimo_default",
        help="MiMo 基准预置音色（默认 mimo_default；仅在不加 --mimo-use-env 时生效）",
    )
    p.add_argument(
        "--mimo-model",
        default="mimo-v2.5-tts",
        help="MiMo 基准预置模型（默认 mimo-v2.5-tts；仅在不加 --mimo-use-env 时生效）",
    )
    p.add_argument(
        "--mimo-use-env",
        action="store_true",
        help="MiMo 不复写环境：沿用 .env（含 MIMO_VOICE_SAMPLE_PATH / voiceclone 模型等），用于测克隆链路",
    )
    args = p.parse_args()

    if args.text_file:
        text = args.text_file.read_text(encoding="utf-8").strip()
        if not text:
            print("文本文件为空", file=sys.stderr)
            return 2
    else:
        text = DEFAULT_TEXT_ZH

    runs = max(1, args.runs)
    providers: list[str]
    if args.provider == "both":
        providers = ["gpt_sovits", "mimo"]
    else:
        providers = [args.provider]

    print(f"文本长度: {len(text)} 字（字符数）")
    if len(text) < 50:
        print("警告: 文本较短，与「约两百字」场景不一致。", file=sys.stderr)

    reports: list[ProviderReport] = []

    for prov in providers:
        discard = (
            args.discard_gpt if prov == "gpt_sovits" else args.discard_mimo
        )
        if prov == "mimo" and not (os.getenv("MIMO_API_KEY") or "").strip():
            print(f"\n跳过 {prov}: 未配置 MIMO_API_KEY", file=sys.stderr)
            continue
        try:
            rep = benchmark_provider(
                prov,
                text,
                runs=runs,
                discard_after_cold=discard,
                gpt_refer_wav_path=args.refer_wav_path,
                gpt_prompt_text=args.prompt_text,
                gpt_prompt_language=args.prompt_language,
                gpt_base=args.gpt_base,
                mimo_voice=args.mimo_voice,
                mimo_model=args.mimo_model,
                mimo_use_env=args.mimo_use_env,
            )
            reports.append(rep)
            _print_report(rep)
        except Exception:
            print(f"\n{prov} 未捕获异常:\n{traceback.format_exc()}", file=sys.stderr)
            reports.append(
                ProviderReport(
                    provider=prov,
                    text_chars=len(text),
                    system=f"{platform.system()} {platform.release()}",
                )
            )

    if reports:
        path = _append_log(reports, text)
        print(f"\n已追加日志: {path}")

    # 若当前 .env 默认 provider 与测试不一致，仅提示
    print(f"当前 TTS_PROVIDER 归一化结果: {normalized_tts_provider()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
