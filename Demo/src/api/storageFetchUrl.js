/**
 * 将对象存储上的 URL 转为浏览器可 fetch 的地址。
 * - 私有 MinIO/S3 的「目录式」public_url 会 403，需先走后端 presign。
 * - 若网关提供「嵌套代理」（把整个目标 URL 做 base64url 挂在路径上），可通过环境变量启用。
 */

/** @param {string} str */
export function base64UrlEncodeUtf8(str) {
    const b64 = btoa(unescape(encodeURIComponent(str)));
    return b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** @param {string} url */
export function looksLikePresignedStorageUrl(url) {
    const u = String(url || '');
    return (
        /[?&]X-Amz-Algorithm=/i.test(u) ||
        /[?&]X-Amz-Signature=/i.test(u) ||
        /[?&]Signature=/i.test(u)
    );
}

/**
 * 可选：把已是「最终可访问」的 URL 再包一层后端代理（例如 /api/v1/download-shared-object/<base64url>）。
 * 构建前设置：VITE_DOWNLOAD_SHARED_OBJECT_BASE=http://127.0.0.1:9001/api/v1/download-shared-object
 * （不要末尾斜杠；留空则不做包装）
 *
 * @param {string} url
 * @returns {string}
 */
export function applyOptionalSharedDownloadProxy(url) {
    const raw = String(url || '').trim();
    if (!raw) {
        return raw;
    }
    const fromEnv =
        typeof import.meta !== 'undefined' &&
        import.meta.env &&
        import.meta.env.VITE_DOWNLOAD_SHARED_OBJECT_BASE;
    const base = String(fromEnv || '').trim().replace(/\/$/, '');
    if (!base) {
        return raw;
    }
    return `${base}/${base64UrlEncodeUtf8(raw)}`;
}
