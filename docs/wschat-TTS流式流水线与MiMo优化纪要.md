# WebSocket 聊天 TTS：问题、方案与配置纪要

**文档性质**：本轮迭代的后端／前端／环境变量变更记录，便于复查与交接。  
**说明**：本文为工程内部说明，**不采用** Chicago／MLA 等出版物引文体系；代码与配置出处以仓库内路径为准（见文末「涉及文件」）。

---

## MiMo 语音合成（`PY/utils/tts.py` → `mimo_tts`）与对话 LLM 是两条链路

**MiMo 语音合成**走的是小米开放平台的 **`chat/completions`** 形态，**不是**在跑侧边对话用的聊天模型（例如 Ollama）。在该形态下，仓库约定为：

- **`assistant`**：固定放 **待朗读的正文**（对白）。
- **`user`**：放 **自然语言导演 / 人设 / 语气**（实现中为 **【人设】**、**【语气】**，来自 MySQL `persona` 的 `character_desc` / `tone_style`）。

这样接口语义很清楚：**一句 user =「怎么播」**，**一句 assistant =「播什么」**。官方示例同样倾向于把风格、指令放在 **user**、把待合成句子放在 **assistant**，因此人设与语气放在 **user** 侧；这与 **对话 LLM 的 `system`**（在 `wschat` 里拼接的人设段落）**无关**——**MiMo 请求根本碰不到那条聊天的 system**。

**本仓库的补充**：当携带数据库导演正文时，可在 **`messages` 最前**增加一条 **`system`**，使用固定短句（默认「你是语音合成助手，请按照【人设】与【语气】合成语音。」，可由环境变量 **`MIMO_TTS_SYSTEM_PROMPT`** 覆盖）；**【人设】【语气】的正文仍在 user**，assistant 仍为待朗读对白。

---

## 1. MiMo 音色克隆分段合成偏慢、Payload 偏大

### 问题

- 使用 **`mimo-v2.5-tts-voiceclone`** 时，单次 HTTP 请求体约 **数百 KB**（`voice_field_chars` 可达数十万字符）：JSON 中 **`audio.voice`** 需携带 **`data:audio/…;base64,…`** 形式的参考音频。
- **按句／按标点切段**时，每一段都会再次带上整块参考音频，导致 **重复上行**、总耗时长、偶发 **读超时**（日志中 `mimo_http retry` … `TimeoutError`）。
- 磁盘上的参考文件（例如约 **515 KB**）经 Base64 膨胀后，与日志里的 **`voice_field_chars`** 量级一致；**非**「重复从磁盘读取 N 倍文件」——服务端可对 Data URL 做内存／Redis 缓存（**`MIMO_VOICE_DATAURL_CACHE`**），但每次请求仍要把整段 Voice 字段 **发往云端**。

### 方案

1. **可选整轮单次合成**  
   - 环境变量 **`MIMO_TTS_WS_SINGLE_SHOT=1`**（显式开启）：在 LLM **全文生成结束后**，只对 **整段助手回复** 调用一次 **`mimo_tts`**，下发 **一条** **`chunk_audio` + WAV**。  
   - **权衡**：减少请求次数与上行次数；**首段可播时间**延后到全文就绪之后。

2. **调试日志字段含义**（**`TTS_DEBUG=1`** 时 **`mimo_http req begin`** 一行）  
   - **`attempt`**：当前重试次序／上限。  
   - **`payload_bytes`**：UTF-8 JSON 正文长度。  
   - **`voice_field_chars`**：**`audio.voice`** 字符串长度（克隆模式下极大）。  
   - **`text_chars` / `assistant_chars` / `user_chars`**：调用参数与请求内 **`messages`** 各段长度（含压平、风格前缀等差异）。  
   - **`timeout_s`**：**`MIMO_TTS_TIMEOUT`**（单次 HTTP）。

---

## 2. 切段策略：按标点攒批

### 问题

- 每遇到一个标点就送 TTS，请求次数多，与大 Payload 叠加后整体更慢。

### 方案

- **`TTS_FLUSH_EVERY_N_SENTENCE_END`**：累计 **`_SENTENCE_PUNC`** 中标点达到 **N** 次后，将当前缓冲 **整段** 入队合成。  
- **未配置环境变量时**，代码默认 **N = 4**（可通过 `.env` 改为 `1` 等）。  
- **注意**：**`N > 1`** 的攒批路径 **不再** 套用 **`TTS_MIN_CHARS_PER_CHUNK`**；逗号等也在标点集合内，中文逗号较多时会较快凑满 **N**。

---

## 3. 流式文本与合成并行（流水线）

### 问题

- 期望：**一段文本攒满即触发 TTS**，继续接收后续 token **不必等待** 上一段 **音频合成完成**；前端仍 **按分段序号顺序** 播放。

### 方案（既有架构 + 显式并行度）

- **文本协程**只做 **`await sentence_queue.put((index, segment))`**，**不等待** `mimo_tts`／`gpt_sovits_tts` 返回。  
- **多个 `tts_worker`** 从同一队列取任务，**并行** **`asyncio.to_thread(...)`** 合成；完成顺序任意。  
- **`tts_completed` + `_tts_flush_ordered`** 按 **`segment index`** 递增向 WebSocket 发送 **`chunk_audio`** 与二进制 WAV，保证前端顺序。  
- **新增 **`TTS_STREAM_PIPELINE_SLOTS`（1～8）**：若设置，则 **覆盖** **`TTS_PARALLEL_WORKERS`**／**`TTS_PARALLEL_WORKERS_MIMO`**，用于声明 **并行合成路数**。  
- MiMo 云端易 **429**：并行度过高需自担风险；可与 **`MIMO_TTS_WS_SINGLE_SHOT`**、限流退避等策略权衡。

---

## 4. 前端：语速偏快与分段衔接「抽搐」

### 问题

- 多段 WAV 用多个 **`<audio>`** 顺序播放时，段间存在微小间隙；口型在每段 **`ended`** 时被置零，易产生 **顿挫**。  
- 合成内容主观 **偏快** 时，浏览器端未统一降速。

### 方案（**`Demo/src/api/ws.js`**）

- 在支持 **Web Audio API** 时： **`decodeAudioData` + `AudioBufferSourceNode`**，按 **`AudioContext.currentTime`** **无缝排程**下一段起点（**`ttsNextScheduleTime`**），减少断档。  
- 默认 **`playbackRate = 0.88`**（可通过 **`globalThis.__TTS_PLAYBACK_RATE__`** 覆盖）。  
- **`AnalyserNode.smoothingTimeConstant`** 适度增大，减轻口型 RMS 抖动。  
- 无 AudioContext 时回退 **`<audio>`**，同样设置 **`playbackRate`**。  
- 打断／重连：**`ttsPlaybackGeneration`** 递增并 **`stop()`** 已排程音源，避免旧会话泄漏。

> **与既有文档的关系**：口型仍由 **AnalyserNode → RMS → `feedChatLive2dTtsAudioLipLevel`** 驱动；具体播放管线已由「MediaElement 队列」升级为「BufferSource 时间轴队列」，详见 **`聊天TTS与Live2D口型同步.md`** 中的概念小节，实现细节以 **`ws.js`** 为准。

---

## 5. 主界面移除 `#output` 气泡

### 问题

- Live2D 区域不再需要 **`#output`** 悬浮文本层。

### 方案

- **`Demo/index.html`**：**`<main id="live2d-panel">`** 内去掉 **`#output`**，并删除相关 CSS。  
- **`Demo/src/api/ws.js`**：**`chunk` / `chunk_audio`** 不再依赖 **`#output`** 是否存在，保证 **`#chat-list`** 流式更新；错误帧在无 **`#output`** 时仍更新聊天列表。  
- **`Demo/src/lappdelegate.js`**：点击穿透判断去掉对已删除节点的 **`#output`** 引用。

---

## 6. 环境变量速查

| 变量 | 作用提要 |
|------|-----------|
| **`MIMO_TTS_WS_SINGLE_SHOT`** | `1`：全文结束后单次 MiMo 合成。 |
| **`TTS_FLUSH_EVERY_N_SENTENCE_END`** | 每 **N** 个标点攒一批再 TTS；默认代码侧 **4**（若未设 env）。 |
| **`TTS_STREAM_PIPELINE_SLOTS`** | 并行 **`tts_worker`** 数量（优先于 **`TTS_PARALLEL_WORKERS*`**）。 |
| **`TTS_PARALLEL_WORKERS_MIMO`** / **`TTS_PARALLEL_WORKERS`** | MiMo／默认并行 worker（未被 pipeline slots 覆盖时生效）。 |
| **`MIMO_VOICE_DATAURL_CACHE`** | 参考音 Data URL 缓存（减轻重复编码，**不减少**上行 Payload 体积）。 |
| **`MIMO_TTS_TIMEOUT`** | 单次 MiMo HTTP 超时（秒）。 |
| **`TTS_DEBUG`** | `1`：输出 **`mimo_http`** 等调试日志。 |

---

## 7. 涉及文件（仓库路径）

| 路径 | 变更类型 |
|------|-----------|
| **`PY/router/wschat.py`** | 单次合成开关、标点默认、流水线 **`TTS_STREAM_PIPELINE_SLOTS`**、文档字符串与注释。 |
| **`PY/utils/tts.py`** | （既有）MiMo 请求／调试字段；参考音缓存逻辑。 |
| **`PY/.env`** | 注释与可选示例（勿将密钥提交公开仓库）。 |
| **`Demo/src/api/ws.js`** | Web Audio 无缝播放、`playbackRate`、流水线兼容的前端队列与打断。 |
| **`Demo/index.html`** | 移除 **`#output`** 及样式。 |
| **`Demo/src/lappdelegate.js`** | **`#output`** 相关交互选择器移除。 |

---

## 8. 数据化验证脚本（仓库内）

在 **`PY`** 目录执行：

| 脚本 | 作用 |
|------|------|
| **`scripts/benchmark_streaming_tts_metrics.py`** | **默认 dry-run**：对齐 **`wschat`** 切段规则，输出 **段数、总字数、串行/多 worker FCFS 墙钟估算、克隆 Payload 总 MB**；支持 **`--every-n-end 1 4`**、**`--json`**；结果追加 **`PY/logs/streaming_tts_metrics.jsonl`**。可选 **`--live-mimo`** 对前几段真实调用 MiMo（慎用）。 |
| **`scripts/compare_streaming_tts_flush.py`** | 仅对比 **every_n_end=1 vs 3** 切块与粗延迟模型（不调 API）。 |
| **`scripts/benchmark_tts.py`** | 单段 **GPT-SoVITS / MiMo** 冷启动与多次采样耗时（与流式切段无关）。 |

调参建议：将 **`benchmark_streaming_tts_metrics.py`** 的 **`--latency-base-ms`** / **`--latency-per-char-ms`** 对齐日志里 **`wschat_mimo_tts_fin wall_ms`** 与 **`text_chars`**，便于 dry-run 接近实测。

---

## 9. 修订记录

| 日期 | 说明 |
|------|------|
| 2026-05-05 | 初稿：汇总 MiMo 载荷与单次合成、标点攒批、并行流水线、前端播放与 UI 调整。 |
| 2026-05-05 | 增补 §8：流式指标基准脚本与日志路径。 |
| 2026-05-05 | 文首增补：MiMo `chat/completions` 下 user／assistant 与对话 LLM `system` 的分工说明。 |
