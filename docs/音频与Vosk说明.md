# 音频与实时语音识别说明

本文说明 Demo 中 `SpeechRecognition.js` 涉及的 **单声道**、**Float32**、**重采样** 等概念，以及它们在本项目中的关系。

相关代码：`Samples/JS/Demo/src/utils/SpeechRecognition.js`、`Samples/JS/Demo/src/api/wsConfig.js`；服务端：`PY/router/asr_ws.py` 的 **`/ws/asr`**（阿里云 DashScope **Fun-ASR 实时识别**，需环境变量 **`DASHSCOPE_API_KEY`**）。

---

## 单声道（Mono）

声音可以按 **声道数** 录制与播放：

- **立体声（双声道）**：左、右各一路信号，常见于音乐。
- **单声道**：只有 **一路** 混合信号。

实时识别服务按 **单路麦克风 PCM** 使用，因此 `getUserMedia` 中设置 `channelCount: 1`，只取 **一条声道**。

---

## Float32（32 位浮点样本）

浏览器 **Web Audio API** 中，`AudioBuffer` / `ScriptProcessorNode` 读到的样本通常是 **`Float32Array`**：

- 每个采样点是一个约 **−1.0～1.0** 的浮点数，表示该时刻波形的 **归一化振幅**。
- 代码中 `getChannelData(0)` 得到的即为 **float32 格式** 的波形数据。

服务端需要的是 **16 bit 小端 PCM 字节**（`format=pcm`），因此前端在发送前会用 `floatTo16BitPCM` 从 float32 转为 **Int16** 再发送。

---

## 重采样（Resampling）

**采样率**表示每秒采多少个样本，例如 **48000 Hz** 即每秒 48000 个采样点。

- 浏览器/声卡常见 **44100 Hz、48000 Hz** 等。
- DashScope **fun-asr-realtime** 使用 **16000 Hz（16 kHz）** PCM。

**重采样**：把某一采样率下得到的一串样本，**换算**成 **16 kHz** 的样本序列。

代码中的 `downsampleTo16k` 将当前 `AudioContext` 的采样率（如 48 kHz）下的缓冲 **降为 16 kHz**，再交给后续 PCM 编码与 WebSocket 发送。

---

## 在本项目中的数据流（概要）

```
麦克风 → 单声道 float32 缓冲 → 重采样到 16 kHz → 转为 16 bit PCM 二进制
    → WebSocket（/ws/asr）→ FastAPI → DashScope Fun-ASR → JSON 文本回调
```

一句话：**单声道**是通道数；**float32** 是浏览器里的样本表示；**重采样**是把时间轴上的采样密度改成 **16 kHz**，以符合云端识别要求。

---

## 相关配置

- WebSocket 基地址见 `src/api/wsConfig.js`（默认 `ws://localhost:8000`，与 `python main.py` 一致）。
- 后端配置集中在 **`PY/.env`**（可复制 `PY/.env.example`），由 `main.py` 的 `load_dotenv` 加载。常用项：
  - **`DASHSCOPE_API_KEY`**：百炼语音识别（**不要**提交到 Git）。
  - **`OLLAMA_HOST`**、**`OLLAMA_MODEL`**、**`NO_PROXY`**：与 `/ws/chat` 对话相关。
- 单独覆盖语音识别完整 WebSocket 地址时，可设置 **`VITE_ASR_WS_URL`**（Vite 构建时注入）。
