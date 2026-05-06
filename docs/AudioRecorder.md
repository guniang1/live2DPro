# AudioRecorder 录音工具说明

本文档介绍 `Samples/JS/Demo/src/utils/AudioRecorder.js` 的录音流程、API 及使用方式。

---

## 1. 概述

`AudioRecorder` 使用浏览器内置的 **MediaRecorder API** 实现麦克风录音，无需额外依赖。

| 属性 | 说明 |
|------|------|
| `mediaRecorder` | MediaRecorder 实例，控制录音的开始、停止等 |
| `audioChunks` | 数组，存储录音产生的音频数据块 |
| `stream` | MediaStream，麦克风音频流 |

---

## 2. start() 之前的步骤

`this.mediaRecorder.start()` 放在最后，前面的步骤是**准备录音环境**：

### 步骤 1：获取麦克风音频流

```javascript
this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
```

- 向浏览器申请麦克风权限
- 用户同意后返回 `MediaStream`（麦克风实时音频流）
- 必须先拿到流才能录音

### 步骤 2：创建 MediaRecorder 实例

```javascript
this.mediaRecorder = new MediaRecorder(this.stream);
```

- 用音频流创建 `MediaRecorder`
- 此时只是创建对象，尚未开始录制

### 步骤 3：清空之前的录音数据

```javascript
this.audioChunks = [];
```

- 避免把上一次的录音混入本次

### 步骤 4：注册数据回调

```javascript
this.mediaRecorder.ondataavailable = (e) => {
    if (e.data.size > 0) this.audioChunks.push(e.data);
};
```

- 设置 `ondataavailable` 回调
- 录音过程中，每次有数据就触发，将数据追加到 `audioChunks`
- **必须在 start() 之前设置**，否则可能收不到数据

### 步骤 5：开始录音

```javascript
this.mediaRecorder.start();
```

- 真正开始录制
- 之后麦克风数据会通过 `ondataavailable` 写入 `audioChunks`

### 流程概览

```
1. getUserMedia()        → 获取麦克风流
2. new MediaRecorder()   → 创建录音器
3. 清空 audioChunks     → 准备存储
4. 设置 ondataavailable  → 接收录音数据
5. start()               → 开始录制
```

---

## 3. 内置函数 / 自定义函数对照

### 3.1 内置函数（浏览器 API）

| 来源 | 函数/属性 | 作用 | 结果 |
|------|-----------|------|------|
| `navigator.mediaDevices` | `getUserMedia({ audio: true })` | 申请麦克风权限，获取音频流 | `Promise<MediaStream>` |
| `MediaRecorder` 构造函数 | `new MediaRecorder(stream)` | 创建录音器实例 | `MediaRecorder` 实例 |
| `MediaRecorder` | `start()` | 开始录制 | `void` |
| `MediaRecorder` | `stop()` | 停止录制，触发 `onstop` | `void` |
| `MediaRecorder` | `ondataavailable`（事件） | 有新的音频数据时触发 | 回调参数 `e.data` 为 Blob |
| `MediaRecorder` | `onstop`（事件） | 录音停止时触发 | `void` |
| `MediaRecorder` | `state`（属性） | 当前状态 | `"inactive"` / `"recording"` / `"paused"` |
| `Blob` 构造函数 | `new Blob(chunks, { type })` | 将音频块合并为 Blob | `Blob` 对象 |
| `MediaStream` | `getTracks().forEach(t => t.stop())` | 停止麦克风轨道 | `void` |
| `Promise` | `new Promise((resolve, reject) => {...})` | 创建异步 Promise | `Promise` 实例 |
| `resolve`（Promise 参数） | `resolve(value)` | 标记 Promise 成功并传出结果 | `void` |

### 3.2 自定义函数（AudioRecorder 类）

| 函数 | 作用 | 结果 |
|------|------|------|
| `start()` | 获取麦克风、创建 MediaRecorder、开始录音 | `Promise<void>` |
| `stop()` | 停止录音，合并 audioChunks 为 Blob | `Promise<Blob>` |
| `isRecording()` | 判断是否正在录音 | `boolean` |

---

## 4. 音频流（Audio Stream）说明

**音频流** 指持续产生的音频数据，像水流一样一段段传输，而非一次性完整文件。

- `getUserMedia` 返回的是**麦克风实时音频流**
- 特点：实时、连续、流式
- `MediaRecorder` 接收该流，边录边存到 `audioChunks`，最终可合并为 Blob 文件

---

## 5. 使用示例

```javascript
import AudioRecorder from './utils/AudioRecorder.js';

// 开始录音
await AudioRecorder.start();

// 停止录音，获取 Blob
const blob = await AudioRecorder.stop();

// 判断是否正在录音
if (AudioRecorder.isRecording()) {
    // ...
}
```

---

## 6. resolve 是回调函数吗？

`stop()` 中使用了 `new Promise((resolve, reject) => {...})`，这里的 `resolve` 是什么？

### 角色

`resolve` 是 **Promise 传入的“完成函数”**，不是传统意义上的回调，但用法类似“完成时调用的函数”。

```javascript
return new Promise((resolve, reject) => {
    this.mediaRecorder.onstop = () => {
        const blob = new Blob(this.audioChunks, { type: "audio/webm" });
        resolve(blob);  // 调用 resolve，表示“成功完成”，并把 blob 传出去
    };
    this.mediaRecorder.stop();
});
```

- `resolve`：表示“成功完成”，把结果传给 `.then()` 或 `await`
- `reject`：表示“失败”，把错误传给 `.catch()` 或 `await` 的异常

### 与回调函数的区别

| 概念 | 说明 |
|------|------|
| **resolve** | Promise 提供的函数，你**调用它**来通知“完成了” |
| **回调函数** | 你**传入**的函数，由别人在合适时调用 |

```javascript
// 回调：你传入，别人调用
setTimeout(() => console.log("1秒后"), 1000);

// resolve：Promise 传入给你，你在完成时调用
new Promise((resolve) => {
    setTimeout(() => resolve("完成"), 1000);
});
```

**总结**：`resolve` 是 Promise 给你的函数，用来“标记成功并传出结果”，可理解为一种“完成回调”，更准确的说法是 **Promise 的 resolve 函数**。

---

## 7. 注意事项

1. **HTTPS**：`getUserMedia` 在非 localhost 的 HTTP 下可能被限制
2. **麦克风权限**：首次使用会弹出授权
3. **格式**：默认 `audio/webm`，如需 mp3 需额外库
