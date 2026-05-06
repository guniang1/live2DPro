import { getApiBase } from "./apiBase.js";

/**
 * 拉取 chat_session 列表（GET /api/chat-sessions）。
 * 不传 session_key 时：按 user_id（+ 可选 package_key）取近期行，适合左侧「该角色」总历史。
 * @param {{ userId: number, packageKey?: string, sessionKey?: string, limit?: number }} p
 * @returns {Promise<Array<{ user_input?: string, ai_reply?: string, create_time?: string }>>}
 */
export async function fetchChatSessionsForPanel(p) {
    const uid = Number(p.userId);
    if (!Number.isInteger(uid) || uid <= 0) {
        throw new Error("无效 user_id");
    }
    const limit = Number(p.limit) > 0 ? Math.min(500, Number(p.limit)) : 200;
    const params = new URLSearchParams();
    params.set("user_id", String(uid));
    params.set("limit", String(limit));
    params.set("offset", "0");
    const pk = p.packageKey != null ? String(p.packageKey).trim() : "";
    if (pk) {
        params.set("package_key", pk);
    }
    const sk =
        p.sessionKey != null && p.sessionKey !== ""
            ? String(p.sessionKey).trim()
            : "";
    if (sk) {
        params.set("session_key", sk);
    }
    const url = `${getApiBase()}/chat-sessions?${params.toString()}`;
    const res = await fetch(url);
    const body = await res.json().catch(() => ({}));
    if (!res.ok || body.code !== 0) {
        throw new Error(
            typeof body.message === "string"
                ? body.message
                : `加载对话失败（HTTP ${res.status}）`
        );
    }
    return Array.isArray(body.data) ? body.data : [];
}
