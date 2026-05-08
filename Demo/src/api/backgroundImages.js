import { getApiBase } from "./apiBase.js";
import { applyOptionalSharedDownloadProxy } from "./storageFetchUrl.js";

/**
 * @param {{ presign?: boolean, expiresIn?: number }} [options]
 * @returns {Promise<{ id: number, name: string, url: string, presigned_expires_in?: number }>}
 */
export async function fetchRandomBackgroundImage(options = {}) {
    const presign = options.presign !== false;
    const expiresIn =
        Number(options.expiresIn) > 0 ? Number(options.expiresIn) : 86400;
    const q = new URLSearchParams();
    q.set("presign", presign ? "true" : "false");
    q.set("expires_in", String(expiresIn));
    const res = await fetch(`${getApiBase()}/background-images/random?${q.toString()}`);
    const wrapped = await res.json().catch(() => ({}));
    if (!res.ok || wrapped.code !== 0 || !wrapped.data) {
        throw new Error(
            wrapped.message || `随机背景失败（HTTP ${res.status}）`
        );
    }
    const row = wrapped.data;
    const url = applyOptionalSharedDownloadProxy(String(row.url || "").trim());
    return { ...row, url };
}
