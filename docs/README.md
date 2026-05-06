# 毕业论文正文内容稿（说明）

本目录中的 `thesis-draft.md` 为学士学位论文**正文内容稿**（Markdown），章节顺序符合《北京语言大学本科生毕业论文（设计）写作规范》中“引言—正文—结论—参考文献—致谢”等组成部分的一般要求。正式排版须使用学校统一印发的 **《本科生毕业论文》Word 模板**，并遵照 **《论文排版注意事项》** 设置页眉、分节页码、目录自动生成、图表公式格式等；**不得以本文 `.md` 替代 Word 模板**。

**使用建议**

1. 在 Word 中打开学校模板，将各章内容自 `thesis-draft.md` 粘贴至对应位置后，按模板应用标题样式并**自动生成目录**。
2. 正文避免大段源代码；必要内容可经整理后放入**附录**（规范建议程序类材料宜放附录）。
3. 参考文献定稿时按院系规定的著录格式统一核对。
4. 服务端 HTTP 接口与配置细则可参见工程内《后端项目正文》等技术说明，以保持正文篇幅适中；**正文稿中刻意不写工程目录名与源文件名**，与定稿论文语体一致。
5. **模型包人设、REST 约定与 MiMo 导演模式（user/assistant 分工）**：见同目录 [`文档.md`](文档.md)。
6. **分层架构、顺序图与 `/ws/chat` 流程图（Mermaid）**：见 [`架构与时序图.md`](架构与时序图.md)。

---

### 还可写入论文或附录的要点（按需裁剪）

下列内容在分散文档里多有涉及，若答辩材料要「一页说清」，可考虑单独做 **附录：部署清单 / 验收用例 / 局限与展望**：

| 主题 | 建议写法 | 已有参考 |
|------|----------|----------|
| **前端构建与环境变量** | 汇总 `VITE_API_BASE`、`VITE_WS_BASE`、`VITE_CHAT_TTS_WS_URL`、`VITE_ASR_WS_URL`、`VITE_DOWNLOAD_SHARED_OBJECT_BASE` 及与 `live2d_info.httpBase` 的优先级 | `Demo/src/api/apiBase.js`、`wsConfig.js` |
| **生产环境安全** | HTTPS/WSS、密钥仅放服务端 `.env`、MinIO **预签名 TTL**、用户密码存储方式（若有哈希实现需在正文一笔带过） | `后端项目正文`、`远程Live2D资源MinIO…` |
| **主动关怀** | `remind_trigger` 表与 REST 已实现；**浏览器推送通道**若未接 Web Push/短信，应在论文写清「数据与接口具备，触达方式可扩展」 | `后端项目正文` §9.6、`架构与时序图` 用例图 |
| **语音识别** | **已实现路径为 `/ws/asr` + DashScope**，非浏览器 Web Speech API | **`SpeechRecognition.md`**（已按本仓库校正） |
| **局限与对比** | 合并流 vs 双通道 TTS、ASR 供应商绑定、长记忆固化依赖 Ollama 可用性等 | `wschat-TTS流式流水线…`、`聊天双层记忆…` |
| **`live2d_info` 本地缓存** | 登录后写入字段（如 `user_id`、`username`、`httpBase`）与 **`wsConfig` `user_id` 对齐**的重要性 | `前端项目正文`、`Login.html` |
| **静态资源路径** | `index.html` 引用 **`./dist/Core/live2dcubismcore.js`** 与 **`./src/main.js`**：开发与构建后目录需一致，避免 Core 404 | `Demo/index.html` |


**与课题实现的对应关系（不写路径，仅作分工提示）**

- 浏览器端：Vite、Live2D Cubism Web、主页面与即时通讯式布局、WebSocket 客户端、录音与识别、登录态（未登录跳转登录页）、按需 presigned 加载远程模型资源、左侧会话面板拉取历史 `chat_session`。
- 服务端：FastAPI（含 **CORS**，可通过环境变量追加允许的 Origin）、`/ws/chat` 与 `/ws/tts` 对话与 TTS 编排（支持 MiMo 合并流下 **`chunk_audio`**）、语音识别 WebSocket、Resources 目录索引与对象存储资源索引、GPT-SoVITS / MiMo 等合成客户端；进程 **`lifespan`** 内启动 **长期记忆后台固化任务**（周期性扫描 `chat_session`，写入 `long_memory.period_overview` 并刷新 Redis 长期正文）。
- 数据层：MySQL 建表与 `/api` REST；Redis 承载瞬时/短期记忆与长期记忆 prompt 片段。
