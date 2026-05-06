# 远程 Live2D 资源加载与 WebGL 贴图：问题、原因与解决方式

本文记录联调中出现的典型故障及对策，便于部署与论文「系统实现 / 联调」章节引用。实现以本仓库当前代码为准。

---

## 摘要

联调中曾出现三类现象：**对象存储直链返回 403**、**预签名下载接口返回 422**、以及 **WebGL `texImage2D` 跨域安全错误**。成因分别为：私有桶未授权的直接访问、HTTP 路由匹配顺序错误、以及跨域图像未按 CORS 规则供 WebGL 使用。通过对前端 manifest 使用预签名 URL（可选网关嵌套代理）、将字面量路径路由置于路径参数路由之前、为纹理 `Image` 设置 `crossOrigin` 并配置存储侧 CORS（或同源代理），可系统性消除上述故障。

---

## 1. 模型 JSON / 二进制等资源：`403 Forbidden`

### 现象

浏览器直接请求 `http://127.0.0.1:9000/...`（或等价 MinIO 对外基址）加载 `.model3.json`、`.moc3` 等失败，HTTP 403。

### 原因

桶策略为**私有**时，**无签名的对象 URL** 不具备读取权限。数据库索引中的 `public_url` 若为「基址 + 桶 + 对象键」式拼接而非临时授权链接，匿名读会被拒绝。

### 解决方式

- **后端**：对具备 `object_key` 的资源提供预签名下载能力（本仓库：`GET /api/live2d-model-assets/download-url`，见 `PY/live2d_db/http_api.py`）。
- **前端**：登录后按包拉取资源列表，构建 `relative_path → 可 fetch URL` 时，对存在 `object_key` 且 URL 尚不像预签名链接的条目，调用上述接口换取 presigned URL，再写入映射供 `LAppModel._resolveAssetUrl` 使用（见 `Demo/src/main.js`）。
- **可选网关嵌套**：若需将整条目标 URL 编码后走统一下载路径，可配置环境变量 `VITE_DOWNLOAD_SHARED_OBJECT_BASE`，逻辑见 `Demo/src/api/storageFetchUrl.js`（`applyOptionalSharedDownloadProxy`）。

---

## 2. `download-url` 接口：`422 Unprocessable Entity`

### 现象

请求  
`GET /api/live2d-model-assets/download-url?asset_id=...&expires_in=...`  
返回 422。

### 原因

在 FastAPI 中，若 **`GET /live2d-model-assets/{asset_id}` 先于**  
`GET /live2d-model-assets/download-url` 注册，则路径段 `download-url` 会被当成 **`asset_id`**，无法解析为整数，触发校验错误。

### 解决方式

将 **`/live2d-model-assets/download-url` 路由声明在 `/{asset_id}` 之前**（本仓库已在 `PY/live2d_db/http_api.py` 中调整并附注释）。

---

## 3. WebGL：`texImage2D` `SecurityError`（cross-origin data）

### 现象

控制台报错大致为：  
`Failed to execute 'texImage2D' on 'WebGL2RenderingContext': The image element contains cross-origin data, and may not be loaded.`  
堆栈常指向纹理加载（如 `lapptexturemanager.js`）。

### 原因

贴图 URL 与前端页面**不同源**（协议/主机/端口任一不同即不同源）。若 `<img>` 在赋值 `src` 前未设置 **`crossOrigin = 'anonymous'`**，像素对脚本/WebGL 不可导出，`texImage2D` 被拒绝。

### 解决方式

- **前端**：对跨源的 `http(s)` 纹理 URL，在设置 `img.src` **之前** 设置 `crossOrigin = 'anonymous'`（见 `Demo/src/lapptexturemanager.js` 中 `applyCrossOriginForWebGLTexture`）。
- **对象存储**：预签名 URL 所在站点必须在响应中包含 **`Access-Control-Allow-Origin`**，允许前端开发/生产 Origin；MinIO 需在对应 Bucket 配置 CORS（参见 `PY/live2d_db/minio_storage.py` 文件头注释）。
- **替代方案**：通过**与前端同源**的反向代理提供贴图，避免跨域。

---

## 4. 小结对照表

| 现象 | 主要原因 | 对策要点 |
|------|----------|----------|
| 403 | 私有桶 + 无签名直链 | manifest 使用 presigned；可选嵌套代理 |
| 422 | 动态路由吞掉字面路径 `download-url` | 字面路径路由排在 `{asset_id}` 前 |
| WebGL SecurityError | 跨域图未声明 CORS 模式 | `crossOrigin='anonymous'` + 存储 CORS 或同源代理 |

---

## 5. 相关工程位置（便于检索）

| 说明 | 路径 |
|------|------|
| 远程 manifest + presign 并发拉取 | `Demo/src/main.js` |
| 可选下载代理包装 | `Demo/src/api/storageFetchUrl.js` |
| API 基址与环境变量说明 | `Demo/src/api/apiBase.js` |
| 纹理跨域 | `Demo/src/lapptexturemanager.js` |
| 资源 URL 解析 | `Demo/src/lappmodel.js`（`setRemoteAssetUrlMap` / `_resolveAssetUrl`） |
| 下载链接与路由顺序 | `PY/live2d_db/http_api.py` |
| MinIO 预签名与公开基址 | `PY/live2d_db/minio_storage.py` |

---

*文档版本：与仓库实现同步整理；若接口或 env 变更，请以源码为准。*
