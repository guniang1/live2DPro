# Demo src 目录文件说明

本文档介绍本仓库 **`Demo/src/`**（Vite 前端应用源码）下主要文件的作用与调用关系。官方 Cubism **Samples** 路径仅作对照；**以本仓库实际路径为准**。

---

## 1. main.js — 应用入口与业务编排

| 方法/逻辑 | 作用 | 调用者 |
|-----------|------|--------|
| `window.addEventListener('load', ...)` | 初始化 Live2D、拉背景列表、按登录用户加载远程模型 manifest（presigned）、建立 WebSocket、绑定 UI | 浏览器 |
| `refreshChatHistoryFromServer` | `GET /api/chat-sessions` 拉取当前用户 + 当前包历史，写入聊天面板 | load / 切模型后 |
| `ensureLive2dRemoteManifests` / `_resolveOneAssetFetchUrl` | 索引行 → `download-url` → 可选下载代理 URL，填入 `lappdefine` 远程 manifest | 已登录且库中有包 |
| `window.addEventListener('beforeunload', ...)` | 释放应用资源 | 浏览器 |

**说明**：在官方 Demo 基础上扩展了 **REST、登录态、远程 Resources、会话历史**；Live2D 帧循环仍由 **`LAppDelegate`** 负责。

---

## 2. lappdefine.js — 常量配置

| 内容 | 作用 |
|------|------|
| 导出常量 | 画布尺寸、视口、资源路径、模型目录、动作组名、命中区域名、优先级、调试开关等 |

**说明**：纯配置模块，无自定义方法，被 `lappdelegate.js`、`lappsubdelegate.js`、`lappmodel.js`、`lappview.js`、`lapplive2dmanager.js` 等引用。

---

## 3. lappdelegate.js — 应用主类（单例）

| 方法 | 作用 | 调用者 |
|------|------|--------|
| `getInstance()` | 获取单例 | main.js |
| `releaseInstance()` | 释放单例 | main.js |
| `initialize()` | 初始化 Cubism SDK、子代理、事件监听 | main.js |
| `run()` | 启动动画帧循环 | main.js |
| `onPointerBegan(e)` | 处理 pointerdown | document 事件 |
| `onPointerMoved(e)` | 处理 pointermove | document 事件 |
| `onPointerEnded(e)` | 处理 pointerup | document 事件 |
| `onPointerCancel(e)` | 处理 pointercancel | document 事件 |
| `onResize()` | 处理窗口 resize | 内部 / ResizeObserver |
| `release()` | 释放资源 | releaseInstance |
| `initializeCubism()` | 初始化 Cubism 框架 | initialize |
| `initializeSubdelegates()` | 创建画布和子代理 | initialize |
| `initializeEventListener()` | 注册 pointer 事件 | initialize |

---

## 4. lappsubdelegate.js — 子代理（单画布管理）

| 方法 | 作用 | 调用者 |
|------|------|--------|
| `initialize(canvas)` | 初始化 GL、纹理、视图、Live2D 管理器 | LAppDelegate |
| `release()` | 释放子代理资源 | LAppDelegate |
| `update()` | 每帧清屏、渲染视图 | LAppDelegate.run() 循环 |
| `onResize()` | 画布尺寸变化时重新初始化 | LAppDelegate / ResizeObserver |
| `onPointBegan(pageX, pageY)` | 触摸/指针按下 | LAppDelegate.onPointerBegan |
| `onPointMoved(pageX, pageY)` | 触摸/指针移动 | LAppDelegate.onPointerMoved |
| `onPointEnded(pageX, pageY)` | 触摸/指针抬起 | LAppDelegate.onPointerEnded |
| `onTouchCancel(pageX, pageY)` | 触摸/指针取消 | LAppDelegate.onPointerCancel |
| `createShader()` | 创建 2D 精灵着色器 | LAppView.initializeSprite |
| `getTextureManager()` | 返回纹理管理器 | LAppView、LAppModel |
| `getFrameBuffer()` | 返回帧缓冲 | LAppModel.doDraw |
| `getCanvas()` | 返回 canvas | LAppView、LAppModel、LAppLive2DManager |
| `getGlManager()` | 返回 GL 管理器 | LAppView、LAppModel |
| `getLive2DManager()` | 返回 Live2D 管理器 | LAppView |
| `resizeCanvas()` | 按设备像素比调整画布尺寸 | initialize、onResize |
| `resizeObserverCallback()` | ResizeObserver 回调 | ResizeObserver |
| `isContextLost()` | 检测 WebGL 上下文是否丢失 | LAppDelegate |

---

## 5. lappmodel.js — Live2D 模型实现

| 方法 | 作用 | 调用者 |
|------|------|--------|
| `loadAssets(dir, fileName)` | 加载 model3.json 并启动 setupModel | LAppLive2DManager.changeScene |
| `setupModel(setting)` | 按配置加载 moc3、表情、物理、姿势等 | loadAssets |
| `setupTextures()` | 加载 PNG 纹理并绑定到渲染器 | setupModel、preLoadMotionGroup |
| `update()` | 每帧更新拖拽、动作、眨眼、表情、呼吸、物理、口型 | LAppLive2DManager.onUpdate |
| `startMotion(group, no, priority, ...)` | 按组和编号播放动作 | startRandomMotion |
| `startRandomMotion(group, priority, ...)` | 在指定组内随机播放动作 | LAppLive2DManager.onTap、update |
| `setExpression(expressionId)` | 播放指定表情 | setRandomExpression |
| `setRandomExpression()` | 随机播放表情 | LAppLive2DManager.onTap |
| `hitTest(hitArenaName, x, y)` | 检测命中区域 | LAppLive2DManager.onTap |
| `preLoadMotionGroup(group)` | 预加载动作组 | setupModel |
| `doDraw()` | 执行模型绘制 | draw |
| `draw(matrix)` | 设置 MVP 并绘制模型 | LAppLive2DManager.onUpdate |
| `setSubdelegate(subdelegate)` | 设置子代理引用 | LAppLive2DManager.changeScene |
| `reloadRenderer()` | 重建渲染器并重新设置纹理 | 上下文恢复等 |
| `releaseMotions()` / `releaseExpressions()` | 释放动作/表情 | 内部 |
| `hasMocConsistencyFromFile()` | 异步检测 MOC3 一致性 | 可选调用 |

---

## 6. lappview.js — 视图与绘制

| 方法 | 作用 | 调用者 |
|------|------|--------|
| `initialize(subdelegate)` | 初始化视口矩阵、设备坐标转换 | LAppSubdelegate |
| `release()` | 释放视图资源 | LAppSubdelegate |
| `render()` | 绘制背景、齿轮、Live2D 模型 | LAppSubdelegate.update |
| `initializeSprite()` | 加载背景和齿轮纹理，创建着色器 | LAppSubdelegate |
| `onTouchesBegan(pointX, pointY)` | 触摸开始 | LAppSubdelegate.onPointBegan |
| `onTouchesMoved(pointX, pointY)` | 触摸移动，更新拖拽 | LAppSubdelegate.onPointMoved |
| `onTouchesEnded(pointX, pointY)` | 触摸结束，处理点击和齿轮 | LAppSubdelegate.onPointEnded |
| `transformViewX/Y(deviceX/Y)` | 设备坐标转视图坐标 | onTouchesMoved、onTouchesEnded |
| `transformScreenX/Y(deviceX/Y)` | 设备坐标转屏幕坐标 | 内部 |

---

## 7. lapplive2dmanager.js — Live2D 模型管理

| 方法 | 作用 | 调用者 |
|------|------|--------|
| `initialize(subdelegate)` | 初始化并加载首个模型 | LAppSubdelegate |
| `onDrag(x, y)` | 将拖拽坐标传给模型 | LAppView.onTouchesMoved/Ended |
| `onTap(x, y)` | 处理点击：头部切表情、身体播动作 | LAppView.onTouchesEnded |
| `onUpdate()` | 更新模型并绘制 | LAppView.render |
| `nextScene()` | 切换到下一个模型 | LAppView.onTouchesEnded（点齿轮） |
| `changeScene(index)` | 按索引切换模型 | nextScene、addModel |
| `addModel(sceneIndex)` | 添加/切换模型 | 外部 |
| `setViewMatrix(m)` | 设置视图矩阵 | LAppView.render |
| `releaseAllModel()` | 清空模型列表 | changeScene |

---

## 8. lappglmanager.js — WebGL 管理

| 方法 | 作用 | 调用者 |
|------|------|--------|
| `initialize(canvas)` | 获取 WebGL2 上下文 | LAppSubdelegate |
| `release()` | 释放资源（当前为空实现） | LAppSubdelegate |
| `getGl()` | 返回 WebGL 上下文 | LAppSubdelegate、LAppTextureManager、LAppSprite、LAppModel |

---

## 9. lapptexturemanager.js — 纹理管理

| 方法 | 作用 | 调用者 |
|------|------|--------|
| `createTextureFromPngFile(fileName, usePremultiply, callback)` | 异步加载 PNG 并创建纹理 | LAppView、LAppModel |
| `setGlManager(glManager)` | 设置 GL 管理器 | LAppSubdelegate |
| `release()` | 释放所有纹理 | LAppSubdelegate |
| `releaseTextures()` | 清空纹理 | 内部 |
| `releaseTextureByTexture/FilePath()` | 按纹理或路径释放 | 内部 |

---

## 10. lappsprite.js — 2D 精灵

| 方法 | 作用 | 调用者 |
|------|------|--------|
| `render(programId)` | 使用指定着色器绘制精灵 | LAppView.render |
| `isHit(pointX, pointY)` | 检测点是否在精灵矩形内 | LAppView.onTouchesEnded |
| `setSubdelegate(subdelegate)` | 设置子代理 | LAppView.initializeSprite |
| `release()` | 释放精灵资源 | LAppView.release |

---

## 11. touchmanager.js — 触摸状态管理

| 方法 | 作用 | 调用者 |
|------|------|--------|
| `touchesBegan(deviceX, deviceY)` | 记录触摸开始 | LAppView.onTouchesBegan |
| `touchesMoved(deviceX, deviceY)` | 记录触摸移动 | LAppView.onTouchesMoved |
| `getX()` / `getY()` | 获取当前触摸坐标 | LAppView |
| `getDeltaX()` / `getDeltaY()` | 获取位移 | 预留 |
| `getFlickDistance()` | 计算滑动距离 | 预留 |
| `calculateDistance()` | 计算两点距离 | getFlickDistance |
| `calculateMovingAmount()` | 计算有效移动量 | 预留 |

---

## 12. lapppal.js — 平台抽象层

| 方法 | 作用 | 调用者 |
|------|------|--------|
| `loadFileAsBytes(filePath, callback)` | 异步读取文件为字节 | 可选 |
| `getDeltaTime()` | 返回帧间时间差 | LAppModel.update |
| `updateTime()` | 更新当前帧时间 | LAppDelegate.run |
| `printMessage(message)` | 输出日志 | 多处 |

---

## 13. lappwavfilehandler.js — WAV 口型同步

| 方法 | 作用 | 调用者 |
|------|------|--------|
| `update(deltaTimeSeconds)` | 更新口型同步 RMS | LAppModel.update |
| `start(filePath)` | 开始播放 WAV | LAppModel.startMotion |
| `getRms()` | 返回 RMS 值用于口型 | LAppModel.update |
| `loadWavFile(filePath)` | 异步加载 WAV | start |
| `getPcmDataChannel()` | 获取指定声道 PCM | 可选 |
| `getWavSamplingRate()` | 获取采样率 | 可选 |

---

## 14. `api/` — HTTP 与 WebSocket

| 文件 | 作用 |
|------|------|
| `apiBase.js` | `getHttpOrigin` / `getApiBase`：`VITE_API_BASE` 或 `localStorage.live2d_info.httpBase` |
| `wsConfig.js` | `session`、`package`、`user_id` 与 WS URL 拼装 |
| `ws.js` | `/ws/chat`、`/ws/tts` 连接、流式正文、`chunk_audio`、TTS 播放队列、口型 **`feedChatLive2dTtsAudioLipLevel`**、`sendChatMessage` |
| `chatSessions.js` | `GET /api/chat-sessions` 封装 |
| `assetUpload.js` | 模型包列表、资源索引、上传 zip 等 |
| `storageFetchUrl.js` | presigned URL 经网关时的可选改写（**`VITE_DOWNLOAD_SHARED_OBJECT_BASE`**） |

---

## 15. `utils/TextChat.js`

独立示例模块（**`connectChat`**）：演示仅用 WebSocket 发一句并处理回复；**当前主流程未在 `main.js` 中引用**，与生产路径 **`api/ws.js`** 并存时注意勿混用两套协议处理逻辑。

---

## 16. `pages/` — 多页面入口

| 文件 | 作用 |
|------|------|
| `Login.html` | 登录；成功后写入 **`live2d_info`** 并跳回 `redirect` |
| `characterDef.html` | 按包编辑人设（`/personas/by-package` 等） |
| `assetUpload.html` / `ttsUpload.html` | 模型 zip / TTS 参考上传 |

---

## 调用关系概览

```
main.js
  └── api/*（ws.js、chatSessions.js、assetUpload.js …）
  └── LAppDelegate (lappdelegate.js)
        ├── LAppSubdelegate (lappsubdelegate.js)
        │     ├── LAppGlManager (lappglmanager.js)
        │     ├── LAppTextureManager (lapptexturemanager.js)
        │     ├── LAppView (lappview.js)
        │     │     ├── LAppSprite (lappsprite.js)
        │     │     └── TouchManager (touchmanager.js)
        │     └── LAppLive2DManager (lapplive2dmanager.js)
        │           └── LAppModel (lappmodel.js)
        │                 └── LAppWavFileHandler (lappwavfilehandler.js)
        └── LAppPal (lapppal.js)
```

整体流程：`main.js` 启动 `LAppDelegate`，由 `LAppDelegate` 创建并管理多个 `LAppSubdelegate`，每个子代理负责一个画布及其 GL、纹理、视图和 Live2D 模型。
