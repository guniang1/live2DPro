# Demo 架构说明：应用、主代理与子代理

本文档说明 Live2D Cubism SDK for Web 的 TypeScript Demo 中**应用**、**主代理（LAppDelegate）**、**子代理（LAppSubdelegate）** 的含义与关系，以及事件监听与 `.bind(this)`、释放流程等。

---

## 一、应用指什么

在本 Demo 中，**「应用」** 指整个 Live2D Cubism 示例程序，即你在浏览器里打开的这个页面及其背后的一整套逻辑。

- **应用主类**：`LAppDelegate`，负责初始化 Cubism SDK、创建/管理子代理、处理指针事件和窗口 resize 等。
- **应用实例**：通过 `LAppDelegate.getInstance()` 得到的单例，代表当前正在运行的这个应用。
- **应用** 通常指：从页面加载 → 初始化 → 渲染模型 → 响应用户操作 → 直到页面关闭的整条链路。

可简单记为：**应用 = 这个 Live2D 示例网页本身**（包含入口、Delegate、子代理、画布、模型等）。

---

## 二、主代理与子代理

### 2.1 主代理（LAppDelegate）

- **职责**：管“整个应用”。
- **主要工作**：
  - 初始化/释放 Cubism SDK；
  - 根据配置创建多个画布（canvas），并为每个画布创建一个子代理；
  - 注册/移除全局事件（pointerdown / pointermove / pointerup / pointercancel、resize）；
  - 每帧循环中依次调用每个子代理的 `update()`；
  - 把指针事件**分发给所有子代理**（不处理具体命中、拖拽等）。

主代理不直接管某一块画布上的模型、视图或触摸逻辑，只做“广播 + 调度”。

### 2.2 子代理（LAppSubdelegate）

**一个子代理 = 一块画布（canvas）的管家。**

**一个子代理对应一块画布，这块画布上可以放多个模型**。

子代理里有 `_live2dManager`（`LAppLive2DManager`），内部用 `_models`（列表）保存多个 `LAppModel`。当前 Demo 只用了 `_models.at(0)`、一次只显示一个模型并做切换场景，但**结构上已经支持多个模型**，只要在 `onUpdate` 里遍历 `_models` 对每个做 `update()` / `draw()` 即可。

每个子代理负责**一块独立的 Live2D 显示区域**，包括：


| 职责        | 说明                              |
| --------- | ------------------------------- |
| 一块画布      | 持有一个 `<canvas>` 及该画布的 WebGL 上下文 |
| 纹理        | 该画布用到的贴图管理（`_textureManager`）   |
| Live2D 模型 | 在该画布上加载、更新、渲染的模型                |
| 视图/渲染     | 镜头、背景、精灵、绘制（`_view`）            |
| 尺寸        | 画布尺寸、ResizeObserver、onResize    |
| 触摸/指针     | 命中检测、拖拽、是否“捕获”当前指针等             |


配置中的 `CanvasNum` 决定画布数量；当 `CanvasNum > 1` 时，页面上会有多块 canvas，每块对应一个子代理，各自管理自己的 WebGL、模型与交互。

**总结**：子代理用来“按画布拆分职责”，每个子代理负责一块画布上的 Live2D 显示与交互。

---

## 三、释放流程：release() 与 releaseSubdelegates()

- `**release()`**：应用级的“总释放”入口。依次执行：
  1. 释放事件监听器（`releaseEventListener()`）；
  2. 释放子代理（`releaseSubdelegates()`）；
  3. 释放 Cubism SDK（`CubismFramework.dispose()`）；
  4. 清空选项引用（`_cubismOption = null`）。
- `**releaseSubdelegates()**`：具体执行“释放子代理”的逻辑：遍历 `_subdelegates`，对每个子代理调用 `release()`，然后清空列表并置空。

因此，“释放子代理”只在一处实现（`releaseSubdelegates()`），`release()` 只是调用它，没有两套释放逻辑。

---

## 四、事件监听器与 .bind(this)

### 4.1 为什么要存一份监听器引用（如 pointBeganEventListener）

- `addEventListener` 和 `removeEventListener` 必须传入**同一个函数引用**才能正确移除。
- 若写 `addEventListener('pointerdown', this.onPointerBegan.bind(this), ...)`，每次 `bind(this)` 都会生成**新函数**，之后用另一个 `bind(this)` 无法移除之前添加的监听器。
- 因此先 `this.pointBeganEventListener = this.onPointerBegan.bind(this)`，添加和移除时都用 `this.pointBeganEventListener`，才能正确移除。

在 `releaseEventListener()` 里移除监听后，再执行 `this.pointBeganEventListener = null`，表示该引用不再使用，避免误用或重复移除。

### 4.2 .bind(this) 是什么、为什么要绑定 this

- `**this.onPointerBegan`**：不是 JavaScript 内置函数，而是本 Demo 在 `LAppDelegate` 类上定义的实例方法，用于在“指针按下”时把事件分发给所有子代理（例如访问 `this._subdelegates`）。
- **何时调用**：  
  - 初始化时：对象调用 `initializeEventListener()`，此时方法里的 `this` 自然是 LAppDelegate 实例。  
  - 用户点击/移动指针时：是 **document 的事件系统**在调用你传进去的回调，调用方是浏览器，不会把 LAppDelegate 实例当作 `this` 传进去，函数里的 `this` 会变成触发事件的元素或 `undefined`。
- `**.bind(this)` 的作用**：  
在“初始化”这一刻，把当前的 `this`（LAppDelegate 实例）“锁”进这个函数。之后无论谁调用（包括浏览器在事件里调用），函数内部的 `this` 都**始终**是当初绑定的那个 LAppDelegate 实例。

因此：**不绑定**时，事件触发后由浏览器调用回调，`this` 会错；**绑定 this** 后，回调里仍能正确访问 `this._subdelegates` 等，逻辑才能正常运行。

---

## 五、onPointerCancel 的作用

- 对应浏览器的 **pointercancel** 事件，表示本次触摸/指针被**系统中途取消**（如来电、弹窗、手势被识别为滚动、指针离开窗口等），而不是用户正常抬起手指。
- **LAppDelegate.onPointerCancel(e)**：在发生 pointercancel 时，遍历所有子代理，对每个调用 `onTouchCancel(e.pageX, e.pageY)`。
- **LAppSubdelegate.onTouchCancel**：将 `_captured = false`，并调用 `this._view.onTouchesEnded(...)`，让视图结束触摸逻辑（如停止拖拽、恢复姿势）。

这样在“触摸被取消”时，所有子代理都能正确释放捕获并结束触摸状态，避免模型一直处于“正在被拖动”等中间状态。

---

## 六、窗口 resize 与 onResize

- 子代理通过 **ResizeObserver** 监听**画布元素**尺寸变化；若配置为 `CanvasSize === 'auto'`，画布随布局变化时会设置 `_needResize`，在下一帧 `update()` 中调用该子代理的 `onResize()`。
- **LAppDelegate.onResize()** 需要由**窗口 resize** 主动调用才会执行。在 `main.js` 中通过 `window.addEventListener('resize', () => LAppDelegate.getInstance().onResize())` 实现。这样在用户改变浏览器窗口大小时，主代理会遍历并调用每个子代理的 `onResize()`，与 ResizeObserver 一起保证画布随窗口正确调整。

---

## 七、相关文件


| 文件                   | 说明                                  |
| -------------------- | ----------------------------------- |
| `lappdelegate.js`    | 应用主类，主代理                            |
| `lappsubdelegate.js` | 子代理，与画布相关的操作封装                      |
| `Demo/src/main.js`   | 入口：初始化、run、resize 与 beforeunload 监听（本仓库路径） |

**另见**：[模型平移与长按拖拽.md](模型平移与长按拖拽.md)（矩阵、名词、`setDragging` 与 `_modelMatrix` 平移的区别及长按手势流程）。


