// /ws/chat：用户发 message；收 JSON 流式内容，并在「有音色参考」时同连接收 chunk_audio 元数据 + 对应二进制 WAV
import * as LAppDefine from "../lappdefine.js";
import { getChatWsUrl } from "./wsConfig.js";
import {
    applyChatLive2dActions,
    feedChatLive2dTtsAudioLipLevel,
    stopChatLive2dLipSync
} from "../lappdelegate.js";

/** 聊天 WebSocket：文本与朗读均在此连接，按序到达 */
let textSocket = null;
let reconnectTimerText = null;
/** 主动重连时跳过后续 onclose 内自动重连（避免与 connect* 重复） */
let skipAutoReconnectCount = 0;

const RECONNECT_MS = 3000;

let clearOutputTimerId = null;
/** 本轮回复流是否已收到 `done`（二进制音频可能仍在播放） */
let replyStreamDone = false;

/** 收到 done 且已无排队/播放中的 TTS 时收口型；表情与动作由 ``live2d_tags`` 帧驱动并保持至下一轮 */
function maybeFinalizeReplyPlayback() {
    if (!replyStreamDone) {
        return;
    }
    if (isPlayingAudio || audioQueue.length > 0) {
        return;
    }
    stopChatLive2dLipSync();
}
/** 上一帧 JSON 声明紧随其后的二进制为 WAV（chunk_audio） */
let pendingAudioMeta = null;
/** MediaElement 回退路径用的 Blob URL 队列 */
const audioQueue = [];
/** Web Audio 路径：待 decode 的 WAV ArrayBuffer（有序） */
const pendingWavQueue = [];
let isPlayingAudio = false;
let currentAudio = null;
/** @type {AudioContext | null} */
let ttsAudioContext = null;
/** @type {GainNode | null} */
let ttsGainNode = null;
/** @type {AnalyserNode | null} */
let ttsAnalyserNode = null;
/** 下一段应在 AudioContext 时间轴上的起点（ gapless 衔接） */
let ttsNextScheduleTime = 0;
/** @type {AudioBufferSourceNode[]} */
const ttsScheduledSources = [];
let ttsDrainRunning = false;
/** 打断朗读时递增，丢弃过时 decode/调度 */
let ttsPlaybackGeneration = 0;
/** 仍活跃的 BufferSource 数（正常播完递减） */
let ttsActiveBufferSources = 0;
let ttsLipRafId = null;

/**
 * 1 = 按 WAV 原速原调播放。MiMo 等云端合成一般为正常语速，默认用 1 避免 playbackRate 连带降调。
 * 若后端为 GPT-SoVITS 且 ``TTS_SPEED`` 偏大仍觉得快，可在页面提前设
 * ``globalThis.__TTS_PLAYBACK_RATE__ = 0.88``（会略降音高，属浏览器 playbackRate 特性）。
 */
const TTS_PLAYBACK_RATE =
    typeof globalThis.__TTS_PLAYBACK_RATE__ === "number" &&
    globalThis.__TTS_PLAYBACK_RATE__ > 0.1 &&
    globalThis.__TTS_PLAYBACK_RATE__ <= 4
        ? globalThis.__TTS_PLAYBACK_RATE__
        : 1;

function getTtsAudioContext() {
    if (!ttsAudioContext) {
        const AC =
            typeof AudioContext !== "undefined"
                ? AudioContext
                : typeof webkitAudioContext !== "undefined"
                  ? webkitAudioContext
                  : null;
        if (!AC) {
            console.warn("[TTS] 当前环境无 Web Audio API，口型将仅靠静音衰减");
            return null;
        }
        ttsAudioContext = new AC();
    }
    return ttsAudioContext;
}

function ensureTtsOutputChain() {
    const ctx = getTtsAudioContext();
    if (!ctx) {
        return null;
    }
    if (ttsGainNode && ttsAnalyserNode) {
        return ctx;
    }
    ttsGainNode = ctx.createGain();
    ttsGainNode.gain.value = 1;
    ttsAnalyserNode = ctx.createAnalyser();
    ttsAnalyserNode.fftSize = 1024;
    ttsAnalyserNode.smoothingTimeConstant = 0.58;
    ttsGainNode.connect(ttsAnalyserNode);
    ttsAnalyserNode.connect(ctx.destination);
    return ctx;
}

function stopTtsLipRaf() {
    if (ttsLipRafId != null) {
        cancelAnimationFrame(ttsLipRafId);
        ttsLipRafId = null;
    }
}

/**
 * @param {AnalyserNode} analyser
 */
function runTtsLipRaf(analyser) {
    const buf = new Uint8Array(analyser.fftSize);
    const step = () => {
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) {
            const x = (buf[i] - 128) / 128;
            sum += x * x;
        }
        const rms = Math.sqrt(sum / buf.length);
        const level = Math.min(1, Math.max(0, rms * 4.5));
        feedChatLive2dTtsAudioLipLevel(level);
        ttsLipRafId = requestAnimationFrame(step);
    };
    stopTtsLipRaf();
    ttsLipRafId = requestAnimationFrame(step);
}
let isAwaitingAiReply = false;
let pendingUserMessageAfterReconnect = null;
/** 一旦出现可见正文即取消「正在等待中」占位；此前 AI 气泡仅占位 */
let aiWaitingPhase = false;

function getOutput() {
    return document.getElementById("output");
}

function updateOutputVisibility() {
    const out = getOutput();
    if (!out) return;
    out.style.display = out.textContent.trim() ? "block" : "none";
}

function appendOutputWithHeightLimit(output, content) {
    if (!output || !content) return;
    output.textContent += content;
    if (output.scrollHeight > output.clientHeight) {
        // 超出容器高度时清空旧内容，仅保留当前新增片段。
        output.textContent = content;
    }
}

function getChatList() {
    return document.getElementById("chat-list");
}

/**
 * @param {string|undefined|null} isoOrRaw 服务端 ISO 串或其它可被 Date 解析的值
 * @returns {string} 空串表示无法展示
 */
function formatChatHistoryTime(isoOrRaw) {
    if (isoOrRaw == null || isoOrRaw === "") return "";
    const d = new Date(isoOrRaw);
    if (Number.isNaN(d.getTime())) return "";
    const now = new Date();
    const hm = { hour: "2-digit", minute: "2-digit", hour12: false };
    const timePart = d.toLocaleTimeString("zh-CN", hm);
    if (d.toDateString() === now.toDateString()) {
        return timePart;
    }
    const yest = new Date(now);
    yest.setDate(yest.getDate() - 1);
    if (d.toDateString() === yest.toDateString()) {
        return `昨天 ${timePart}`;
    }
    const withYear = d.getFullYear() !== now.getFullYear();
    const datePart = withYear
        ? `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`
        : `${d.getMonth() + 1}月${d.getDate()}日`;
    return `${datePart} ${timePart}`;
}

/**
 * @param {"user"|"ai"|"remind"} role
 * @param {string} text
 * @param {{ timeLabel?: string, timeIso?: string }} [options]
 */
function createChatBubbleElement(role, text, options = {}) {
    const bubble =
        role === "user" ? "user" : role === "remind" ? "remind" : "ai";
    const item = document.createElement("div");
    item.className = `chat-item ${bubble}`;
    item.textContent = text;

    const timeLabel =
        options.timeLabel != null && String(options.timeLabel).trim()
            ? String(options.timeLabel).trim()
            : "";
    if (!timeLabel) {
        return item;
    }

    const cluster = document.createElement("div");
    cluster.className = `chat-message-cluster chat-message-cluster--${bubble}`;
    cluster.appendChild(item);
    const meta = document.createElement("time");
    meta.className = "chat-message-meta-time";
    const timeIso = options.timeIso != null ? String(options.timeIso).trim() : "";
    if (timeIso) meta.dateTime = timeIso;
    meta.textContent = timeLabel;
    cluster.appendChild(meta);
    return cluster;
}

export function appendChatMessage(role, text) {
    const list = getChatList();
    if (!list || !text) return;

    list.appendChild(createChatBubbleElement(role, text));
    list.scrollTop = list.scrollHeight;
}

export function clearChatList() {
    const list = getChatList();
    if (!list) return;
    list.innerHTML = "";
}

/** 定时关怀落库曾用「【主动关怀】」+ trigger_type 作占位 user_input；界面不展示该条。 */
function isRemindCareUserStub(text) {
    return String(text || "").trim().startsWith("【主动关怀】");
}

/**
 * 按 chat_session 表一行渲染用户气泡 + AI 气泡（时间升序列表）。
 * @param {Array<{ user_input?: string, ai_reply?: string, create_time?: string }>} rows
 */
export function renderChatHistoryRows(rows) {
    clearChatList();
    const list = getChatList();
    if (!list || !Array.isArray(rows)) return;
    const frag = document.createDocumentFragment();
    for (const row of rows) {
        const u = String(row.user_input || "").trim();
        const a = String(row.ai_reply || "").trim();
        const timeLabel = formatChatHistoryTime(row.create_time);
        const timeOpts = timeLabel
            ? {
                  timeLabel,
                  timeIso:
                      row.create_time != null
                          ? String(row.create_time).trim()
                          : ""
              }
            : {};
        if (u && !isRemindCareUserStub(u)) {
            frag.appendChild(createChatBubbleElement("user", u, timeOpts));
        }
        if (a) frag.appendChild(createChatBubbleElement("ai", a, timeOpts));
    }
    list.appendChild(frag);
    const prevBehavior = list.style.scrollBehavior;
    list.style.scrollBehavior = "auto";
    list.scrollTop = list.scrollHeight;
    list.style.scrollBehavior = prevBehavior;
}

/**
 * 将更早的一页会话插到列表顶部（调用方负责恢复 scrollTop，避免跳动）。
 * rows 须已是时间正序（与 renderChatHistoryRows 一致）。
 * @param {Array<{ user_input?: string, ai_reply?: string, create_time?: string }>} rows
 */
export function prependChatHistoryRows(rows) {
    const list = getChatList();
    if (!list || !Array.isArray(rows) || rows.length === 0) return;
    const frag = document.createDocumentFragment();
    for (const row of rows) {
        const u = String(row.user_input || "").trim();
        const a = String(row.ai_reply || "").trim();
        const timeLabel = formatChatHistoryTime(row.create_time);
        const timeOpts = timeLabel
            ? {
                  timeLabel,
                  timeIso:
                      row.create_time != null
                          ? String(row.create_time).trim()
                          : ""
              }
            : {};
        if (u && !isRemindCareUserStub(u)) {
            frag.appendChild(createChatBubbleElement("user", u, timeOpts));
        }
        if (a) frag.appendChild(createChatBubbleElement("ai", a, timeOpts));
    }
    list.insertBefore(frag, list.firstChild);
}

function scheduleClearOutput() {
    if (clearOutputTimerId) {
        clearTimeout(clearOutputTimerId);
    }
    clearOutputTimerId = setTimeout(() => {
        const out = getOutput();
        if (out) out.textContent = "";
        updateOutputVisibility();
        clearOutputTimerId = null;
    }, 5000);
}

/** 避免对空/非 WAV 建 Blob：<audio> 会对 blob 发 Range 请求，无效体会触发 net::ERR_REQUEST_RANGE_NOT_SATISFIABLE */
function isLikelyWavBuffer(ab) {
    if (!ab || ab.byteLength < 12) return false;
    const u8 = new Uint8Array(ab, 0, 12);
    return (
        u8[0] === 0x52 &&
        u8[1] === 0x49 &&
        u8[2] === 0x46 &&
        u8[3] === 0x46 &&
        u8[8] === 0x57 &&
        u8[9] === 0x41 &&
        u8[10] === 0x56 &&
        u8[11] === 0x45
    );
}

function stopAllWebAudioTtsSources() {
    const toStop = ttsScheduledSources.splice(0, ttsScheduledSources.length);
    for (const s of toStop) {
        try {
            s.stop(0);
        } catch (_) {
            /* ignore */
        }
    }
    ttsNextScheduleTime = 0;
    pendingWavQueue.splice(0, pendingWavQueue.length);
    ttsDrainRunning = false;
}

function attachWebAudioSourceEnded(src, gen) {
    ttsActiveBufferSources++;
    src.onended = () => {
        if (gen !== ttsPlaybackGeneration) {
            return;
        }
        ttsActiveBufferSources = Math.max(0, ttsActiveBufferSources - 1);
        const ix = ttsScheduledSources.indexOf(src);
        if (ix >= 0) {
            ttsScheduledSources.splice(ix, 1);
        }
        if (ttsActiveBufferSources <= 0) {
            ttsNextScheduleTime = 0;
            stopTtsLipRaf();
            feedChatLive2dTtsAudioLipLevel(0);
            isPlayingAudio = false;
            maybeFinalizeReplyPlayback();
        }
    };
}

/**
 * @param {AudioContext} ctx
 * @param {ArrayBuffer} arrayBuffer
 * @param {number} gen
 */
async function scheduleWebAudioWav(ctx, arrayBuffer, gen) {
    if (gen !== ttsPlaybackGeneration) {
        return;
    }
    let audioBuffer;
    try {
        audioBuffer = await ctx.decodeAudioData(arrayBuffer.slice(0));
    } catch (e) {
        console.warn("[TTS] decodeAudioData 失败，跳过分段:", e);
        return;
    }
    if (gen !== ttsPlaybackGeneration || !ttsGainNode) {
        return;
    }
    const src = ctx.createBufferSource();
    src.buffer = audioBuffer;
    src.playbackRate.value = TTS_PLAYBACK_RATE;

    const now = ctx.currentTime;
    const startAt = Math.max(now, ttsNextScheduleTime || 0);
    const wallDur = audioBuffer.duration / TTS_PLAYBACK_RATE;
    ttsNextScheduleTime = startAt + wallDur;

    src.connect(ttsGainNode);
    ttsScheduledSources.push(src);
    attachWebAudioSourceEnded(src, gen);
    if (ttsAnalyserNode) {
        runTtsLipRaf(ttsAnalyserNode);
    }
    isPlayingAudio = true;
    src.start(startAt);
}

async function drainWebAudioWavQueue() {
    if (ttsDrainRunning) {
        return;
    }
    const ctx = ensureTtsOutputChain();
    if (!ctx || !ttsGainNode) {
        return;
    }
    ttsDrainRunning = true;
    const gen = ttsPlaybackGeneration;
    await ctx.resume().catch(() => {});
    try {
        while (pendingWavQueue.length > 0 && gen === ttsPlaybackGeneration) {
            const ab = pendingWavQueue.shift();
            if (!ab) {
                continue;
            }
            await scheduleWebAudioWav(ctx, ab, gen);
        }
    } finally {
        ttsDrainRunning = false;
        if (
            pendingWavQueue.length > 0 &&
            gen === ttsPlaybackGeneration
        ) {
            void drainWebAudioWavQueue();
        }
    }
}

function enqueueAudioAndPlay(arrayBuffer) {
    if (!isLikelyWavBuffer(arrayBuffer)) {
        console.warn(
            "[TTS] 跳过无效或空分段（需有效 WAV 头 RIFF/WAVE），字节数:",
            arrayBuffer?.byteLength ?? 0
        );
        return;
    }
    if (ensureTtsOutputChain()) {
        pendingWavQueue.push(arrayBuffer);
        void drainWebAudioWavQueue();
        return;
    }
    const blob = new Blob([arrayBuffer], { type: "audio/wav" });
    const url = URL.createObjectURL(blob);
    audioQueue.push(url);
    if (!isPlayingAudio) {
        void playAudioQueueMediaFallback();
    }
}

/** 无 Web Audio API 时用 HTMLAudioElement 顺序播放 */
async function playAudioQueueMediaFallback() {
    isPlayingAudio = true;
    try {
        while (audioQueue.length > 0) {
            const url = audioQueue.shift();
            if (!url) continue;
            const audio = new Audio();
            currentAudio = audio;
            audio.playbackRate = TTS_PLAYBACK_RATE;
            /** @type {MediaElementAudioSourceNode | null} */
            let mediaSource = null;
            /** @type {AnalyserNode | null} */
            let analyser = null;
            try {
                const ctx = getTtsAudioContext();
                if (ctx) {
                    await ctx.resume().catch(() => {});
                    mediaSource = ctx.createMediaElementSource(audio);
                    analyser = ctx.createAnalyser();
                    analyser.fftSize = 1024;
                    analyser.smoothingTimeConstant = 0.58;
                    mediaSource.connect(analyser);
                    analyser.connect(ctx.destination);
                    runTtsLipRaf(analyser);
                } else {
                    audio.volume = 1;
                }
                audio.src = url;
                await audio.play();
                await new Promise((resolve) => {
                    audio.onended = resolve;
                    audio.onerror = resolve;
                });
            } catch (e) {
                console.warn("音频播放失败，跳过该分段:", e);
            } finally {
                stopTtsLipRaf();
                feedChatLive2dTtsAudioLipLevel(0);
                try {
                    analyser?.disconnect();
                    mediaSource?.disconnect();
                } catch (_) {
                    /* ignore */
                }
                currentAudio = null;
                URL.revokeObjectURL(url);
            }
        }
    } finally {
        stopTtsLipRaf();
        feedChatLive2dTtsAudioLipLevel(0);
        isPlayingAudio = false;
        maybeFinalizeReplyPlayback();
    }
}

let currentAiReply = "";
let streamingAiItem = null;

function ensureStreamingAiMessageItem() {
    const list = getChatList();
    if (!list) return null;
    if (!streamingAiItem || !streamingAiItem.isConnected) {
        const item = document.createElement("div");
        item.className = "chat-item ai";
        list.appendChild(item);
        streamingAiItem = item;
    }
    return streamingAiItem;
}

/**
 * 用户消息已入列后调用：在列表底部展示 AI 侧「正在等待中」占位（有待显示正文即取消）。
 */
export function beginAiReplyWaitingUi() {
    aiWaitingPhase = true;
    const list = getChatList();
    if (!list) return;
    streamingAiItem = null;
    const item = ensureStreamingAiMessageItem();
    item.classList.add("chat-item--waiting");
    item.setAttribute("aria-busy", "true");
    item.replaceChildren();
    const label = document.createElement("span");
    label.className = "chat-waiting-label";
    label.textContent = "正在等待中";
    const dots = document.createElement("span");
    dots.className = "chat-waiting-dots";
    dots.setAttribute("aria-hidden", "true");
    dots.textContent = "···";
    item.appendChild(label);
    item.appendChild(dots);
    list.scrollTop = list.scrollHeight;
}

function updateStreamingAiMessage(text) {
    const item = ensureStreamingAiMessageItem();
    const list = getChatList();
    if (!item || !list) return;
    const t = String(text ?? "");
    if (aiWaitingPhase) {
        if (!t.trim().length) {
            list.scrollTop = list.scrollHeight;
            return;
        }
        aiWaitingPhase = false;
        item.classList.remove("chat-item--waiting");
        item.removeAttribute("aria-busy");
        item.replaceChildren();
    }
    item.textContent = t;
    list.scrollTop = list.scrollHeight;
}

function finalizeStreamingAiMessage() {
    if (!streamingAiItem) return;
    aiWaitingPhase = false;
    streamingAiItem.classList.remove("chat-item--waiting");
    streamingAiItem.removeAttribute("aria-busy");
    if (!streamingAiItem.textContent.trim()) {
        streamingAiItem.remove();
    }
    streamingAiItem = null;
}

/** 解析服务端下发的 ``#emotion#`` / ``#motion#`` 标签行（可多行），不写入聊天气泡 */
function applyLive2dTagText(text) {
    const raw = String(text ?? "").trim();
    if (!raw) {
        return;
    }
    const em = "#emotion#";
    const mo = "#motion#";
    for (const line of raw.split(/\r?\n/)) {
        const s = line.trim();
        if (!s) {
            continue;
        }
        const low = s.toLowerCase();
        if (low.startsWith(em)) {
            applyChatLive2dActions(s.slice(em.length).trim(), undefined);
        } else if (low.startsWith(mo)) {
            applyChatLive2dActions(undefined, s.slice(mo.length).trim());
        }
    }
}

/**
 * 将一条可见文本段落到 UI（与对应音频同序，由上游保证）
 * @param {string} content
 */
function applyVisibleTextSegment(content) {
    const output = document.getElementById("output");
    if (output && content) {
        appendOutputWithHeightLimit(output, content);
    }
    currentAiReply += content;
    updateStreamingAiMessage(currentAiReply);
    updateOutputVisibility();
}

/** 解析自 /ws/chat 的 JSON 文本帧 */
function handleTextChatIncoming(event) {
    if (typeof event.data !== "string") return;
    const data = JSON.parse(event.data);

    if (data.type === "catalog") {
        console.info(
            "[catalog]",
            data.package_key,
            "表情",
            data.expression?.length ?? 0,
            "动作",
            data.motion?.length ?? 0
        );
    } else if (data.type === "live2d_tags") {
        applyLive2dTagText(data.text);
    } else if (data.type === "chunk") {
        applyVisibleTextSegment(data.content);
    } else if (data.type === "chunk_audio") {
        pendingAudioMeta = {
            index: data.index,
            size: data.size
        };
        // 定时关怀朗读：正文已在 remind_trigger 里用角色侧紫色气泡展示，勿再走流式 AI 气泡
        if (!data.remind_audio) {
            applyVisibleTextSegment(data.content);
        }
    } else if (data.type === "remind_trigger") {
        const scene = String(data.trigger_type ?? "").trim();
        const body = String(data.delivery_message ?? "").trim();
        // trigger_type 为空表示无需展示关怀；正文为空也不使用兜底话术
        if (!scene || !body) {
            console.info(
                "[remind_trigger] skip empty type or body",
                data.trigger_id,
                { scene: !!scene, bodyLen: body.length }
            );
            return;
        }
        // 仅展示角色侧话术；user_input 落库为空（见 wschat._persist_remind_delivery_to_chat_session）
        appendChatMessage("ai", body);
        console.info("[remind_trigger]", data.trigger_id, scene);
    } else if (data.type === "done") {
        pendingAudioMeta = null;
        console.log("回复完成（文本与音频帧已结束）");
        aiWaitingPhase = false;
        if (streamingAiItem) {
            streamingAiItem.classList.remove("chat-item--waiting");
            streamingAiItem.removeAttribute("aria-busy");
        }
        if (!streamingAiItem) {
            appendChatMessage("ai", currentAiReply.trim());
        } else {
            updateStreamingAiMessage(currentAiReply.trim());
            finalizeStreamingAiMessage();
        }
        replyStreamDone = true;
        isAwaitingAiReply = false;
        currentAiReply = "";
        scheduleClearOutput();
        maybeFinalizeReplyPlayback();
    } else if (data.type === "error") {
        pendingAudioMeta = null;
        const outEl = document.getElementById("output");
        const msg = data.message || "未知错误";
        if (outEl) {
            appendOutputWithHeightLimit(outEl, `\n[错误] ${msg}`);
        }
        currentAiReply += `\n[错误] ${msg}`;
        aiWaitingPhase = false;
        if (streamingAiItem) {
            streamingAiItem.classList.remove("chat-item--waiting");
            streamingAiItem.removeAttribute("aria-busy");
        }
        updateStreamingAiMessage(currentAiReply);
        updateOutputVisibility();
        isAwaitingAiReply = false;
    }
}

/** 紧跟 chunk_audio 的二进制 WAV（无前置元数据则丢弃，避免误播） */
function handleChatBinaryIncoming(event) {
    const info = pendingAudioMeta;
    pendingAudioMeta = null;
    if (!info) {
        console.warn("[chat] 收到二进制帧但无前置 chunk_audio，已忽略");
        return;
    }
    if (event.data instanceof ArrayBuffer) {
        enqueueAudioAndPlay(event.data);
        console.info(
            `收到与文本同步的音频分段 #${info.index} (${info.size} bytes)`
        );
    } else if (event.data instanceof Blob) {
        const sock = event.target;
        event.data.arrayBuffer().then((buf) => {
            if (sock !== textSocket) {
                return;
            }
            enqueueAudioAndPlay(buf);
            console.info(
                `收到与文本同步的音频分段 #${info.index} (${info.size} bytes)`
            );
        });
    } else {
        console.warn("[chat] 未识别的二进制消息类型", typeof event.data);
    }
}

function handleChatIncoming(event) {
    // 重连后 textSocket 已指向新连接，忽略旧 socket 上迟到的 JSON/二进制（避免旧段音频灌进队列）
    if (event.target !== textSocket) {
        return;
    }
    if (typeof event.data === "string") {
        handleTextChatIncoming(event);
    } else {
        handleChatBinaryIncoming(event);
    }
}

function wireTextSocket(ws, url, label) {
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
        console.info(`${label} 已连接:`, url);
        if (
            pendingUserMessageAfterReconnect &&
            textSocket === ws &&
            ws.readyState === WebSocket.OPEN
        ) {
            const message = pendingUserMessageAfterReconnect;
            pendingUserMessageAfterReconnect = null;
            resetStreamingState();
            try {
                ws.send(JSON.stringify(buildChatWsPayload(message)));
                isAwaitingAiReply = true;
                console.info("已打断上一轮并发送新消息");
            } catch (e) {
                console.error("打断后自动发送失败:", e);
                isAwaitingAiReply = false;
            }
        }
    };
    ws.onmessage = handleChatIncoming;
    ws.onerror = (err) => {
        console.error(`${label} 错误:`, err);
    };
}

function connectText() {
    if (reconnectTimerText) {
        clearTimeout(reconnectTimerText);
        reconnectTimerText = null;
    }
    const url = getChatWsUrl();
    const ws = new WebSocket(url);
    textSocket = ws;
    wireTextSocket(ws, url, "/ws/chat");
    ws.onclose = () => {
        if (textSocket !== ws) {
            return;
        }
        textSocket = null;
        if (skipAutoReconnectCount > 0) {
            skipAutoReconnectCount--;
            return;
        }
        console.warn(`聊天 WebSocket 已断开，${RECONNECT_MS / 1000}s 后重连…`);
        reconnectTimerText = setTimeout(connectText, RECONNECT_MS);
    };
}

connectText();
updateOutputVisibility();

/**
 * 切换 Live2D 模型包名后调用：立即用新 ?package= 重连 /ws/chat。
 */
export function reconnectChatWebSocketsForNewPackage() {
    if (reconnectTimerText) {
        clearTimeout(reconnectTimerText);
        reconnectTimerText = null;
    }
    skipAutoReconnectCount = 1;
    resetStreamingState();
    try {
        if (textSocket) {
            textSocket.close();
        }
    } catch (e) {
        console.warn("关闭聊天 WebSocket:", e);
    }
    connectText();
}

function formatUserWorldTimeForChat() {
    try {
        return new Date().toLocaleString("zh-CN", {
            weekday: "short",
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            hour12: false,
        });
    } catch {
        return new Date().toISOString();
    }
}

function buildChatWsPayload(text) {
    const scene_location = String(
        LAppDefine.backgroundCycle.displayName || ""
    ).trim();
    const scene_time = formatUserWorldTimeForChat();
    const payload = { message: text };
    if (scene_location) {
        payload.scene_location = scene_location;
    }
    if (scene_time) {
        payload.scene_time = scene_time;
    }
    return payload;
}

function resetStreamingState() {
    aiWaitingPhase = false;
    if (clearOutputTimerId) {
        clearTimeout(clearOutputTimerId);
        clearOutputTimerId = null;
    }
    replyStreamDone = false;
    ttsPlaybackGeneration++;
    stopAllWebAudioTtsSources();
    ttsActiveBufferSources = 0;
    stopTtsLipRaf();
    stopChatLive2dLipSync();
    const out = getOutput();
    if (out) out.textContent = "";
    finalizeStreamingAiMessage();
    currentAiReply = "";
    pendingAudioMeta = null;
    audioQueue.splice(0, audioQueue.length);
    if (currentAudio) {
        try {
            currentAudio.pause();
            currentAudio.currentTime = 0;
        } catch (e) {
            console.warn("停止当前音频播放失败:", e);
        }
        currentAudio = null;
    }
    updateOutputVisibility();
}

/**
 * 向 /ws/chat 发送用户输入；朗读音频由同连接在对应文本后推送。
 * @param {string} message
 * @returns {boolean} 是否已发送
 */
export function sendChatMessage(message) {
    const text = String(message || "").trim();
    if (!text) {
        return false;
    }

    // 若上一轮仍在进行，直接打断：断开并重连后自动发送新消息。
    if (isAwaitingAiReply) {
        pendingUserMessageAfterReconnect = text;
        reconnectChatWebSocketsForNewPackage();
        return true;
    }

    if (!textSocket || textSocket.readyState !== WebSocket.OPEN) {
        if (pendingUserMessageAfterReconnect === null) {
            pendingUserMessageAfterReconnect = text;
        } else {
            pendingUserMessageAfterReconnect = text;
        }
        reconnectChatWebSocketsForNewPackage();
        return true;
    }
    try {
        resetStreamingState();
        textSocket.send(JSON.stringify(buildChatWsPayload(text)));
        isAwaitingAiReply = true;
        return true;
    } catch (e) {
        console.error("WebSocket 发送失败:", e);
        isAwaitingAiReply = false;
        return false;
    }
}
