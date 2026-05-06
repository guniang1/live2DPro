# GPT-SoVITS 与 PY 服务对接（HTTP）

`PY`（本仓库 `PY/main.py`，默认端口 `8000`）与 GPT-SoVITS **应作为两个独立进程** 运行：先在本机启动 GPT-SoVITS 的 **`api.py` HTTP 服务**，再在 FastAPI 里用 **HTTP 客户端** 请求音频流。

官方接口说明见 GPT-SoVITS 仓库内 `api.py` 顶部注释；默认监听 **`127.0.0.1:9880`**（可用启动参数 `-p` 修改）。

---

## 1. 先启动 GPT-SoVITS API

整合包若只启动了 **`go-webui.bat`（WebUI）**，并不能直接替代 **`api.py`**。要让 `PY` 通过 HTTP 调用，需要另行在同一目录启动 **`api.py`**。

在 **GPT-SoVITS 解压目录（整合包根目录）** 下打开终端，使用整合包自带的解释器（路径以你本机为准）：

```text
.\runtime\python.exe api.py -a 127.0.0.1 -p 9880 -dr "参考音频.wav" -dt "参考音频对应的文字。" -dl "zh"
```

说明：

- `-dr` / `-dt` / `-dl`：未在每次请求里传参考音频时使用的**默认参考音频**（路径、文本、语言）；语言可用 `zh` / `en` / `ja` 等。
- 已训练好的 **SoVITS / GPT 权重** 路径可在同目录 `config.py` 或 `-s` / `-g` 指定（见上游文档）。

看到控制台提示监听 `9880` 后，再用下面的 PY 代码调用。

---

## 2. 在 `PY/.env` 中配置基址（可选）

```env
GPTSOVITS_API_BASE=http://127.0.0.1:9880
```

---

## 3. 最小调用示例（标准库，无额外依赖）

成功时接口返回 **WAV 二进制**（HTTP 200）；失败时常为 JSON + 400。

```python
import json
import os
import urllib.error
import urllib.request

def gpt_sovits_tts(
    text: str,
    *,
    text_language: str = "zh",
    base: str | None = None,
    timeout_s: float = 300.0,
) -> bytes:
    base = (base or os.getenv("GPTSOVITS_API_BASE") or "http://127.0.0.1:9880").rstrip("/")
    url = base + "/"
    payload = {"text": text, "text_language": text_language}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GPT-SoVITS API HTTP {e.code}: {err_body}") from e


if __name__ == "__main__":
    wav_bytes = gpt_sovits_tts("你好，这是测试。")
    with open("out.wav", "wb") as f:
        f.write(wav_bytes)
```

---

## 4. 在 FastAPI 路由里转发出一段 WAV（示例）

以下为**独立示例片段**，非已合并进 `main.py`；可按需拷贝到 `router/` 下某模块并 `include_router`。

```python
import os
from fastapi import APIRouter, Response

from gpt_sovits_client import gpt_sovits_tts  # 上一节函数可放到例如 PY/gpt_sovits_client.py

router = APIRouter()

@router.post("/tts/gpt-sovits")
async def tts_proxy(text: str, text_language: str = "zh"):
    # 若需严格 async，可改为 httpx.AsyncClient；此处同步函数亦可放在线程池以免阻塞事件循环
    wav = gpt_sovits_tts(text, text_language=text_language, base=os.getenv("GPTSOVITS_API_BASE"))
    return Response(content=wav, media_type="audio/wav")
```

说明：长推理会占用事件循环时，建议用 `asyncio.to_thread(gpt_sovits_tts, ...)` 或 **httpx 异步客户端**。

---

## 5. 单次请求携带参考音频（本机路径）

若默认参考音频未通过 `api.py` 启动参数设置，可在 POST JSON 中加入（路径须能被 **GPT-SoVITS 进程** 访问，一般是 Windows 本机绝对路径）：

```json
{
  "refer_wav_path": "E:/path/to/ref.wav",
  "prompt_text": "参考音频对应的文本。",
  "prompt_language": "zh",
  "text": "要合成的目标文本",
  "text_language": "zh"
}
```

---

## 6. 常见问题

| 现象 | 处理 |
|------|------|
| `Connection refused` | 确认 `api.py` 已启动且端口与 `GPTSOVITS_API_BASE` 一致。 |
| 400 / JSON 错误 | 检查默认参考音频是否设置、模型路径、`text_language` 是否与内容匹配。 |
| PY 为 Python 3.11+，GPT-SoVITS 为 3.9/3.10 | **无需统一版本**；两边进程独立，只要 HTTP 互通即可。 |

更全参数（切分符号、流式、半精度等）见上游 **`api.py` 文件头部文档字符串**。

---

## 7. LLM 流式文本 -> 标点触发 TTS（推荐流程）

目标：让大模型边生成文字，后端边做语音，遇到断句标点再发声，减少“半句话就说出来”的违和感。

### 7.1 核心状态

- 全局缓冲 `A`（字符串）：累计 LLM 流式 token。
- 标点集合：`。！？.!?；;`（可按业务扩展）。
- 可选兜底阈值：`max_tokens_without_punc`（防止长时间无标点导致不出声）。

### 7.2 主流程

1. 启动 LLM 流式输出（例如 Ollama `stream=True`）。
2. 每收到 token：`A += token`。
3. 若 token 是断句标点（或达到兜底阈值）：
   - 调用 `gpt_sovits_tts(A)`；
   - 将返回音频发送给前端（或落盘）；
   - 清空缓冲：`A = ""`。
4. LLM 结束后，若 `A` 非空，再补一次 TTS。

### 7.3 伪代码

```python
A = ""
for token in llm_stream():
    A += token
    if is_sentence_punc(token) or token_count(A) >= MAX_TOKENS_WITHOUT_PUNC:
        wav = gpt_sovits_tts(A)
        emit_audio(wav)   # websocket推流 / 文件写入 / 播放队列
        A = ""

if A:
    wav = gpt_sovits_tts(A)
    emit_audio(wav)
```

### 7.4 与当前项目对应

- 文本流来源：`PY/router/wschat.py`（当前已是 Ollama 流式）。
- TTS 调用：`PY/utils/tts.py` 中 `gpt_sovits_tts()` / `llm_to_tts_stream()`。
- 当前建议模式：`flush-mode=punc`（仅标点触发）；如需兜底可切 `mixed`。


```mermaid

flowchart TD
    A[开始: 用户发起对话] --> B[后端请求 LLM stream=True]
    B --> C{收到新 token?}
    C -->|是| D[A += token]
    C -->|否| Z{LLM 已结束?}

    D --> E{token 是断句标点?}
    E -->|是| F[调用 GPT-SoVITS TTS(A)]
    E -->|否| G{达到兜底阈值?\n可选: 长时间无标点}
    G -->|是| F
    G -->|否| C

    F --> H{TTS 成功?}
    H -->|是| I[输出音频 chunk\nWS推送/落盘/播放队列]
    H -->|否| J[记录错误并继续主流程]

    I --> K[清空缓冲 A = ""]
    J --> K
    K --> C

    Z -->|是| L{A 是否非空?}
    Z -->|否| C
    L -->|是| M[补一次 TTS(A)]
    L -->|否| N[结束]

    M --> O[输出最后音频 chunk]
    O --> N[结束]

```