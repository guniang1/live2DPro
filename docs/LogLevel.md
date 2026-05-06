# LogLevel 日志级别

本文档说明 Live2D Cubism SDK for Web 中 **LogLevel** 的定义、作用及调用/使用方法。

---

## 一、定义

**LogLevel** 是 Framework 中的日志级别枚举，定义在 `Framework/src/live2dcubismframework.ts`：

```typescript
export enum LogLevel {
  LogLevel_Verbose = 0,  // 详细日志
  LogLevel_Debug,        // 调试
  LogLevel_Info,         // 信息
  LogLevel_Warning,      // 警告
  LogLevel_Error,        // 错误
  LogLevel_Off           // 关闭所有日志
}
```

数值越小，输出的日志越多；数值越大，只输出更“严重”的级别。  
过滤规则：**仅当「本条日志的级别 ≥ 当前设定的 CubismLoggingLevel」时才会输出**（数值比较）。

---

## 二、作用

1. **控制 Framework 内部日志量**  
   初始化时通过 `Option.loggingLevel` 传入一个 LogLevel，Framework 内部（如 `CubismDebug.print`）在打日志前会与 `CubismFramework.getLoggingLevel()` 比较，低于设定级别的日志不会输出。

2. **统一业务日志级别**  
   业务代码使用 Framework 提供的 `CubismLogError`、`CubismLogInfo` 等时，同样受该级别控制，便于开发时开详细日志、发布时只保留错误或关闭。

3. **输出目标可配置**  
   通过 `Option.logFunction` 可指定日志实际输出到何处（如控制台、自定义上报等），与 LogLevel 配合使用。

---

## 三、调用方式 / 使用方法

### 3.1 配置全局日志级别（应用侧）

在应用定义文件中设置 **CubismLoggingLevel**，例如 `Samples/TypeScript/Demo/src/lappdefine.js`：

```javascript
import { LogLevel } from '@framework/live2dcubismframework';

// 根据需要选择其一
export const CubismLoggingLevel = LogLevel.LogLevel_Verbose;  // 输出全部
// export const CubismLoggingLevel = LogLevel.LogLevel_Info;  // 仅 Info / Warning / Error
// export const CubismLoggingLevel = LogLevel.LogLevel_Warning; // 仅 Warning / Error
// export const CubismLoggingLevel = LogLevel.LogLevel_Error;   // 仅 Error
// export const CubismLoggingLevel = LogLevel.LogLevel_Off;     // 不输出
```

在委托类初始化 Cubism 时，将该值传给 Framework（无需再写其它代码），例如 `lappdelegate.js`：

```javascript
initializeCubism() {
  LAppPal.updateTime();
  this._cubismOption.logFunction = LAppPal.printMessage;
  this._cubismOption.loggingLevel = LAppDefine.CubismLoggingLevel;
  CubismFramework.startUp(this._cubismOption);
  CubismFramework.initialize();
}
```

之后 Framework 内部所有日志都会按 `CubismLoggingLevel` 过滤。

### 3.2 在业务代码中打日志（受同一级别控制）

推荐使用 Framework 提供的日志函数，这样会与 `CubismLoggingLevel` 一致：

```javascript
import {
  CubismLogError,
  CubismLogInfo,
  CubismLogWarning
} from '@framework/utils/cubismdebug';

// 错误
CubismLogError('加载失败: {0}', [url]);

// 信息
CubismLogInfo('模型已加载: {0}', [modelName]);

// 警告（若 Framework 已导出）
CubismLogWarning('不建议的操作: {0}', [reason]);
```

格式字符串中的 `{0}`、`{1}` 等会被后面数组中的参数替换。

### 3.3 自行根据级别判断后再输出

若使用 `console.log` 等，又希望与当前配置的级别一致，可先读取再判断：

```javascript
import { LogLevel, CubismFramework } from '@framework/live2dcubismframework';

if (CubismFramework.getLoggingLevel() <= LogLevel.LogLevel_Info) {
  console.log('你的调试信息', data);
}
```

这样你的自定义日志也会随 `lappdefine.js` 中的 `CubismLoggingLevel` 一起生效。

---

## 四、级别与使用场景建议

| 级别 | 适用场景 |
|------|----------|
| `LogLevel_Verbose` | 开发调试，需要最详细输出 |
| `LogLevel_Info` | 日常开发，减少无关调试信息 |
| `LogLevel_Warning` | 预发布/测试，只关心警告与错误 |
| `LogLevel_Error` | 生产环境，仅保留错误 |
| `LogLevel_Off` | 完全关闭 Framework 日志 |

---

## 五、相关 API

- **CubismFramework.getLoggingLevel()**  
  返回当前生效的日志级别（来自 `startUp` 时传入的 `Option.loggingLevel`）。

- **CubismDebug.print(logLevel, format, args?)**  
  Framework 内部统一入口；低于 `getLoggingLevel()` 的日志不会输出。

- **Option.loggingLevel**  
  在 `CubismFramework.startUp(option)` 时设置，决定全局日志级别。

- **Option.logFunction**  
  在 `startUp` 时设置，决定日志字符串的实际输出目标（如 `console.log` 或自定义函数）。
