/**
 * HTTP API 根路径（含 /api 后缀），与 FastAPI 挂载的 /api 一致。
 * 云端部署：构建前设置 VITE_API_BASE=https://你的域名或IP:端口（不要末尾斜杠）。
 *
 * 私有 MinIO 若还需「网关嵌套下载」（整条 URL base64 挂在路径上），另设：
 * VITE_DOWNLOAD_SHARED_OBJECT_BASE=http://127.0.0.1:9001/api/v1/download-shared-object
 * （见 storageFetchUrl.js）
 */

export function getHttpOrigin() {
    const fromEnv =
        typeof import.meta !== "undefined" &&
        import.meta.env &&
        import.meta.env.VITE_API_BASE;
    const envTrim = String(fromEnv || "").trim().replace(/\/$/, "");
    if (envTrim) {
        return envTrim;
    }
    try {
        const raw = localStorage.getItem("live2d_info");
        const auth = raw ? JSON.parse(raw) : null;
        const hb = auth && String(auth.httpBase || "").trim().replace(/\/$/, "");
        if (hb) {
            return hb;
        }
    } catch (_) {
        /* ignore */
    }
    return "http://localhost:8000";
}

/** @returns {string} 例如 http://host:8000/api */
export function getApiBase() {
    return `${getHttpOrigin()}/api`;
}
