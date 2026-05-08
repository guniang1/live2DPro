import { getApiBase } from "./apiBase.js";

/**
 * 立即触发当前用户 + 当前模型包的长期记忆总结（POST /api/long-memories/consolidate-now）。
 * 不受服务端后台 24 小时最短间隔限制。
 */
export async function triggerLongMemoryConsolidateNow({ userId, packageKey }) {
    const uid = Number(userId);
    if (!uid || !Number.isInteger(uid) || uid < 1) {
        throw new Error("请先登录（无效 user_id）");
    }
    const pkg = String(packageKey || "default").trim() || "default";
    const q = new URLSearchParams({ user_id: String(uid), package_key: pkg });
    const url = `${getApiBase()}/long-memories/consolidate-now?${q.toString()}`;
    const ctrl = new AbortController();
    const tid = setTimeout(() => ctrl.abort(), 180000);
    let res;
    try {
        res = await fetch(url, { method: "POST", signal: ctrl.signal });
    } catch (e) {
        if (e && e.name === "AbortError") {
            throw new Error(
                "请求超时：长期记忆总结耗时过长，请稍后再试或检查 Ollama 是否正常响应。"
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
