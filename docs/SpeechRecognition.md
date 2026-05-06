# SpeechRecognition 语音识别工具说明

本文档说明本仓库 **`Demo/src/utils/SpeechRecognition.js`**（单例 **`SpeechRecognizer`**）的实现：**浏览器麦克风 PCM → WebSocket `/ws/asr` → 服务端转发阿里云 DashScope Fun-ASR**，**不使用**浏览器内置 Web Speech API。

更完整的端到端时序见 **`docs/架构与时序图.md`** §1.2。

---

## 1. 概述

| 项目 | 说明 |
|------|------|
| **协议** | 前端与 **`PY/router/asr_ws.py`** 暴露的 **`/ws/asr`** 建立 WebSocket；上行 **二进制 PCM**，下行 **JSON** |
| **采样** | 麦克风经 **AudioContext** 采集；若非 16 kHz，在前端 **`downsampleTo16k`** 转为 **16 kHz**，再 **`floatTo16BitPCM`** 为小端 **Int16** |
| **云端引擎** | **`fun-asr-realtime`**（见服务端 `Recognition` 配置） |
| **回调** | **`start(onResult)`** 传入 **`(text, isFinal) => void`**；**`isFinal`** 对应服务端 JSON 的 **`partial === false`**（一句结束） |

---

## 2. 服务端下行 JSON 形态

由 **`asr_ws`** 转发 DashScope 回调（句末为最终结果）：

| 字段 | 含义 |
|------|------|
| **`text`** | 当前识别文本 |
| **`partial`** | **`true`**：中间结果；**`false`**：该句最终结果 |
| **`error`** | 异常信息字符串；前端会 **`alert` 并 stop** |

---

## 3. 前端 API

| 方法 | 作用 |
|------|------|
| **`start(onResult?)`** | 连接 **`getAsrWsUrl()`**（默认同源 **`/ws/asr`**，见 **`wsConfig.js`**），打开麦克风并开始 **`onaudioprocess`** 发送 PCM |
| **`stop()`** | 发送 **`{"cmd":"flush"}`**，释放音频节点与 WebSocket |
| **`isRecognizingNow()`** | 是否处于识别中 |
| **`isSupported()`** | 是否具备 **`getUserMedia`** 与 **`WebSocket`** |

说明：源码里部分日志仍写「Vosk」字样，为历史遗留；**实际链路为 DashScope**。

---

## 4. 配置与环境变量

| 变量 | 作用 |
|------|------|
| **`DASHSCOPE_API_KEY`** | 服务端必选；未设置时 **`/ws/asr`** 返回 **`error`** 并关闭连接 |
| **`VITE_ASR_WS_URL`** | 前端可选：覆盖 **`/ws/asr`** 完整 WebSocket URL |
| **`pip install dashscope`** | 服务端依赖；未安装时 **`/ws/asr`** 提示安装 |

---

## 5. 与主对话衔接

**`main.js`** 中通常在 **`isFinal && text`** 时调用 **`sendChatMessage(text)`**，后续与 **文本对话** 相同： **`/ws/chat`** → Ollama → TTS 等（见 **`docs/架构与时序图.md`** §1.1）。

---

## 6. 注意事项

1. **麦克风权限**：HTTPS 或 localhost；用户需允许麦克风。  
2. **与 AudioRecorder**：若页面同时使用 **`AudioRecorder`**，注意并行占用麦克风策略（以 **`main.js`** 实际绑定为准）。  
3. **答辩/论文**：可强调「**端侧仅上传 PCM，识别在云端完成**，便于替换模型商」；局限性包括 **网络延迟**、**API Key 运维**、**供应商绑定（DashScope）**。
