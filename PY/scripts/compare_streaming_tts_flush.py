#!/usr/bin/env python3
"""
对比「流式分句 → 送 TTS」两种攒批策略（与 router/wschat.py 中逻辑一致）：

- every_n_end=1：每遇到一个句末标点（且满足最短字数等条件）就 flush 一段；
- every_n_end=3：累计 3 个句末标点才把当前 buffer 整段 flush。

用法（在 PY 目录下）::
    python scripts/compare_streaming_tts_flush.py
    python scripts/compare_streaming_tts_flush.py --min-chars 1
    python scripts/compare_streaming_tts_flush.py --text-file story.txt

不设 API、不调 MiMo，只做**切块结果**与**粗粒度首包延迟**对比，便于调参。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 与 wschat._SENTENCE_END_PUNC 保持一致（仅句末；逗号/顿号/分号不触发）
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


def simulate_flush(
    stream_chunks: list[str],
    *,
    every_n_end: int,
    min_chars: int,
    max_chars: int = 200,
) -> list[str]:
    """按 wschat punctuation 模式：句末即 flush；超 max_chars 强制切。"""
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


def fake_tts_latency_ms(sentence: str, *, base_ms: float, per_char_ms: float) -> float:
    """粗模型：单段合成耗时 = 固定开销 + 按字线性（仅用于对比直觉，非真实 MiMo 曲线）。"""
    return base_ms + per_char_ms * len(sentence)


def chunks_simulating_stream(full_text: str, *, chunk_size: int) -> list[str]:
    """把全文切成多段，模拟 Ollama 流式 chunk（按字符切，避免拆坏英文词时可改用更大 chunk）。"""
    if chunk_size <= 0:
        return [full_text]
    out: list[str] = []
    i = 0
    while i < len(full_text):
        out.append(full_text[i : i + chunk_size])
        i += chunk_size
    return out


DEFAULT_SAMPLE = """当然可以，这是一个关于勇气和友谊的故事。

从前，在一个遥远的地方，有一座被浓雾笼罩着的小村庄。孩子们是山林的守护者。有一天，恶势力闯入了这片土地。

小明挺身而出。他和朋友们一起踏上了旅程。最终，他们找到了真相，村子恢复了平静。"""


def summarize(name: str, segments: list[str], latency_base: float, latency_per_char: float) -> None:
    total_chars = sum(len(s) for s in segments)
    first = segments[0] if segments else ""
    first_audio_ms = (
        fake_tts_latency_ms(first, base_ms=latency_base, per_char_ms=latency_per_char)
        if first
        else 0.0
    )
    print(f"\n{'=' * 60}")
    print(f"【{name}】")
    print(f"  合成段数: {len(segments)}  |  总字数(含标点): {total_chars}")
    print(
        f"  首段字数: {len(first)}  |  粗估首段合成耗时: {first_audio_ms:.0f} ms "
        f"(模型: {latency_base:.0f} + {latency_per_char:.1f}×字数)"
    )
    for i, seg in enumerate(segments, 1):
        preview = seg.replace("\n", " ")
        if len(preview) > 56:
            preview = preview[:56] + "…"
        print(f"  #{i:02d} ({len(seg)}字) {preview}")


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="对比 TTS 分句 flush：1 断句 vs 3 断句")
    parser.add_argument(
        "--text-file",
        type=Path,
        default=None,
        help="从文件读入全文；不传则用内置示例故事",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=3,
        help="模拟流式时每包字符数（默认 3，贴近「几个字一跳」）",
    )
    parser.add_argument(
        "--min-chars",
        type=int,
        default=1,
        help="对应 TTS_MIN_CHARS_PER_CHUNK：遇句末标点但不足此字数则继续攒（默认 1，与 wschat 一致）",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=200,
        help="对应 TTS_MAX_CHARS_PER_CHUNK：超此字数强制切段（默认 200）",
    )
    parser.add_argument("--latency-base-ms", type=float, default=300.0)
    parser.add_argument("--latency-per-char-ms", type=float, default=12.0)
    args = parser.parse_args()

    if args.text_file:
        text = args.text_file.read_text(encoding="utf-8")
    else:
        text = DEFAULT_SAMPLE

    stream = chunks_simulating_stream(text, chunk_size=args.chunk_size)

    a = simulate_flush(
        stream,
        every_n_end=1,
        min_chars=args.min_chars,
        max_chars=args.max_chars,
    )
    b = simulate_flush(
        stream,
        every_n_end=3,
        min_chars=args.min_chars,
        max_chars=args.max_chars,
    )

    print(
        "流式分句 → TTS 切块对比（规则对齐 wschat：句末标点 + max_chars + every_n_end）\n"
        f"模拟流式: chunk_size={args.chunk_size}, min_chars={args.min_chars}, "
        f"max_chars={args.max_chars}"
    )

    summarize(
        "策略 A：每 1 个句末标点即 flush（TTS_FLUSH_EVERY_N_SENTENCE_END=1）",
        a,
        args.latency_base_ms,
        args.latency_per_char_ms,
    )
    summarize(
        "策略 B：每 3 个句末标点 flush（TTS_FLUSH_EVERY_N_SENTENCE_END=3）",
        b,
        args.latency_base_ms,
        args.latency_per_char_ms,
    )

    print(f"\n{'=' * 60}")
    print("小结（同样文本、同样 min_chars 的前提下）")
    print(f"  - 策略 A 合成调用次数通常 ≥ 策略 B；首段往往更短，更易「更早开口」。")
    print(f"  - 策略 B 单次 payload 更长，HTTP/云端次数更少，首段可能更晚、更长。")
    print(f"  - min_chars 较大时，两种策略都会在早期标点处「暂不送 TTS」，避免极短碎片。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
