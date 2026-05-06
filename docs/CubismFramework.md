# CubismFramework 与 Option

本文档说明 Live2D Cubism SDK for Web 中 **CubismFramework**、**Option** 的定义、作用，以及 **startUp 与 initialize 的区别**与使用方式。

---

## 一、导入方式

```typescript
import { CubismFramework, Option } from '@framework/live2dcubismframework';
```

实现位于：`Framework/src/live2dcubismframework.ts`。

---

## 二、CubismFramework 定义与作用

**CubismFramework** 是 Live2D Cubism SDK for Web 的**入口类（静态类）**，负责：

- SDK 的启动与关闭
- 内部资源的初始化与释放
- 日志回调绑定、ID 管理器等全局资源管理

该类**不可实例化**（构造函数为 `private constructor()`），所有方法均为静态方法。

### 主要 API

| 方法 | 说明 |
|------|------|
| `startUp(option?: Option)` | 启用框架，设置日志等；执行一次即可，重复调用会跳过 |
| `initialize(memorySize?: number)` | 初始化内部资源（ID 管理器、Core 内存等），使模型可加载与渲染 |
| `dispose()` | 释放内部资源（需先执行过 `initialize`） |
| `cleanUp()` | 重置为未启动状态，便于再次 `startUp` |
| `isStarted()` | 是否已执行过 `startUp` |
| `isInitialized()` | 是否已执行过 `initialize` |
| `getIdManager()` | 获取内部 ID 管理器 |
| `getLoggingLevel()` | 获取当前日志级别 |
| `coreLogFunction(message)` | 调用已绑定的 Core 日志函数 |

---

## 三、Option 定义与作用

**Option** 是传给 `CubismFramework.startUp()` 的**配置类**，用于控制日志行为：

```typescript
export class Option {
  logFunction: Live2DCubismCore.csmLogFunction;  // 日志输出函数
  loggingLevel: LogLevel;                        // 日志输出级别
}
```

- **logFunction**：自定义日志回调（如输出到控制台或 UI）
- **loggingLevel**：日志级别（Verbose / Debug / Info / Warning / Error / Off），详见 [LogLevel.md](./LogLevel.md)

---

## 四、startUp 与 initialize 的区别

两者**职责不同**，且**必须先 startUp，再 initialize**。

### 4.1 概念区别

| | startUp（开始） | initialize（初始化） |
|--|----------------|----------------------|
| **做什么** | 框架层面的“启用” | 资源层面的“初始化” |
| **内容** | 保存 Option、绑定 Core 日志、打印版本信息 | 创建 CubismIdManager、初始化 Core 内存、JSON 静态初始化等 |
| **资源** | 几乎不分配与模型相关的资源 | 分配/初始化模型显示所需资源 |

### 4.2 调用时机

- **startUp**：程序启动时调用一次；重复调用会被跳过。
- **initialize**：在需要加载、显示模型之前调用一次；若未先调用 `startUp`，会打出警告并直接返回。

### 4.3 结束时的对应关系

- **dispose()**：对应 `initialize()`，释放内部资源。
- **cleanUp()**：对应 `startUp()`，将状态重置为未启动，便于再次使用。

---

## 五、推荐使用流程

```typescript
import { CubismFramework, Option, LogLevel } from '@framework/live2dcubismframework';

// 1. 可选：配置 Option
const option = new Option();
option.logFunction = (message: string) => console.log('[Cubism]', message);
option.loggingLevel = LogLevel.LogLevel_Info;

// 2. 启动框架（只需一次）
CubismFramework.startUp(option);

// 3. 初始化资源（加载模型前调用一次）
CubismFramework.initialize();

// 4. 使用 SDK：加载模型、更新、渲染……

// 5. 结束时释放并清理
CubismFramework.dispose();
CubismFramework.cleanUp();
```

**注意**：必须先调用 `startUp` 再调用 `initialize`，否则 `initialize` 会因“未启动”而直接返回。
