#!/usr/bin/env python3
"""
流式聊天 TTS 指标：用可复现的数字对比切段策略与并行流水线（对齐 router/wschat.py 切段规则）。

模式
----
1. **dry-run（默认）**：不调 API。根据模拟 LLM 流式 chunk + every_n_end / min_chars，
   输出切段列表、段数、每段字数；并用简单耗时模型估算 **串行合成墙钟** vs **W 路并行**墙钟；
   可选按 ``voice_field_chars`` 估算 **MiMo 克隆每条请求的 JSON 体积量级**。
2. **--live-mimo**：对切段结果逐段调用 ``mimo_tts``（沿用当前 .env，含 voiceclone），
   打印每段真实耗时（注意计费与 429；建议加 ``--max-segments`` 限制）。

用法（在 PY 目录下）::

    python scripts/benchmark_streaming_tts_metrics.py
    python scripts/benchmark_streaming_tts_metrics.py --every-n-end 1 4 --workers 1 2 3
    python scripts/benchmark_streaming_tts_metrics.py --text-file story.txt --json
    python scripts/benchmark_streaming_tts_metrics.py --live-mimo --max-segments 2

与 ``scripts/compare_streaming_tts_flush.py`` 的关系：本脚本默认 **every_n_end 含 4**
（与 wschat 未设置 env 时一致），并补充 **并行调度** 与 **payload** 估算。
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import re
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SENTENCE_END_PUNC = {"。", "！", "？", ".", "!", "?"}


def iter_tokens(text: str) -> list[str]:
    pattern = r"[\u4e00-\u9fff]|[A-Za-z]+(?:'[A-Za-z]+)?|\d+|[^\w\s]"
    return re.findall(pattern, text)


def _split_tokens_prefix_by_max_chars(
    tokens: list[str], max_chars: int
) -> tuple[str, list[str]]:
    if not tokens:
        return "", []
    acc: list[str] = []
    n = 0
    cut = 0
    for i, tk in enumerate(tokens):
        if acc and n + len(tk) > max_chars:
            break
        acc.append(tk)
        n += len(tk)
        cut = i + 1
    if not acc:
        acc = [tokens[0]]
        cut = 1
    return "".join(acc).strip(), tokens[cut:]


def simulate_flush_wschat(
    stream_chunks: list[str],
    *,
    every_n_end: int,
    min_chars: int,
    max_chars: int = 200,
) -> list[str]:
    """对齐 wschat：句末即 flush；超 max_chars 强制切。"""
    every_n_end = max(1, min(200, every_n_end))
    min_chars = max(1, min(200, min_chars))
    max_chars = max(20, min(2000, max_chars))

    text_buffer: list[str] = []
    tts_sentence_end_punc_count = 0
    flushed: list[str] = []

    for content in stream_chunks:
        for tk in iter_tokens(content):
            text_buffer.append(tk)
            if tk in _SENTENCE_END_PUNC:
                tts_sentence_end_punc_count += 1

            while text_buffer:
                raw = "".join(text_buffer)
                if not raw.strip():
                    text_buffer.clear()
                    break
                if len(raw) > max_chars:
                    seg, rest = _split_tokens_prefix_by_max_chars(
                        text_buffer, max_chars
                    )
                    if not seg:
                        break
                    del text_buffer[:]
                    text_buffer.extend(rest)
                    tts_sentence_end_punc_count = 0
                    flushed.append(seg)
                    continue
                break

            if every_n_end <= 1:
                if tk not in _SENTENCE_END_PUNC:
                    continue
                sentence = "".join(text_buffer).strip()
                if not sentence or len(sentence) < min_chars:
                    continue
            else:
                if tts_sentence_end_punc_count < every_n_end:
                    continue
                sentence = "".join(text_buffer).strip()
                if not sentence:
                    continue

            text_buffer.clear()
            tts_sentence_end_punc_count = 0
            flushed.append(sentence)

    while text_buffer:
        raw = "".join(text_buffer)
        if not raw.strip():
            text_buffer.clear()
            break
        if len(raw) > max_chars:
            seg, rest = _split_tokens_prefix_by_max_chars(text_buffer, max_chars)
            del text_buffer[:]
            text_buffer.extend(rest)
            flushed.append(seg)
        else:
            text_buffer.clear()
            flushed.append(raw.strip())

    return flushed


def chunks_simulating_stream(full_text: str, *, chunk_size: int) -> list[str]:
    if chunk_size <= 0:
        return [full_text]
    out: list[str] = []
    i = 0
    while i < len(full_text):
        out.append(full_text[i : i + chunk_size])
        i += chunk_size
    return out


def segment_durations_ms(
    segments: list[str], *, base_ms: float, per_char_ms: float
) -> list[float]:
    return [base_ms + per_char_ms * len(s) for s in segments]


def wall_clock_parallel_fcfs(durations_ms: list[float], workers: int) -> float:
    """多 worker 从同一队列取任务：按到达顺序把下一段交给最先空闲的 worker。"""
    if not durations_ms:
        return 0.0
    w = max(1, workers)
    if w == 1:
        return sum(durations_ms)
    # min-heap：各 worker 当前累计结束时刻
    h = [0.0] * w
    heapq.heapify(h)
    for d in durations_ms:
        t = heapq.heappop(h)
        heapq.heappush(h, t + d)
    return max(h)


def estimate_json_payload_bytes(
    *,
    voice_field_chars: int,
    assistant_chars: int,
    user_chars: int,
    model_chars: int,
) -> int:
    """粗估：voice 多为 ASCII；messages 与外壳 JSON 用倍增近似。"""
    voice_bytes = voice_field_chars  # base64 data URL → 1 byte/char in utf-8 json string
    shell = 200 + model_chars + assistant_chars + user_chars
    shell *= 4  # json quoting / braces / audio.format 等
    return int(voice_bytes + shell)


DEFAULT_SAMPLE = """当然可以，这是一个关于勇气和友谊的故事。

从前，在一个遥远的地方，有一座被浓雾笼罩着的小村庄。孩子们是山林的守护者。有一天，恶势力闯入了这片土地。

小明挺身而出。他和朋友们一起踏上了旅程。最终，他们找到了真相，村子恢复了平静。"""


def run_scenario(
    *,
    stream_chunks: list[str],
    every_n_end: int,
    min_chars: int,
    max_chars: int,
    base_ms: float,
    per_char_ms: float,
    workers_list: list[int],
    voice_field_chars: int,
    user_chars_proxy: int,
    model_name: str,
) -> dict:
    segments = simulate_flush_wschat(
        stream_chunks,
        every_n_end=every_n_end,
        min_chars=min_chars,
        max_chars=max_chars,
    )
    lens = [len(s) for s in segments]
    durs = segment_durations_ms(segments, base_ms=base_ms, per_char_ms=per_char_ms)
    seq_wall = sum(durs)
    parallel_walls = {
        w: round(wall_clock_parallel_fcfs(durs, w), 2) for w in workers_list
    }
    assistant_lens = lens
    payloads = [
        estimate_json_payload_bytes(
            voice_field_chars=voice_field_chars,
            assistant_chars=a,
            user_chars=user_chars_proxy,
            model_chars=len(model_name),
        )
        for a in assistant_lens
    ]
    total_upload_bytes = sum(payloads)

    return {
        "every_n_end": every_n_end,
        "min_chars": min_chars,
        "segments_n": len(segments),
        "chars_per_segment": lens,
        "total_chars_segments": sum(lens),
        "model_latency_ms_sequential_sum": round(seq_wall, 2),
        "model_latency_ms_parallel_by_workers": parallel_walls,
        "estimated_payload_bytes_per_segment": payloads,
        "estimated_total_upload_bytes": total_upload_bytes,
        "estimated_total_upload_mb": round(total_upload_bytes / (1024 * 1024), 3),
        "latency_model": {"base_ms": base_ms, "per_char_ms": per_char_ms},
        "voice_field_chars_assumed": voice_field_chars,
    }


def print_table(rows: list[dict], workers: list[int]) -> None:
    header = ["every_n_end", "segments_n", "total_chars", "seq_sum_ms"]
    for w in workers:
        header.append(f"par_{w}w_ms")
    header.append("upload_MB")
    print("\n" + " | ".join(header))
    print("-" * min(120, 8 * len(header)))
    for r in rows:
        pw = r["model_latency_ms_parallel_by_workers"]
        cells = [
            str(r["every_n_end"]),
            str(r["segments_n"]),
            str(r["total_chars_segments"]),
            str(r["model_latency_ms_sequential_sum"]),
        ]
        for w in workers:
            cells.append(str(pw.get(w, 0)))
        cells.append(str(r["estimated_total_upload_mb"]))
        print(" | ".join(cells))


def live_mimo_segments(
    segments: list[str],
    *,
    max_segments: int,
    text_language: str,
) -> list[dict]:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
    from utils.tts import mimo_tts

    if not (os.getenv("MIMO_API_KEY") or "").strip():
        raise RuntimeError("未配置 MIMO_API_KEY，无法 --live-mimo")

    out: list[dict] = []
    for i, seg in enumerate(segments[:max_segments], 1):
        t0 = time.perf_counter()
        err = None
        wav = b""
        try:
            wav = mimo_tts(seg, text_language=text_language, refer_runtime=None)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        ms = (time.perf_counter() - t0) * 1000
        out.append(
            {
                "index": i,
                "chars": len(seg),
                "wall_ms": round(ms, 2),
                "wav_bytes": len(wav),
                "ok": bool(wav),
                "error": err,
            }
        )
    return out


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="流式 TTS 切段与并行指标（数据输出）")
    ap.add_argument("--text-file", type=Path, default=None, help="UTF-8 全文；默认内置短文")
    ap.add_argument("--chunk-size", type=int, default=3, help="模拟 LLM 流式每包字符数")
    ap.add_argument("--min-chars", type=int, default=1, help="TTS_MIN_CHARS_PER_CHUNK")
    ap.add_argument("--max-chars", type=int, default=200, help="TTS_MAX_CHARS_PER_CHUNK")
    ap.add_argument(
        "--every-n-end",
        type=int,
        nargs="+",
        default=[1, 4],
        help="对比多种攒批标点阈值（默认 1 与 4，对齐 wschat 默认 4）",
    )
    ap.add_argument("--latency-base-ms", type=float, default=2800.0, help="单段固定开销（贴近克隆首次 RTT）")
    ap.add_argument("--latency-per-char-ms", type=float, default=8.0, help="按字线性增量（经验系数，可调）")
    ap.add_argument(
        "--workers",
        type=int,
        nargs="+",
        default=[1, 2, 3],
        help="并行路数列表（用于墙钟估算）",
    )
    ap.add_argument(
        "--voice-field-chars",
        type=int,
        default=752938,
        help="克隆时 audio.voice 字符串长度量级（来自 TTS_DEBUG voice_field_chars）",
    )
    ap.add_argument("--user-chars-proxy", type=int, default=243, help="user content 长度近似（导演等）")
    ap.add_argument(
        "--model-name",
        default="mimo-v2.5-tts-voiceclone",
        help="用于 payload 粗估的模型名字符长度",
    )
    ap.add_argument("--json", action="store_true", help="stdout 只打印一行 JSON（多 scenario 为数组）")
    ap.add_argument("--live-mimo", action="store_true", help="对切段真实调用 mimo_tts（慎用）")
    ap.add_argument("--max-segments", type=int, default=2, help="--live-mimo 最多测前几段")
    ap.add_argument("--tts-lang", default="zh", help="--live-mimo 文本语言")
    args = ap.parse_args()

    text = (
        args.text_file.read_text(encoding="utf-8")
        if args.text_file
        else DEFAULT_SAMPLE
    )
    stream = chunks_simulating_stream(text.strip(), chunk_size=args.chunk_size)

    rows: list[dict] = []
    for en in args.every_n_end:
        row = run_scenario(
            stream_chunks=stream,
            every_n_end=en,
            min_chars=args.min_chars,
            max_chars=args.max_chars,
            base_ms=args.latency_base_ms,
            per_char_ms=args.latency_per_char_ms,
            workers_list=args.workers,
            voice_field_chars=args.voice_field_chars,
            user_chars_proxy=args.user_chars_proxy,
            model_name=args.model_name,
        )
        rows.append(row)

    meta = {
        "script": "benchmark_streaming_tts_metrics.py",
        "source_chars": len(text.strip()),
        "chunk_size": args.chunk_size,
        "workers_evaluated": args.workers,
    }

    if args.live_mimo:
        # 使用第一条 every_n_end 策略生成切段（可用 CLI 显式只传一个）
        primary_en = args.every_n_end[0]
        segments = simulate_flush_wschat(
            stream,
            every_n_end=primary_en,
            min_chars=args.min_chars,
            max_chars=args.max_chars,
        )
        live = live_mimo_segments(
            segments,
            max_segments=max(1, args.max_segments),
            text_language=args.tts_lang,
        )
        meta["live_mimo"] = {
            "every_n_end": primary_en,
            "segments_total": len(segments),
            "sampled": live,
            "sum_wall_ms_sampled": round(sum(x["wall_ms"] for x in live), 2),
        }

    if args.json:
        print(json.dumps({"meta": meta, "scenarios": rows}, ensure_ascii=False))
        return 0

    print("=== 流式 TTS 指标（dry-run 耗时为模型：base + per_char×字数；非 MiMo 实测）===")
    print(f"meta: {json.dumps(meta, ensure_ascii=False)}")
    print(
        f"\nlatency_model: base={args.latency_base_ms} ms + {args.latency_per_char_ms} ms/char\n"
        f"payload_model: voice_field_chars={args.voice_field_chars} (+ shell×4 粗估)\n"
    )
    print_table(rows, args.workers)

    print("\n说明:")
    print("  - seq_sum_ms：假设「串行合成」时各段耗时相加（等价 1 worker 且无流水线重叠误差）。")
    print("  - par_Nw_ms：同一切段序列、N 个 worker FCFS 调度下的墙钟上界（理想忽略队列抖动）。")
    print("  - upload_MB：按段数 ×（voice + 文本外壳粗估）之和；克隆场景 voice 占绝对大头。")
    print("  - 调 base/per_char 贴近你机器上 TTS_DEBUG 的 wall_ms，可提高预测可信度。")

    if args.live_mimo and "live_mimo" in meta:
        print("\n=== --live-mimo 实测（当前 .env）===")
        print(json.dumps(meta["live_mimo"], ensure_ascii=False, indent=2))

    log_dir = _ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "streaming_tts_metrics.jsonl"
    record = {"meta": meta, "scenarios": rows}
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"\n已追加 JSONL: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
