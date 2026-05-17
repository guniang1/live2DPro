import { getApiBase } from "./apiBase.js";

/**
 * 立即触发当前用户的画像合并（POST /api/user-profiles/consolidate-now）。
 * 不受服务端后台 24 小时最短间隔限制；取材近 24h 内全部 chat_session（跨模型包）。
 */
export async function triggerUserProfileConsolidateNow({ userId }) {
    const uid = Number(userId);
    if (!uid || !Number.isInteger(uid) || uid < 1) {
        throw new Error("请先登录（无效 user_id）");
    }
    const q = new URLSearchParams({ user_id: String(uid) });
    const url = `${getApiBase()}/user-profiles/consolidate-now?${q.toString()}`;
    const ctrl = new AbortController();
    const tid = setTimeout(() => ctrl.abort(), 180000);
    let res;
    try {
        res = await fetch(url, { method: "POST", signal: ctrl.signal });
    } catch (e) {
        if (e && e.name === "AbortError") {
            throw new Error(
                "请求超时：用户画像总结耗时过长，请稍后再试或检查 Ollama 是否正常响应。"
            );
        }
        throw e;
    } finally {
        clearTimeout(tid);
    }
    const body = await res.json().catch(() => ({}));
    if (!res.ok || body.code !== 0) {
        throw new Error(
            typeof body.message === "string"
                ? body.message
                : `请求失败（HTTP ${res.status}）`
        );
    }
    return body.data && typeof body.data === "object"
        ? body.data
        : { ok: true, updated: false };
}
