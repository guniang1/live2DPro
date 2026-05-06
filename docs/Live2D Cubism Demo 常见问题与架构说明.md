# Live2D Cubism Demo 常见问题与架构说明

本文档整理自对 Cubism SDK Web 示例项目的常见问题解答，帮助理解 Demo 的架构与各类职责。

---

## 一、LAppSubdelegate 介绍

### 是什么？

`LAppSubdelegate` 是**与单个画布（canvas）相关的操作封装类**。它把 WebGL 上下文、纹理管理、Live2D 模型管理、视图渲染、画布自适应、以及指针/触摸输入，集中在一个对象里。

### 核心职责

1. **启动时**：`initialize(canvas)` → 初始化 WebGL、设置画布、视图、模型
2. **每帧**：`update()` → 清屏、调用 `_view.render()` 画模型
3. **输入**：`onPointBegan/Moved/Ended` → 坐标转换、转发给 View

### 内部成员


| 成员                | 作用           |
| ----------------- | ------------ |
| `_glManager`      | 管理 WebGL 上下文 |
| `_textureManager` | 管理贴图         |
| `_live2dManager`  | 管理 Live2D 模型 |
| `_view`           | 负责实际绘制和交互    |


---

## 二、WebGL 上下文与贴图

### WebGL 上下文（WebGL Context）

- **含义**：浏览器给 canvas 提供的一套「画图能力」，用来用 GPU 画 2D/3D 图形。
- **类比**：Canvas = 画布，WebGL 上下文 = 画笔、颜料、调色板等工具。
- **获取方式**：`canvas.getContext('webgl')` 或 `'webgl2'`。

### 贴图（Texture）

- **含义**：贴在模型表面的图片，用于给模型上色（皮肤、衣服等）。
- **类比**：模型 = 石膏人偶，贴图 = 贴在表面的彩纸。
- **在 Demo 中**：`LAppGlManager` 负责 WebGL 上下文；`LAppTextureManager` 负责加载和管理贴图。

---

## 三、LAppDelegate 与 LAppSubdelegate 的关系

### 两者都定义并执行方法

两者都**既定义方法，又执行方法**，区别在于**职责和层级**：


|        | LAppDelegate | LAppSubdelegate |
| ------ | ------------ | --------------- |
| **职责** | 调度、协调整个应用    | 实现单个画布的具体逻辑     |
| **角色** | 调度者，决定「何时调用」 | 执行者，实现「具体怎么处理」  |


### 调用关系

- 用户点击 → `LAppDelegate.onPointerBegan(e)` → 遍历 subdelegates → `subdelegate.onPointBegan()`
- 每帧 → `LAppDelegate.run()` → `subdelegate.update()` → `_view.render()`

---

## 四、View 是什么？为什么要每帧画模型？

### View（LAppView）

- **角色**：绘制类，负责「怎么画、画什么」。
- **职责**：设置坐标系、绘制背景/齿轮/Live2D 模型、处理触摸。

### 为什么要每帧画模型？

1. **屏幕不会自动保存画面**：每帧都要重新写入像素。
2. **WebGL 是即时模式**：画完就结束，不会自动保留。
3. **Live2D 是动态的**：眨眼、呼吸、拖拽等，每帧的网格顶点都在变。

### 贴图与「画」的关系

- **贴好了**：贴图已加载到 GPU，UV 映射已定义。
- **画模型**：每帧用这些数据，让 GPU 真正把像素画到屏幕上。

---

## 五、各类的交互顺序

### 整体结构

---

## 六、谁来控制模型表情和动作？

### 1. 用户点击（手动触发）

**LAppLive2DManager.onTap(x, y)** 负责根据点击区域决定播放表情或动作：

- **点击头部** → `model.setRandomExpression()` → 随机切换表情
- **点击身体** → `model.startRandomMotion(TapBody, ...)` → 播放 TapBody 随机动作

命中区域由 `LAppModel.hitTest(hitArenaName, x, y)` 判断，区域名在 `LAppDefine` 中定义（`HitAreaNameHead`、`HitAreaNameBody`）。

### 2. 每帧自动更新

**LAppModel.update()** 每帧执行，负责各类效果的更新：

| 效果 | 控制者 | 说明 |
|------|--------|------|
| 待机动作 | `startRandomMotion(Idle)` | 动作队列为空时自动播放 Idle 待机动作 |
| 眨眼 | `CubismEyeBlink` | 自动眨眼 |
| 表情 | `_expressionManager.updateMotion()` | 播放当前选中的表情 |
| 呼吸 | `CubismBreath` | 呼吸效果 |
| 物理 | `_physics.evaluate()` | 物理模拟 |
| 口型 | `LAppWavFileHandler` | 随 WAV 音频做口型同步 |
| 拖拽 | `_dragManager` | 根据拖拽更新头部/身体角度 |

### 3. 调用链概览

```
用户点击
  → LAppDelegate.onPointerEnded
  → LAppSubdelegate.onPointEnded
  → LAppView.onTouchesEnded
  → LAppLive2DManager.onTap(x, y)
      ├─ hitTest(Head) → model.setRandomExpression()
      └─ hitTest(Body) → model.startRandomMotion(TapBody)

每帧
  → LAppDelegate.run()
  → LAppSubdelegate.update()
  → LAppLive2DManager.onUpdate()
  → model.update()  ← 在这里更新动作、表情、眨眼、呼吸等
```

### 4. 总结

- **LAppLive2DManager**：根据点击区域（头部/身体）决定要播放的表情或动作，并调用 `LAppModel` 的接口。
- **LAppModel**：实际执行表情和动作的播放，并在每帧 `update()` 中更新待机动作、眨眼、呼吸、物理、口型等效果。

表情和动作的数据来自模型配置（`.model3.json` 中引用的 `.exp3.json` 和 `.motion3.json`），由 `LAppDefine` 中的 `MotionGroupIdle`、`MotionGroupTapBody`、`HitAreaNameHead`、`HitAreaNameBody` 等常量与模型资源对应。

---

## 术语与概念说明

在阅读各文件说明前，建议先了解以下术语的含义。

### GL / WebGL

**GL** 即 **OpenGL**（Open Graphics Library），是一套跨平台的图形渲染 API。  
**WebGL** 是 OpenGL 在浏览器中的实现，让网页能用 GPU 进行 2D/3D 绘图，而不依赖插件。

在本 Demo 中，所有 Live2D 模型、背景图、按钮等都是在 Canvas 上通过 WebGL2 绘制的。`getGl()` 返回的便是这个 WebGL 上下文对象，用于创建着色器、纹理、缓冲区并执行绘制命令。

---

### GL 管理器（LAppGlManager）

**GL 管理器** 是对 WebGL 上下文的封装类。它负责：

- 从 Canvas 获取 WebGL2 上下文（`canvas.getContext('webgl2')`）
- 在应用内统一提供 `getGl()`，供纹理管理、精灵、模型渲染等模块使用
- 集中管理 GL 的创建与释放，避免多处重复初始化

可以理解为：GL 管理器是「WebGL 的入口」，其他需要画图的模块都通过它拿到 GL 对象。

---

### 纹理（Texture）

**纹理** 是贴在 3D/2D 表面上的图像数据。在 Live2D 中：

- **模型纹理**：角色皮肤、衣服等 PNG 图片，加载后上传到 GPU，供模型网格采样显示
- **UI 纹理**：背景图、齿轮图标等，同样以纹理形式加载，由精灵绘制

`LAppTextureManager` 负责：从 PNG 文件加载图片 → 用 WebGL 创建纹理对象 → 缓存并管理这些纹理，供模型和精灵使用。

---

### 精灵（Sprite）

**精灵** 是 2D 游戏/应用里常用的概念：一张图片 + 一个矩形区域，可以放在屏幕指定位置绘制。

在本 Demo 中，**LAppSprite** 用于绘制：

- 背景图（`_back`）
- 齿轮图标（`_gear`）

每个精灵有：位置 (x, y)、宽高、纹理 ID。`render()` 会把这些数据传给着色器，在 Canvas 上画出对应的矩形图像。精灵不负责加载图片，只负责「用已有纹理画一个矩形」。

---

### 命中检测 / 检测点（Hit Test）

**命中检测**（Hit Test）指：判断用户点击/触摸的坐标是否落在某个可交互区域内。

在本 Demo 中有两类：

1. **精灵命中**（`LAppSprite.isHit`）：判断点击是否在齿轮图标或背景的矩形范围内，用于「点齿轮切换模型」等逻辑。
2. **模型命中区域**（`LAppModel.hitTest`）：Live2D 模型在编辑器中可定义多个命中区域（如 Head、Body）。`hitTest` 根据模型当前姿态和网格，判断点击是否落在指定区域内，用于「点头部换表情」「点身体播动作」。

「检测点」即用户触摸/点击的屏幕坐标 (x, y)，需要转换到视图/模型坐标系后再做判断。

#### 命中区域不生效（hitTest 始终为 false）

若 `model.hitTest(LAppDefine.HitAreaNameHead, x, y)` 始终不进入，通常是因为 **model3.json 中未配置 HitAreas**。

**解决方法**：在模型的 `.model3.json` 中添加 `HitAreas` 配置，例如：

```json
"HitAreas": [
  { "Id": "HitAreaHead", "Name": "Head" },
  { "Id": "HitAreaBody", "Name": "Body" }
]
```

- `Name`：与 `LAppDefine.HitAreaNameHead`、`HitAreaNameBody` 对应（默认 `"Head"`、`"Body"`）。
- `Id`：必须对应模型 moc3 中某个 **Drawable 的 ID**，否则 `getDrawableIndex` 返回 -1，命中检测失败。

**如何获取 Drawable ID**：开启 `LAppDefine.DebugLogEnable` 后运行 Demo，模型加载完成时会在控制台打印所有 Drawable ID。从中选取覆盖头部、身体的 Drawable ID，填入 `HitAreas` 的 `Id` 字段。

---

### WAV 口型同步（Lip Sync）

**口型同步** 指让角色的嘴型随语音变化。  
**WAV 口型同步** 即：根据 WAV 音频的波形强度，驱动 Live2D 的口型参数，使嘴型与声音节奏一致。

流程大致为：

1. 动作文件（.motion3.json）可配置关联的 WAV 语音文件
2. 播放动作时，`LAppWavFileHandler` 加载并解析 WAV，得到 PCM 采样数据
3. 每帧计算当前时间段的 **RMS**（均方根，表示音量大小）
4. 将 RMS 映射到口型参数（如 `ParamMouthOpenY`），模型嘴部会随音量开合

这样在播放带语音的动作时，角色会「跟着说话」动嘴，而不是嘴型与声音脱节。

---


**PCM** 全称 **Pulse Code Modulation**（脉冲编码调制），是数字音频最基础的表示方式。

### 简单理解

把连续的声音波形按固定时间间隔采样，每个采样点用一个数字表示当时的音量，这一串数字就是 PCM 数据。

- **采样率**：每秒采样多少次（如 44100 Hz = 每秒 44100 个点）
- **位深**：每个采样用多少位表示（如 16 bit）
- **声道数**：单声道 1 个，立体声 2 个

### 和 WAV 的关系

WAV 文件里通常直接存放 PCM 数据（或少量压缩格式）。  
WAV 的头部描述采样率、位深、声道数，后面就是原始 PCM 采样值。

### 在本 Demo 中的用途

在 `LAppWavFileHandler` 中：

1. 解析 WAV 文件，得到 PCM 采样数组（`_pcmData`）
2. 按当前播放时间，取出对应时间段的采样
3. 计算这些采样的 **RMS**（均方根，表示音量大小）
4. 用 RMS 驱动 Live2D 的口型参数，实现嘴型随音量变化

所以这里的 PCM 就是「从 WAV 里读出来的原始音频采样数据」，用来做口型同步。