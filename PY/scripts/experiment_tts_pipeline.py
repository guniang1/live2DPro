#!/usr/bin/env python3
"""
TTS 流水线对比实验（MiMo + Ollama/Qwen）

实验一：文本 → 单次 MiMo TTS（整段音频，无 LLM）
实验二：文本问题 → Qwen/Ollama 流式生成 → 遇 **句末标点**（。！？. ! ?）立即切段 → 逐段 MiMo TTS（逗号/分号不停顿）
实验三：同实验二，且 MiMo 附带语气/风格提示词（如「悲伤」，走导演指令 + ``MIMO_TTS_STYLE``）
实验四：同实验三链路，但用 **悲伤向聊天记录** 作上下文（LLM 多轮 + MiMo 导演），**不用**语气标签
实验五：同实验二链路，对比 **5a 先情感分析→风格标签** vs **5b 人设+短导演**（实测 5b 更快、语调更稳；**线上默认 5b**，见 ``wschat._mimo_director_user_prompt_sync``）

用法（在 PY 目录下）::

    python scripts/experiment_tts_pipeline.py --experiment 1 --question "今天天气怎么样？"
    python scripts/experiment_tts_pipeline.py --experiment 2 --question "用三句话介绍你自己。"
    python scripts/experiment_tts_pipeline.py --experiment 3 --question "我今天很难过。" --tts-emotion 悲伤
    python scripts/experiment_tts_pipeline.py --experiment 4 --question "我该怎么办？"
    python scripts/experiment_tts_pipeline.py --experiment 5 --question "我今天很难过。"
    python scripts/experiment_tts_pipeline.py --experiment all --min-chars 1
    python scripts/experiment_tts_pipeline.py --experiment 2 --dry-run

默认音色克隆参考：``assets/打电话了我没接到抱歉姐姐我现在下去接你.wav``（可用 ``--voice-sample`` 覆盖）。

结果写入 ``logs/experiment_tts_pipeline/``（JSON + 可选 WAV）。

实验三 vs 四见附录 I / ``\ref{sec:tts_exp34}``；实验五见附录 J / ``\ref{sec:tts_exp5}``（**推荐 5b**）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from utils.tts import mimo_tts, mimo_tts_configured  # noqa: E402

_DEFAULT_VOICE_SAMPLE = (
    _ROOT / "assets" / "打电话了我没接到抱歉姐姐我现在下去接你.wav"
)

# 句末标点：触发切段；中间停顿号（，、；等）不触发
_SENTENCE_END_PUNC = {"。", "！", "？", ".", "!", "?"}

# 实验四默认：内容偏悲伤的多轮记录（非「语气」标签）
# 实验五 5b：对齐 wschat 【人设】【语气】短导演（可 --persona-file 覆盖）
_DEFAULT_EXPERIMENT_PERSONA: dict[str, str] = {
    "character_desc": (
        "你是温柔、耐心的陪伴者，善于倾听对方烦恼，用词克制，会安抚情绪，"
        "不评判、不说教，像一位可靠的朋友。"
    ),
    "tone_style": (
        "语速适中，声线偏软，情绪平稳连贯，句末略带关切；不要夸张表演或突然拔高音调。"
    ),
}

_EMOTION_LABEL_WHITELIST = (
    "平静",
    "开心",
    "悲伤",
    "愤怒",
    "焦虑",
    "温柔",
    "鼓励",
)

_DEFAULT_SAD_CHAT_HISTORY: list[dict[str, str]] = [
    {
        "role": "user",
        "content": "最近总是失眠，一到晚上就忍不住想哭，白天也提不起劲。",
    },
    {
        "role": "assistant",
        "content": "听起来你这段时间很不容易。愿意慢慢说说，是什么让你这么难受吗？",
    },
    {
        "role": "user",
        "content": "有个很重要的朋友要搬去外地了，以后可能很难再见，我觉得自己又被丢下了。",
    },
    {
        "role": "assistant",
        "content": "告别确实会让人心里空落落的。你有这样的感受很正常，不用责怪自己。",
    },
]


def _resolve_refer_runtime(voice_sample: Path | None) -> dict | None:
    """MiMo 音色克隆：``refer_runtime`` 传入本地 wav/mp3 路径。"""
    if voice_sample is None:
        return None
    p = voice_sample.expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"参考音频不存在: {p}")
    return {"refer_wav_path": str(p)}


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


def flush_segments_by_sentence_end(
    stream_chunks: list[str],
    *,
    min_chars: int = 1,
    max_chars: int = 200,
) -> list[str]:
    """句末（。！？. ! ?）立即切段；超 ``max_chars`` 强制切；逗号/顿号不触发。"""
    min_chars = max(1, min(200, min_chars))
    max_chars = max(20, min(2000, max_chars))
    text_buffer: list[str] = []
    flushed: list[str] = []

    def _append_segment(sentence: str) -> None:
        s = sentence.strip()
        if s:
            flushed.append(s)

    for content in stream_chunks:
        for tk in iter_tokens(content):
            text_buffer.append(tk)
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
                    _append_segment(seg)
                    continue
                break
            if tk not in _SENTENCE_END_PUNC:
                continue
            sentence = "".join(text_buffer).strip()
            if not sentence or len(sentence) < min_chars:
                continue
            text_buffer.clear()
            _append_segment(sentence)

    while text_buffer:
        raw = "".join(text_buffer)
        if not raw.strip():
            text_buffer.clear()
            break
        if len(raw) > max_chars:
            seg, rest = _split_tokens_prefix_by_max_chars(text_buffer, max_chars)
            del text_buffer[:]
            text_buffer.extend(rest)
            _append_segment(seg)
        else:
            text_buffer.clear()
            _append_segment(raw)
    return flushed


def _is_wav(blob: bytes) -> bool:
    return len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WAVE"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Exp1Result:
    experiment: int = 1
    question: str = ""
    tts_text: str = ""
    ok: bool = False
    error: str = ""
    tts_ms: float = 0.0
    wav_bytes: int = 0
    wav_path: str | None = None
    started_at: str = ""


@dataclass
class SegmentTiming:
    index: int
    text: str
    chars: int
    segment_ready_ms: float
    mimo_ms: float
    cumulative_since_start_ms: float
    wav_bytes: int = 0
    wav_path: str | None = None
    ok: bool = False
    error: str = ""


@dataclass
class ExpStreamResult:
    """实验二 / 三共用：LLM 流式 + 句末切段 + MiMo。"""

    experiment: int = 2
    question: str = ""
    model: str = ""
    min_chars: int = 1
    max_chars: int = 200
    tts_emotion: str = ""
    mimo_director_prompt: str = ""
    llm_mood_hint: str = ""
    chat_history_turns: int = 0
    chat_history_text: str = ""
    variant: str = ""
    emotion_analyze_ms: float | None = None
    detected_emotion: str = ""
    ok: bool = False
    error: str = ""
    llm_first_token_ms: float | None = None
    llm_complete_ms: float | None = None
    answer_text: str = ""
    answer_chars: int = 0
    segment_count: int = 0
    total_mimo_ms: float = 0.0
    time_to_first_audio_ms: float | None = None
    wall_clock_ms: float = 0.0
    segments: list[SegmentTiming] = field(default_factory=list)
    started_at: str = ""


def _build_persona_director(character_desc: str, tone_style: str) -> str:
    """对齐 ``wschat._mimo_director_role_guide_text``。"""
    role = (character_desc or "").strip()
    tone = (tone_style or "").strip()
    blocks: list[str] = []
    if role:
        blocks.append("【人设】\n" + role)
    if tone:
        blocks.append("【语气】\n" + tone)
    if not blocks:
        blocks.append("【人设】\n（未配置；请自然朗读 assistant 中的台词。）")
    return "\n\n".join(blocks).strip()


def _build_mimo_emotion_director(emotion: str) -> str:
    e = (emotion or "").strip()
    if not e:
        return ""
    return f"请用「{e}」的语气朗读下面这句，情绪要自然、连贯。"


def _normalize_chat_history(raw: list[dict]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in ("user", "assistant", "system") or not content:
            continue
        out.append({"role": role, "content": content})
    return out


def _format_chat_history_for_prompt(history: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for h in history:
        role = h["role"]
        label = "用户" if role == "user" else ("助手" if role == "assistant" else "系统")
        lines.append(f"{label}：{h['content']}")
    return "\n".join(lines)


def _build_mimo_history_director(history: list[dict[str, str]]) -> str:
    if not history:
        return ""
    body = _format_chat_history_for_prompt(history)
    return (
        "【近期对话记录】\n"
        f"{body}\n\n"
        "请结合以上对话的情境与情绪延续性，自然、连贯地朗读下面这句。"
        "不要脱离语境，也不要刻意夸张。"
    )


def _build_llm_messages(
    question: str,
    *,
    system_prompt: str | None,
    llm_mood_hint: str | None,
    chat_history: list[dict[str, str]] | None = None,
) -> list[dict]:
    messages: list[dict] = []
    sys_parts: list[str] = []
    if system_prompt:
        sys_parts.append(system_prompt.strip())
    mood = (llm_mood_hint or "").strip()
    if mood:
        sys_parts.append(f"请用「{mood}」的语气组织回答。")
    history = chat_history or []
    if history and not system_prompt:
        sys_parts.append(
            "你是陪伴型对话助手。请结合下方对话历史理解用户处境，"
            "回答时保持与共情一致，语气真诚、克制。"
        )
    if sys_parts:
        messages.append({"role": "system", "content": "\n".join(sys_parts)})
    for h in history:
        if h["role"] == "system":
            continue
        messages.append({"role": h["role"], "content": h["content"]})
    q = question.strip()
    if history:
        q = f"【当前用户问题】\n{q}"
    messages.append({"role": "user", "content": q})
    return messages


def _load_chat_history_from_file(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return _normalize_chat_history(data)
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return _normalize_chat_history(data["messages"])
    raise ValueError("聊天记录 JSON 须为 [{role, content}, ...] 或 {messages: [...]}")


def _ollama_client(host: str):
    try:
        import ollama
    except ImportError as e:
        raise RuntimeError("未安装 ollama 包，请 pip install ollama") from e
    return ollama.Client(host=host)


def _ollama_message_content(resp: object) -> str:
    """非流式 / 流式均可用：取 ``message.content``。"""
    if resp is None:
        return ""
    try:
        if isinstance(resp, dict):
            m = resp.get("message")
        else:
            m = getattr(resp, "message", None)
        if m is None:
            return ""
        if isinstance(m, dict):
            return str(m.get("content") or "").strip()
        c = getattr(m, "content", None)
        return str(c).strip() if c is not None else ""
    except Exception:
        return ""


def _normalize_emotion_label(raw: str) -> str:
    s = (raw or "").strip().replace("。", "").replace("，", "")
    if not s:
        return "平静"
    for w in _EMOTION_LABEL_WHITELIST:
        if w in s:
            return w
    return s[:8] if len(s) <= 8 else s[:8]


def _analyze_emotion_label(
    client: object,
    model: str,
    question: str,
) -> tuple[str, float]:
    """额外一轮 Ollama：从用户问题抽取风格标签（实验 5a）。"""
    system = (
        "你是情感分析助手。根据用户最后一句话判断其主导情绪，"
        "只输出一个词作为语音风格标签，必须从以下选且仅输出该词："
        "平静、开心、悲伤、愤怒、焦虑、温柔、鼓励。"
        "不要解释，不要标点，不要换行。"
    )
    t0 = time.perf_counter()
    resp = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": question.strip()},
        ],
        stream=False,
    )
    ms = (time.perf_counter() - t0) * 1000
    label = _normalize_emotion_label(_ollama_message_content(resp))
    return label, ms


def _ollama_stream_chunk_content(chunk: object) -> str:
    """从 Ollama 流式 chunk 取增量文本（兼容 dict 与 ``ChatResponse`` 对象）。"""
    if chunk is None:
        return ""
    try:
        if isinstance(chunk, dict):
            m = chunk.get("message")
        else:
            m = getattr(chunk, "message", None)
        if m is None:
            return ""
        if isinstance(m, dict):
            return str(m.get("content") or "")
        c = getattr(m, "content", None)
        return str(c) if c is not None else ""
    except Exception:
        return ""


def run_experiment_1(
    *,
    question: str,
    tts_text: str | None,
    out_dir: Path,
    dry_run: bool,
    speech_assistant_only: bool,
    refer_runtime: dict | None,
) -> Exp1Result:
    """实验一：待朗读文本 → 单次 MiMo TTS。"""
    text = (tts_text if tts_text is not None else question).strip()
    res = Exp1Result(
        question=question.strip(),
        tts_text=text,
        started_at=_now_iso(),
    )
    if not text:
        res.error = "待合成文本为空"
        return res
    if dry_run:
        res.ok = True
        res.tts_ms = 0.0
        return res
    if not mimo_tts_configured():
        res.error = "未配置 MIMO_API_KEY"
        return res

    t0 = time.perf_counter()
    try:
        wav = mimo_tts(
            text,
            text_language="zh",
            refer_runtime=refer_runtime,
            speech_assistant_only=speech_assistant_only,
            merge_env_user_prompts=not speech_assistant_only,
        )
        res.tts_ms = (time.perf_counter() - t0) * 1000
        res.wav_bytes = len(wav)
        res.ok = bool(wav) and _is_wav(wav)
        if res.ok:
            out_dir.mkdir(parents=True, exist_ok=True)
            p = out_dir / "exp1_single.wav"
            p.write_bytes(wav)
            res.wav_path = str(p)
        else:
            res.error = "响应为空或非标准 WAV"
    except Exception as e:
        res.tts_ms = (time.perf_counter() - t0) * 1000
        res.error = f"{type(e).__name__}: {e}"
    return res


def run_experiment_streaming(
    *,
    experiment_id: int,
    question: str,
    model: str,
    ollama_host: str,
    min_chars: int,
    max_chars: int,
    out_dir: Path,
    dry_run: bool,
    refer_runtime: dict | None,
    system_prompt: str | None,
    tts_emotion: str | None = None,
    llm_mood_hint: str | None = None,
    chat_history: list[dict[str, str]] | None = None,
    wav_prefix: str = "exp_seg",
    exp5_variant: str = "",
    persona_character_desc: str = "",
    persona_tone_style: str = "",
) -> ExpStreamResult:
    """实验二～五：Qwen 流式 → 句末切段 → MiMo。"""
    history = list(chat_history or [])
    emotion = (tts_emotion or "").strip()
    emotion_analyze_ms: float | None = None
    detected = ""

    if experiment_id == 4:
        emotion = ""
        director = _build_mimo_history_director(history)
        mood = ""
    elif exp5_variant == "emotion_analyze":
        mood = ""
    elif exp5_variant == "persona":
        emotion = ""
        director = _build_persona_director(
            persona_character_desc, persona_tone_style
        )
        mood = ""
    else:
        director = _build_mimo_emotion_director(emotion) if emotion else ""
        mood = (llm_mood_hint or emotion or "").strip()

    res = ExpStreamResult(
        experiment=experiment_id,
        question=question.strip(),
        model=model,
        min_chars=min_chars,
        max_chars=max_chars,
        tts_emotion=emotion,
        mimo_director_prompt=director if exp5_variant != "emotion_analyze" else "",
        llm_mood_hint=mood,
        chat_history_turns=len(history),
        chat_history_text=_format_chat_history_for_prompt(history),
        variant=exp5_variant,
        started_at=_now_iso(),
    )
    if not res.question:
        res.error = "问题为空"
        return res

    t_wall0 = time.perf_counter()
    stream_chunks: list[str] = []
    answer_parts: list[str] = []
    llm_first_token_ms: float | None = None

    if exp5_variant == "emotion_analyze" and not dry_run:
        try:
            client = _ollama_client(ollama_host)
            detected, emotion_analyze_ms = _analyze_emotion_label(
                client, model, res.question
            )
            emotion = detected
            director = _build_mimo_emotion_director(emotion)
            res.tts_emotion = emotion
            res.detected_emotion = detected
            res.emotion_analyze_ms = round(emotion_analyze_ms, 2)
            res.mimo_director_prompt = director
        except Exception as e:
            res.error = f"情感分析失败: {type(e).__name__}: {e}"
            res.wall_clock_ms = (time.perf_counter() - t_wall0) * 1000
            return res
    elif exp5_variant == "emotion_analyze" and dry_run:
        detected = "悲伤"
        emotion = detected
        director = _build_mimo_emotion_director(emotion)
        emotion_analyze_ms = 120.0
        res.tts_emotion = emotion
        res.detected_emotion = detected
        res.emotion_analyze_ms = emotion_analyze_ms
        res.mimo_director_prompt = director

    if dry_run:
        sample = (
            "你好，我是你的虚拟伙伴。我可以陪你聊天、回答问题，"
            "也会根据你的情绪调整语气。有什么想聊的吗？"
        )
        stream_chunks = [sample[i : i + 3] for i in range(0, len(sample), 3)]
        answer_parts = [sample]
        res.llm_first_token_ms = 50.0
        res.llm_complete_ms = 200.0
    else:
        try:
            client = _ollama_client(ollama_host)
            messages = _build_llm_messages(
                res.question,
                system_prompt=system_prompt,
                llm_mood_hint=mood or None,
                chat_history=history or None,
            )
            stream = client.chat(model=model, messages=messages, stream=True)
            t_llm0 = time.perf_counter()
            for chunk in stream:
                content = _ollama_stream_chunk_content(chunk)
                if not content:
                    continue
                if llm_first_token_ms is None:
                    llm_first_token_ms = (time.perf_counter() - t_llm0) * 1000
                stream_chunks.append(content)
                answer_parts.append(content)
            res.llm_complete_ms = (time.perf_counter() - t_llm0) * 1000
            res.llm_first_token_ms = llm_first_token_ms
        except Exception as e:
            res.error = f"Ollama 流式失败: {type(e).__name__}: {e}"
            res.wall_clock_ms = (time.perf_counter() - t_wall0) * 1000
            return res

    res.answer_text = "".join(answer_parts).strip()
    res.answer_chars = len(res.answer_text)
    segments = flush_segments_by_sentence_end(
        stream_chunks, min_chars=min_chars, max_chars=max_chars
    )
    res.segment_count = len(segments)

    if not segments:
        res.error = "LLM 无输出或切段为空"
        res.wall_clock_ms = (time.perf_counter() - t_wall0) * 1000
        return res

    if not dry_run and not mimo_tts_configured():
        res.error = "未配置 MIMO_API_KEY"
        res.wall_clock_ms = (time.perf_counter() - t_wall0) * 1000
        return res

    out_dir.mkdir(parents=True, exist_ok=True)
    total_mimo = 0.0
    first_audio_ms: float | None = None
    use_emotion_tts = bool(emotion)
    use_mimo_director = bool((res.mimo_director_prompt or "").strip())
    style_env_key = "MIMO_TTS_STYLE"
    prev_style = os.environ.get(style_env_key)

    for i, seg_text in enumerate(segments, start=1):
        seg_ready_ms = (time.perf_counter() - t_wall0) * 1000
        st = SegmentTiming(
            index=i,
            text=seg_text,
            chars=len(seg_text),
            segment_ready_ms=round(seg_ready_ms, 2),
            mimo_ms=0.0,
            cumulative_since_start_ms=round(seg_ready_ms, 2),
        )
        if dry_run:
            st.ok = True
            st.mimo_ms = 300.0 + len(seg_text) * 8.0
            total_mimo += st.mimo_ms
            if first_audio_ms is None:
                first_audio_ms = seg_ready_ms + st.mimo_ms
            st.cumulative_since_start_ms = round(seg_ready_ms + st.mimo_ms, 2)
            res.segments.append(st)
            continue

        t_m0 = time.perf_counter()
        try:
            if use_emotion_tts:
                os.environ[style_env_key] = emotion
            wav = mimo_tts(
                seg_text,
                text_language="zh",
                refer_runtime=refer_runtime,
                user_director_prompt=director or None,
                speech_assistant_only=not use_mimo_director,
                merge_env_user_prompts=False,
            )
            st.mimo_ms = round((time.perf_counter() - t_m0) * 1000, 2)
            st.wav_bytes = len(wav)
            st.ok = bool(wav) and _is_wav(wav)
            if st.ok:
                p = out_dir / f"{wav_prefix}_{i:03d}.wav"
                p.write_bytes(wav)
                st.wav_path = str(p)
            else:
                st.error = "非标准 WAV 或空响应"
        except Exception as e:
            st.mimo_ms = round((time.perf_counter() - t_m0) * 1000, 2)
            st.error = f"{type(e).__name__}: {e}"
        finally:
            if use_emotion_tts:
                if prev_style is None:
                    os.environ.pop(style_env_key, None)
                else:
                    os.environ[style_env_key] = prev_style
        total_mimo += st.mimo_ms
        cum = (time.perf_counter() - t_wall0) * 1000
        st.cumulative_since_start_ms = round(cum, 2)
        if st.ok and first_audio_ms is None:
            first_audio_ms = cum
        res.segments.append(st)

    res.total_mimo_ms = round(total_mimo, 2)
    res.time_to_first_audio_ms = (
        round(first_audio_ms, 2) if first_audio_ms is not None else None
    )
    res.wall_clock_ms = round((time.perf_counter() - t_wall0) * 1000, 2)
    res.ok = all(s.ok for s in res.segments) if res.segments else False
    if not res.ok and not res.error:
        failed = [s for s in res.segments if not s.ok]
        res.error = f"{len(failed)}/{len(res.segments)} 段 MiMo 失败"
    return res


def _print_exp1(r: Exp1Result) -> None:
    print("\n=== 实验一：文本 → 单次 MiMo TTS ===")
    print(f"问题: {r.question!r}")
    print(f"待合成文本 ({len(r.tts_text)} 字): {r.tts_text[:80]!r}{'…' if len(r.tts_text) > 80 else ''}")
    if r.error:
        print(f"失败: {r.error}")
    else:
        print(f"MiMo 耗时: {r.tts_ms:.1f} ms")
        print(f"WAV: {r.wav_bytes} bytes → {r.wav_path or '(dry-run)'}")


def _print_exp_stream(r: ExpStreamResult) -> None:
    titles = {
        2: "实验二：Qwen 流式 → 句末标点切段 → 逐段 MiMo",
        3: "实验三：Qwen 流式 + 语气提示 → 句末切段 → MiMo",
        4: "实验四：Qwen 流式 + 悲伤聊天记录 → 句末切段 → MiMo",
        5: "实验五：Qwen 流式 → 句末切段 → MiMo（子方案见 variant）",
    }
    title = titles.get(r.experiment, f"实验{r.experiment}")
    if r.variant == "emotion_analyze":
        title = "实验五a：先情感分析 → 风格标签 → MiMo"
    elif r.variant == "persona":
        title = "实验五b：人设+短导演 → MiMo（无情感分析轮）"
    print(f"\n=== {title} ===")
    print(f"问题: {r.question!r}")
    print(
        f"模型: {r.model}  切段: 句末 。！？. ! ?（最短 {r.min_chars} 字，"
        f"最长 {r.max_chars} 字强制切）"
    )
    if r.experiment == 3:
        print(f"MiMo 语气: {r.tts_emotion!r}  导演: {r.mimo_director_prompt!r}")
        if r.llm_mood_hint:
            print(f"LLM 语气提示: {r.llm_mood_hint!r}")
    if r.experiment == 4:
        print(f"聊天记录: {r.chat_history_turns} 轮")
        if r.chat_history_text:
            preview = r.chat_history_text.replace("\n", " | ")[:120]
            print(f"历史摘要: {preview}{'…' if len(r.chat_history_text) > 120 else ''}")
        if r.mimo_director_prompt:
            dp = r.mimo_director_prompt.replace("\n", " ")[:100]
            print(f"MiMo 语境导演: {dp}…")
    if r.variant == "emotion_analyze":
        if r.emotion_analyze_ms is not None:
            print(f"情感分析耗时: {r.emotion_analyze_ms:.1f} ms  检出标签: {r.detected_emotion!r}")
        print(f"MiMo 导演: {r.mimo_director_prompt!r}")
    if r.variant == "persona":
        plen = len(r.mimo_director_prompt or "")
        print(f"MiMo 人设导演 ({plen} 字，无额外 LLM 分析轮)")
    if r.llm_first_token_ms is not None:
        print(f"LLM 首 token: {r.llm_first_token_ms:.1f} ms")
    if r.llm_complete_ms is not None:
        print(f"LLM 全文完成: {r.llm_complete_ms:.1f} ms  ({r.answer_chars} 字)")
    print(f"段数: {r.segment_count}  MiMo 合计: {r.total_mimo_ms:.1f} ms")
    if r.time_to_first_audio_ms is not None:
        print(f"首段音频就绪（墙钟）: {r.time_to_first_audio_ms:.1f} ms")
    print(f"总墙钟: {r.wall_clock_ms:.1f} ms")
    if r.error:
        print(f"状态: 失败 — {r.error}")
    print("\n分段明细:")
    print(f"{'#':>3} {'字':>4} {'段就绪ms':>10} {'MiMoms':>8} {'累计ms':>10}  文本")
    for s in r.segments:
        mark = "OK" if s.ok else "FAIL"
        preview = s.text.replace("\n", " ")[:36]
        print(
            f"{s.index:3d} {s.chars:4d} {s.segment_ready_ms:10.1f} "
            f"{s.mimo_ms:8.1f} {s.cumulative_since_start_ms:10.1f}  {mark} {preview}"
        )


def _save_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入: {path.resolve()}")


def main() -> int:
    p = argparse.ArgumentParser(description="MiMo TTS 流水线对比实验")
    p.add_argument(
        "--experiment",
        choices=["1", "2", "3", "4", "5", "both", "all"],
        default="both",
        help="1/2/3/4/5 单项；5=5a+5b 对比；both=1+2；all=1~5",
    )
    p.add_argument("--question", default="请用三句话介绍一下你自己。", help="用户文本问题")
    p.add_argument(
        "--tts-text",
        default=None,
        help="实验一专用：待朗读文本（默认与 --question 相同；可填完整「回答」做一次性合成对比）",
    )
    p.add_argument("--model", default=None, help="Ollama 模型，默认 OLLAMA_MODEL")
    p.add_argument("--ollama-host", default=None, help="默认 OLLAMA_HOST")
    p.add_argument(
        "--min-chars",
        type=int,
        default=1,
        help="遇句末标点后本段至少多少字才送 TTS（默认 1=立即；TTS_MIN_CHARS_PER_CHUNK）",
    )
    p.add_argument(
        "--max-chars",
        type=int,
        default=200,
        help="单段最大字数，超出强制切（默认 200；TTS_MAX_CHARS_PER_CHUNK）",
    )
    p.add_argument(
        "--system-prompt",
        default=None,
        help="实验二/三可选 LLM system 提示（默认仅 user 问题）",
    )
    p.add_argument(
        "--tts-emotion",
        default="悲伤",
        help="实验三：MiMo 语气/风格（导演指令 + MIMO_TTS_STYLE，默认「悲伤」）",
    )
    p.add_argument(
        "--llm-mood",
        default=None,
        help="实验三：LLM 回答语气（默认与 --tts-emotion 相同；设为空串则只改 MiMo）",
    )
    p.add_argument(
        "--chat-history-file",
        type=Path,
        default=None,
        help="实验四：聊天记录 JSON 文件（默认内置悲伤向多轮示例）",
    )
    p.add_argument(
        "--chat-history-json",
        default=None,
        help='实验四：内联聊天记录 JSON，如 [{"role":"user","content":"..."}]',
    )
    p.add_argument(
        "--persona-file",
        type=Path,
        default=None,
        help="实验五b：人设 JSON（character_desc / tone_style）",
    )
    p.add_argument(
        "--persona-role",
        default=None,
        help="实验五b：覆盖【人设】正文",
    )
    p.add_argument(
        "--persona-tone",
        default=None,
        help="实验五b：覆盖【语气】正文",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="输出目录，默认 logs/experiment_tts_pipeline/<时间戳>",
    )
    p.add_argument("--dry-run", action="store_true", help="不调 API，仅验证切段与报告结构")
    p.add_argument(
        "--voice-sample",
        type=Path,
        default=_DEFAULT_VOICE_SAMPLE,
        help=f"MiMo 音色克隆参考音频（默认 {_DEFAULT_VOICE_SAMPLE.name}）",
    )
    p.add_argument(
        "--mimo-preset-only",
        action="store_true",
        help="改用 MiMo 预置音色，不使用 --voice-sample",
    )
    args = p.parse_args()

    refer_runtime: dict | None = None
    voice_sample_path: str | None = None
    if args.mimo_preset_only:
        os.environ.pop("MIMO_VOICE_SAMPLE_PATH", None)
        if not (os.getenv("MIMO_TTS_MODEL") or "").strip():
            os.environ.setdefault("MIMO_TTS_MODEL", "mimo-v2.5-tts")
        if not (os.getenv("MIMO_TTS_VOICE") or "").strip():
            os.environ.setdefault("MIMO_TTS_VOICE", "mimo_default")
    else:
        refer_runtime = _resolve_refer_runtime(args.voice_sample)
        voice_sample_path = refer_runtime["refer_wav_path"]
        os.environ["MIMO_VOICE_SAMPLE_PATH"] = voice_sample_path
        os.environ.pop("MIMO_TTS_VOICE", None)

    if voice_sample_path:
        print(f"MiMo 音色克隆: {voice_sample_path}")
    else:
        print("MiMo 预置音色（--mimo-preset-only）")

    model = (args.model or os.getenv("OLLAMA_MODEL") or "qwen2.5:3b").strip()
    ollama_host = (args.ollama_host or os.getenv("OLLAMA_HOST") or "http://127.0.0.1:11434").strip()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out_dir or (_ROOT / "logs" / "experiment_tts_pipeline" / ts)

    report: dict = {
        "meta": {
            "created_at": _now_iso(),
            "dry_run": args.dry_run,
            "ollama_host": ollama_host,
            "ollama_model": model,
            "min_chars": args.min_chars,
            "max_chars": args.max_chars,
            "flush_mode": "sentence_end",
            "mimo_configured": mimo_tts_configured(),
            "voice_sample": voice_sample_path,
            "mimo_preset_only": args.mimo_preset_only,
        },
        "experiment_1": None,
        "experiment_2": None,
        "experiment_3": None,
        "experiment_4": None,
        "experiment_5a": None,
        "experiment_5b": None,
        "tts_emotion_default": args.tts_emotion,
    }

    persona_desc = _DEFAULT_EXPERIMENT_PERSONA["character_desc"]
    persona_tone = _DEFAULT_EXPERIMENT_PERSONA["tone_style"]
    if args.persona_file:
        pdata = json.loads(args.persona_file.expanduser().read_text(encoding="utf-8"))
        if isinstance(pdata, dict):
            persona_desc = str(pdata.get("character_desc") or persona_desc).strip()
            persona_tone = str(pdata.get("tone_style") or persona_tone).strip()
    if args.persona_role:
        persona_desc = args.persona_role.strip()
    if args.persona_tone:
        persona_tone = args.persona_tone.strip()

    exp4_history: list[dict[str, str]] = []
    if args.chat_history_json:
        exp4_history = _normalize_chat_history(json.loads(args.chat_history_json))
    elif args.chat_history_file:
        exp4_history = _load_chat_history_from_file(args.chat_history_file.expanduser())
    else:
        exp4_history = list(_DEFAULT_SAD_CHAT_HISTORY)

    exit_code = 0
    run_exp1 = args.experiment in ("1", "both", "all")
    run_exp2 = args.experiment in ("2", "both", "all")
    run_exp3 = args.experiment in ("3", "all")
    run_exp4 = args.experiment in ("4", "all")
    run_exp5 = args.experiment in ("5", "all")

    try:
        r2: ExpStreamResult | None = None
        r3: ExpStreamResult | None = None
        r4: ExpStreamResult | None = None
        r5a: ExpStreamResult | None = None
        r5b: ExpStreamResult | None = None

        if run_exp2:
            r2 = run_experiment_streaming(
                experiment_id=2,
                question=args.question,
                model=model,
                ollama_host=ollama_host,
                min_chars=args.min_chars,
                max_chars=args.max_chars,
                out_dir=out_dir / "exp2",
                dry_run=args.dry_run,
                system_prompt=args.system_prompt,
                refer_runtime=refer_runtime,
                wav_prefix="exp2_seg",
            )
            _print_exp_stream(r2)
            report["experiment_2"] = asdict(r2)
            if not r2.ok:
                exit_code = 1

        if run_exp3:
            llm_mood = args.llm_mood
            if llm_mood is None:
                llm_mood = args.tts_emotion
            r3 = run_experiment_streaming(
                experiment_id=3,
                question=args.question,
                model=model,
                ollama_host=ollama_host,
                min_chars=args.min_chars,
                max_chars=args.max_chars,
                out_dir=out_dir / "exp3",
                dry_run=args.dry_run,
                system_prompt=args.system_prompt,
                refer_runtime=refer_runtime,
                tts_emotion=args.tts_emotion,
                llm_mood_hint=llm_mood,
                wav_prefix="exp3_seg",
            )
            _print_exp_stream(r3)
            report["experiment_3"] = asdict(r3)
            if not r3.ok:
                exit_code = 1

        if run_exp4:
            r4 = run_experiment_streaming(
                experiment_id=4,
                question=args.question,
                model=model,
                ollama_host=ollama_host,
                min_chars=args.min_chars,
                max_chars=args.max_chars,
                out_dir=out_dir / "exp4",
                dry_run=args.dry_run,
                system_prompt=args.system_prompt,
                refer_runtime=refer_runtime,
                chat_history=exp4_history,
                wav_prefix="exp4_seg",
            )
            _print_exp_stream(r4)
            report["experiment_4"] = asdict(r4)
            report["meta"]["chat_history_turns"] = len(exp4_history)
            if not r4.ok:
                exit_code = 1

        if run_exp5:
            r5a = run_experiment_streaming(
                experiment_id=5,
                question=args.question,
                model=model,
                ollama_host=ollama_host,
                min_chars=args.min_chars,
                max_chars=args.max_chars,
                out_dir=out_dir / "exp5a",
                dry_run=args.dry_run,
                system_prompt=args.system_prompt,
                refer_runtime=refer_runtime,
                wav_prefix="exp5a_seg",
                exp5_variant="emotion_analyze",
            )
            _print_exp_stream(r5a)
            report["experiment_5a"] = asdict(r5a)

            r5b = run_experiment_streaming(
                experiment_id=5,
                question=args.question,
                model=model,
                ollama_host=ollama_host,
                min_chars=args.min_chars,
                max_chars=args.max_chars,
                out_dir=out_dir / "exp5b",
                dry_run=args.dry_run,
                system_prompt=args.system_prompt,
                refer_runtime=refer_runtime,
                wav_prefix="exp5b_seg",
                exp5_variant="persona",
                persona_character_desc=persona_desc,
                persona_tone_style=persona_tone,
            )
            _print_exp_stream(r5b)
            report["experiment_5b"] = asdict(r5b)
            if not r5a.ok or not r5b.ok:
                exit_code = 1

        if run_exp1:
            tts_for_exp1 = args.tts_text
            if (
                args.experiment in ("both", "all")
                and tts_for_exp1 is None
                and r2
                and r2.answer_text
            ):
                tts_for_exp1 = r2.answer_text
                print(
                    "\n[both] 实验一将朗读实验二生成的完整回答（同文本、单次 MiMo vs 分段 MiMo）"
                )
            r1 = run_experiment_1(
                question=args.question,
                tts_text=tts_for_exp1,
                out_dir=out_dir / "exp1",
                dry_run=args.dry_run,
                speech_assistant_only=True,
                refer_runtime=refer_runtime,
            )
            _print_exp1(r1)
            report["experiment_1"] = asdict(r1)
            if not r1.ok:
                exit_code = 1

        _save_report(out_dir / "report.json", report)

        stream_keys = (
            ("实验二", "experiment_2"),
            ("实验三", "experiment_3"),
            ("实验四", "experiment_4"),
            ("实验五a", "experiment_5a"),
            ("实验五b", "experiment_5b"),
        )
        if any(report.get(k) for _, k in stream_keys):
            print("\n--- 对比摘要 ---")
            if report.get("experiment_1"):
                e1_ms = report["experiment_1"].get("tts_ms") or 0
                print(f"实验一 单次 MiMo: {e1_ms:.1f} ms")
            for label, key in stream_keys:
                block = report.get(key)
                if not block:
                    continue
                first = block.get("time_to_first_audio_ms")
                wall = block.get("wall_clock_ms") or 0
                extra = ""
                emo = block.get("tts_emotion") or ""
                if block.get("emotion_analyze_ms") is not None:
                    extra = (
                        f" 情感分析={block.get('emotion_analyze_ms'):.0f}ms"
                        f" 标签={block.get('detected_emotion')!r}"
                    )
                elif emo:
                    extra = f" 语气={emo!r}"
                elif block.get("chat_history_turns"):
                    extra = f" 历史={block.get('chat_history_turns')}轮"
                elif block.get("variant") == "persona":
                    extra = " 人设导演"
                if first is not None:
                    print(f"{label} 首段音频墙钟: {first:.1f} ms{extra}")
                print(f"{label} 全流程墙钟: {wall:.1f} ms{extra}")

            b5a = report.get("experiment_5a")
            b5b = report.get("experiment_5b")
            if b5a and b5b:
                fa = b5a.get("time_to_first_audio_ms")
                fb = b5b.get("time_to_first_audio_ms")
                ea = b5a.get("emotion_analyze_ms") or 0
                print("\n--- 实验五 5a vs 5b（哪个更快）---")
                print(
                    "5a：多一轮 Ollama 情感分析 → 动态风格标签 + 短导演（类似实验三）"
                )
                print("5b：无分析轮，直接 wschat 式【人设】【语气】短导演")
                if fa is not None and fb is not None:
                    delta = fa - fb
                    print(
                        f"首段音频墙钟：5a={fa:.0f} ms  5b={fb:.0f} ms  "
                        f"差值={delta:+.0f} ms（5b 通常更快，约省去情感分析 {ea:.0f} ms）"
                    )

    except Exception:
        traceback.print_exc()
        return 2

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
