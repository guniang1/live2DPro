# 聊天 TTS 与 Live2D 口型同步

本文说明 **`/ws/chat`** 流式回复中 **文本、`chunk_audio` 与二进制 WAV** 的到达顺序，以及 **口型由 TTS 音频音量驱动**（而非根据文本模拟）的前后端约定与调参位置。

---

## 1. 行为概要

| 项目 | 说明 |
|------|------|
| **口型数据来源** | 浏览器端对 **正在播放的 TTS WAV** 做 **Web Audio `AnalyserNode`** 时域采样，计算 **RMS**，映射为 **0～1** 的强度，写入 Live2D 嘴部参数。 |
| **不再使用** | 根据流式 **文本 chunk** 用正弦节奏 **`triggerTextLipSync`** 模拟张嘴（已停用，避免与真实朗读不同步）。 |
| **纯文本模式** | 若后端仅推送 **`chunk`**、无 **`chunk_audio` + WAV**，则 **不会** 因文本对口型（符合「只听音频」策略）。 |
| **与动作配音** | 模型动作自带的 `.wav` 仍通过 **`LAppWavFileHandler`**（`_lipsync` 为真时）走原有 RMS 路径；与聊天 TTS 口型在 **`LAppModel.update`** 中取 **`Math.max`**，二者可并存。 |

---

## 2. 后端：`/ws/chat` 帧类型（与口型相关）

- **`chunk`**：JSON，`content` 为一段可见文本；可用于 UI 与表情/动作字段。
- **`chunk_audio`**：JSON，除 `content` 外声明 **`index`、`size`**；**下一帧**必须为 **二进制**，且为合法 **RIFF/WAVE**，字节数与 `size` 一致。
- **`done`**：本轮结束；前端应停止口型采样并复位（见下）。

TTS 并行合成与有序下发逻辑见 **`PY/router/wschat.py`**（`sentence_queue`、`tts_worker`、`merged_stream` 等）。

### 2.1 流式口型序号变量

消费流式文本并按句送 TTS 时，切段序号 **`tts_sentence_index`** 须在循环前 **初始化为 `0`**，再在每次 flush 时 **`+= 1`**，否则首次切段会触发 **`UnboundLocalError`**（曾被外层异常日志误标为「Ollama 调用失败」）。

---

## 3. 前端：音频口型数据流

1. **`Demo/src/api/ws.js`**  
   - 收到二进制 WAV 后 **`enqueueAudioAndPlay`**。  
   - **`playAudioQueue`** 内：共享 **`AudioContext`**（懒创建，`resume()` 应对自动播放策略）、**`createMediaElementSource(audio)`** → **`AnalyserNode`** → **`destination`**。  
   - **`requestAnimationFrame`** 循环中 **`getByteTimeDomainData`** → RMS → **`feedChatLive2dTtsAudioLipLevel(level)`**。  
   - 每段播放结束 **`finally`**：**取消 RAF**、**`feedChatLive2dTtsAudioLipLevel(0)`**、断开节点；队列清空后同样归零。  
   - **`resetStreamingState`**（用户打断重发等）：**`stopTtsLipRaf`** + **`stopChatLive2dLipSync`**，避免旧会话口型残留。

2. **`Demo/src/lappdelegate.js`**  
   - **`feedChatLive2dTtsAudioLipLevel(level)`**：委托到当前 **`LAppLive2DManager`**。

3. **`Demo/src/lapplive2dmanager.js`**  
   - **`feedTtsAudioLipLevel`** → **`LAppModel.setTtsAudioLipLevel`**。  
   - **`stopLipSync`**：**`clearTtsAudioLip(true)`**（立即闭嘴）并保留 **`stopTextLipSync`** 兼容调用。

4. **`Demo/src/lappmodel.js`**  
   - **`_ttsAudioLipTarget` / `_ttsAudioLipSmoothed`**：按 **`deltaTime`** 做平滑后与 WAV 动作口型一起参与 **`lipSyncValue`**。  
   - **`setTtsAudioLipLevel` / `clearTtsAudioLip`**：供 TTS 专用。

### 3.1 调参

- **口型幅度**：`ws.js` 中 RMS 映射系数（如 **`rms * 4.5`**），过大则嘴张得过开，过小则不明显。  
- **跟随速度**：`lappmodel.js` 中 **`ttsTau`**（与 **`deltaTimeSeconds * ttsTau`** 相关），越大越快贴近目标。

### 3.2 句末定时器与表情

**`scheduleStopOnTextTail`** 仅在句末标点后延迟触发 **`resetChatLive2dExpression`**；**不再**在该定时器内 **`stopChatLive2dLipSync`**，以免 **文本已显示句末而音频仍在播放** 时嘴突然闭合。

---

## 4. MinIO：预签名与 `region`（参考音频等）

**`PY/live2d_db/minio_storage.py`** 创建 **`Minio`** 客户端时传入 **`region`**（默认 **`us-east-1`**，可通过 **`MINIO_REGION`** 覆盖）。

作用：生成 **`presigned_get_object`** 时 **MinIO Python SDK** 否则会发起 **`GetBucketLocation`**；若本机 **`MINIO_ENDPOINT`** 未监听（例如 MinIO 未启动），会连接失败。**显式 region** 可避免此次网络探测。

上传、`bucket_exists` 等仍依赖 MinIO 服务可达。

---

## 5. 相关文件索引

| 角色 | 路径 |
|------|------|
| 聊天 WS、音频队列、Analyser、RAF | `Demo/src/api/ws.js` |
| 委托导出 | `Demo/src/lappdelegate.js` |
| 管理器转发 | `Demo/src/lapplive2dmanager.js` |
| 嘴部参数混合与平滑 | `Demo/src/lappmodel.js` |
| 聊天与 TTS 编排 | `PY/router/wschat.py` |
| MinIO 客户端与预签名 | `PY/live2d_db/minio_storage.py` |

更通用的前端架构见 **`docs/前端项目正文.md`**；工程级 WS 与人设见 **`docs/文档.md`**。
