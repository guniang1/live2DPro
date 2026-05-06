/**
 * WebSocket 地址配置：
 * - 优先 VITE_WS_BASE（HTTPS 站点用 wss://），不要末尾斜杠。
 * - 未设置时从 VITE_API_BASE（见 apiBase.js）推导：http→ws、https→wss，主机端口一致。
 * - 二者皆无时默认 ws://localhost:8000（与 FastAPI 本地默认一致）。
 * VITE_ASR_WS_URL 可单独覆盖语音识别 /ws/asr。
 * /ws/chat：对话与朗读同连接；?session=、?package=、?user_id=。
 */

import { getHttpOrigin } from "./apiBase.js";
import * as LAppDefine from "../lappdefine.js";

const _SESSION_STORAGE_KEY = "live2d_ws_chat_session";

let _sessionId = null;
/** 与当前 Live2D 模型目录名一致（如 Xiaozi、Xiaogou），默认与 ModelDir[0] 对齐 */
let _live2dPackage = LAppDefine.ModelDir[0] || "Xiaozi";
/** 当前用户 ID（用于按用户维度加载模型参考音频配置） */
let _userId = 1;

/** 浏览器标签页会话 ID，用于 /ws/chat 与 chat_session.session_key 对齐；同标签内刷新不丢失 */
export function getSessionId() {
    if (_sessionId) {
        return _sessionId;
    }
    try {
        const saved = sessionStorage.getItem(_SESSION_STORAGE_KEY);
        if (saved && String(saved).trim()) {
            _sessionId = String(saved).trim();
            return _sessionId;
        }
    } catch (_) {
        /* ignore */
    }
    _sessionId =
        typeof crypto !== "undefined" && crypto.randomUUID
            ? crypto.randomUUID()
            : `s-${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
    try {
        sessionStorage.setItem(_SESSION_STORAGE_KEY, _sessionId);
    } catch (_) {
        /* ignore */
    }
    return _sessionId;
}

/** 当前后端应扫描的模型包名（与 public/Resources/<name>/ 一致） */
export function getLive2dPackage() {
    return _live2dPackage;
}

export function getUserId() {
    return _userId;
}

/** 切换模型后调用，下次重连 /ws/chat 时会携带新 package */
export function setLive2dPackage(name) {
    const s = String(name || "").trim();
    if (s) {
        _live2dPackage = s;
    }
}

export function setUserId(userId) {
    const n = Number(userId);
    if (Number.isInteger(n) && n > 0) {
        _userId = n;
    }
}

/** 返回 WebSocket 基地址（无末尾斜杠），供聊天与 ASR 共用 */
export function getWsBase() {
    const rawWs =
        typeof import.meta !== "undefined" &&
        import.meta.env &&
        import.meta.env.VITE_WS_BASE;
    const explicit = String(rawWs || "").trim().replace(/\/$/, "");
    if (explicit) {
        return explicit;
    }
    try {
        const u = new URL(getHttpOrigin());
        const wsProto = u.protocol === "https:" ? "wss:" : "ws:";
        return `${wsProto}//${u.host}`;
    } catch {
        return "ws://localhost:8000";
    }
}

/** 拼接聊天接口完整 WebSocket URL（JSON 流式对话） */
export function getChatWsUrl() {
    const q = new URLSearchParams();
    q.set("session", getSessionId());
    q.set("package", getLive2dPackage());
    q.set("user_id", String(getUserId()));
    return `${getWsBase()}/ws/chat?${q.toString()}`;
}

/** 遗留：单独 /ws/tts（当前前端不再连接）；需与 getChatWsUrl 同一 session */
export function getChatTtsWsUrl() {
    const sid = getSessionId();
    if (
        typeof import.meta !== "undefined" &&
        import.meta.env &&
        import.meta.env.VITE_CHAT_TTS_WS_URL
    ) {
        const base = import.meta.env.VITE_CHAT_TTS_WS_URL;
        const sep = base.includes("?") ? "&" : "?";
        return `${base}${sep}session=${encodeURIComponent(sid)}&package=${encodeURIComponent(getLive2dPackage())}`;
    }
    const q = new URLSearchParams();
    q.set("session", sid);
    q.set("package", getLive2dPackage());
    q.set("user_id", String(getUserId()));
    return `${getWsBase()}/ws/tts?${q.toString()}`;
}

/** 拼接实时语音识别 WebSocket URL（DashScope /ws/asr）；VITE_ASR_WS_URL 可覆盖完整地址 */
export function getAsrWsUrl() {
    if (
        typeof import.meta !== "undefined" &&
        import.meta.env &&
        import.meta.env.VITE_ASR_WS_URL
    ) {
        return import.meta.env.VITE_ASR_WS_URL;
    }
    return `${getWsBase()}/ws/asr`;
}
