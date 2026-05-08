import { getApiBase } from "./apiBase.js";

/**
 * 立即触发服务端一轮 remind_trigger 扫描（POST /api/remind-triggers/scan-now）。
 */
export async function triggerRemindScanNow() {
    const url = `${getApiBase()}/remind-triggers/scan-now`;
    const res = await fetch(url, { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || body.code !== 0) {
        throw new Error(
            typeof body.message === "string"
                ? body.message
                : `扫描请求失败（HTTP ${res.status}）`
        );
    }
    return body.data && typeof body.data === "object" ? body.data : { ok: true };
}
