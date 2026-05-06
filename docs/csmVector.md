## csmVector：Cubism SDK 中的可变数组容器

本文档介绍 Live2D Cubism SDK for Web 中的 `csmVector`：它是什么、有什么作用、为什么使用它，以及在 Demo 中的典型用法。

---

## 一、csmVector 是什么

- **定义位置**：`Framework/src/type/csmvector.ts`
- **类型说明**：源码注释为「ベクター型（可変配列型）」，即**“向量型（可变长数组容器）”**。
- **本质**：对原生数组做了一层封装，提供类似 C++ `csm::Vector` 的接口，用来统一管理 Cubism SDK 内部的“列表类数据”。

简单理解：**`csmVector<T>` = Cubism 自己实现的泛型动态数组容器**。

---

## 二、主要能力与 API

`csmVector<T>` 内部维护：

- `_ptr: T[]`：真正存数据的数组
- `_size: number`：当前元素个数
- `_capacity: number`：预分配的容量

常用方法：

- **构造**：`new csmVector<T>(initialCapacity?)`
- **访问元素**：
  - `at(index: number): T`：按下标读取
  - `set(index: number, value: T): void`：按下标写入
- **增删**：
  - `pushBack(value: T): void`：在末尾追加元素（不够时自动扩容）
  - `remove(index: number): boolean`：按下标删除
  - `clear(): void`：清空所有元素
- **大小与容量**：
  - `getSize(): number`：当前元素个数
  - `resize(newSize: number, value?: T): void`：调整长度
  - `assign(newSize: number, value: T): void`：重置为若干个相同值
  - `prepareCapacity(newSize: number): void`：预分配容量
- **遍历与子区间**：
  - `get(offset?: number): T[]`：从 offset 起返回普通数组副本
  - `getOffset(offset: number): csmVector<T>`：返回从 offset 起的新 `csmVector`
  - `begin()/end()` 与 `iterator<T>`：提供类 C++ 风格的迭代器。

和普通 `Array` 的简单对照：

| 功能     | csmVector           | Array         |
|--------|----------------------|--------------|
| 创建     | `new csmVector()`   | `[]`         |
| 尾部添加  | `pushBack(x)`       | `push(x)`    |
| 按下标读  | `at(i)`             | `arr[i]`     |
| 按下标写  | `set(i, x)`         | `arr[i] = x` |
| 长度     | `getSize()`         | `length`     |
| 清空     | `clear()`           | `arr.length = 0` |

---

## 三、为什么使用 csmVector（好处）

### 1. 与 C++/Native SDK 对齐

Live2D 的 C++/Native SDK 使用 `csm::Vector`。Web 版通过 `csmVector` 复刻了同样的概念和 API：

- 文档/示例更容易在 C++ ⇄ Web 之间对照；
- 从原生实现移植逻辑时，容器接口基本一致；
- 降低“这边用数组，那边用 Vector”的心智切换成本。

### 2. 统一容器类型，便于维护

在 Cubism Framework 里，约定“动态列表 = `csmVector`”。例如：

- 模型参数 ID 列表、眼睛眨眼参数 ID 列表；
- 命中区域、用户数据列表；
- 物理输入/输出、剪裁上下文列表等。

统一用 `csmVector` 带来的好处：

- 接口/类型统一（返回值、参数类型一眼能看懂）；
- 减少“有的地方用 `Array`，有的地方用其它容器”的混乱；
- 更方便后续做统一优化或修改容器实现。

### 3. 封装容量与扩容策略

`csmVector` 内部维护 `_capacity`，在 `pushBack` 时如果空间不够会自动扩容：

- 初始容量默认为 `DefaultSize = 10`；
- 不够时按 2 倍策略增长。

这样 SDK 内部可以：

- 明确控制扩容策略；
- 在性能敏感代码中减少不必要的数组重分配。

对使用者来说，与 `Array.push` 基本一样简单，但行为更可预测。

### 4. 迭代器与区间操作

通过 `begin()/end()` 和 `iterator<T>`，可以实现类似 C++ 的区间操作：

- `insert(position, begin, end)`：把一段区间插入到指定位置；
- `erase(ite)`：删除某个迭代器指向的元素。

这些能力主要在 **Framework 内部** 使用，对应用层业务代码来说不一定需要，但对实现复杂的数据结构和算法会更方便。

---

## 四、在 Demo 中的典型用法

在 `Samples/TypeScript/Demo/src` 里可以看到多处使用 `csmVector`，例如：

- `lappdelegate.js`：
  - `_subdelegates = new csmVector();`：保存所有子代理（每个子代理对应一块 canvas）。
  - `_canvases = new csmVector();`：保存所有画布元素。
- `lappmodel.js`：
  - `_eyeBlinkIds = new csmVector();`：管理眨眼相关参数 ID。
  - `_lipSyncIds = new csmVector();`：管理口型同步参数 ID。
  - `_hitArea = new csmVector();`、`_userArea = new csmVector();`：管理命中区域与用户自定义区域。

在这些地方，`csmVector` 的用法可以概括为：

```ts
import { csmVector } from '@framework/type/csmvector';

// 创建容器
const list = new csmVector();

// 追加元素
list.pushBack(item);

// 遍历
for (let i = 0; i < list.getSize(); i++) {
  const elem = list.at(i);
  // ... 使用 elem ...
}

// 删除/清空
list.remove(0);
list.clear();
```

也就是说：**在 Demo 和 Framework 中，只要看到“需要一个可以动态增删的列表”，基本上就会用 `csmVector` 来管理**。

---

## 五、总结

- `csmVector<T>` 是 Cubism SDK for Web 中的**泛型动态数组容器**，封装了数组、容量和扩容逻辑。
- 它的主要价值在于：
  - 与 C++/Native 版 SDK 的 `csm::Vector` 一致；
  - 统一 Framework 内部的容器类型；
  - 提供可控的扩容策略与迭代器接口。
- 在应用/Demo 代码中，使用方式类似普通数组：`new csmVector()` 创建，`pushBack` 添加，`at(i)` 读取，`getSize()` 获取长度，`clear()` 清空。

在阅读或编写 Cubism 相关代码时，只要把 `csmVector` 理解为“Cubism 框架规定使用的动态数组”即可。

