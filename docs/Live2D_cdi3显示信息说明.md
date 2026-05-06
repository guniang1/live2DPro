# Live2D `*.cdi3.json` 显示信息说明

本文以 `Demo/public/Resources/Xiaogou/Xiaogou.cdi3.json` 为例，说明 **Cubism Display Information（CDI）** 文件的结构与字段含义。

---

## 1. 文件是什么、在工程里做什么

- 文件名为 **`*.cdi3.json`**，在 `*.model3.json` 中通过 **`"DisplayInfo": "Xiaogou.cdi3.json"`** 引用。
- 内容由 **Cubism Editor** 导出，本质是 **JSON 数据**，不是可执行代码。
- **作用**：为模型中的 **参数（Parameter）**、**部件（Part）** 提供 **可读显示名** 与 **分组**；方便在编辑器中浏览。运行时 SDK 也可用于显示/调试。
- **变形与绑定关系**在 **`.moc3`** 中定义；**cdi3 只描述「叫什么名字、归哪个组」**，不改变模型的数学结构。

---

## 2. 顶层结构概览

整份文件通常包含以下顶层键：

| 键 | 含义 |
|----|------|
| `Version` | 格式版本，与 Model3 一致为 **3**。 |
| `Parameters` | 每个 **参数** 一条：内部 `Id`、所属分组、显示名。 |
| `ParameterGroups` | **参数分组**（编辑器中类似文件夹）。 |
| `Parts` | 每个 **部件** 一条：内部 `Id`、显示名。 |
| `CombinedParameters` | **组合参数**：把两个参数绑成一对（常见为 X/Y），编辑器中联动调节。 |

---

## 3. `Parameters`：参数列表

每一项对应模型里的一个 **Cubism 参数**（与 `.moc3`、物理、表情中使用的 **`Id` 一致**）。

```json
{
  "Id": "ParamAngleX",
  "GroupId": "ParamGroup7",
  "Name": "角度 X"
}
```

| 字段 | 说明 |
|------|------|
| `Id` | SDK、物理、表情、脚本里引用的 **唯一标识**（如 `ParamEyeLOpen`）。 |
| `GroupId` | 对应下文 **`ParameterGroups`** 中某项的 **`Id`**，表示该参数在 UI 上归哪一组。 |
| `Name` | 给人看的标签，可为中文；**不参与程序逻辑**，主要影响编辑器/部分工具的显示。 |

若出现 `Param24` 等 **Name 为占位符**（如 `xxx`）的情况，说明作者在编辑器中未改全显示名，**以 **`Id`** 为准**。

---

## 4. `ParameterGroups`：参数分组

把多个 `Parameters` 按主题折叠成「分组」，便于在编辑器中浏览。

```json
{
  "Id": "ParamGroup10",
  "GroupId": "",
  "Name": "眼睛"
}
```

| 字段 | 说明 |
|------|------|
| `Id` | 被 **`Parameters[].GroupId`** 引用。 |
| `GroupId` | 多为空字符串 `""`，表示不再嵌套；若需「组中组」可填父组 Id（依工程而定）。 |
| `Name` | 分组标题，如「整体透视」「眼睛」「嘴巴」「表情」等。 |

---

## 5. `Parts`：部件（网格/Drawable 层级）

**Part** 是模型中可开关、可参与绑定的 **部件**；此处同样只做 **显示名映射**。

```json
{
  "Id": "Part",
  "Name": "人物"
}
```

| 字段 | 说明 |
|------|------|
| `Id` | 与 moc 中的部件 ID 一致（如 `Part`、`Part2`）。 |
| `Name` | 编辑器中显示的名称；名称中含「的複製」多为复制部件时的默认命名。 |

---

## 6. `CombinedParameters`：组合参数（成对参数）

每一项为 **长度为 2 的数组**，里面是 **`Parameters` 中已存在的 `Id`**，表示这两个参数在编辑器中作为 **一对**（常见为平面上的 X/Y）：

```json
"CombinedParameters": [
  ["ParamAngleX", "ParamAngleY"],
  ["ParamBodyAngleX", "ParamBodyAngleY"]
]
```

含义示例：**脸部角度 X/Y**、**身体旋转 X/Y** 在 Cubism Editor 中会作为 **二维向量** 一起调节。

---

## 7. 与 `exp3.json`、Web Demo 的关系

- **表情**（`*.exp3.json`）里写的是 **`Param74`、`Param81`** 等 **`Id`**；这些 **Id** 必须在 **moc** 中存在。
- **cdi** 里为同一参数起的 **`Name`**（例如分组「表情」下的 `"1"`、`"2"`）**仅便于在编辑器中辨认**；**运行时改表情仍只认 `Id`**。
- Web Demo **不要求手写 cdi**；一般随 Cubism 工程导出，与 `physics3`、`model3` 保持同步即可。

---

## 8. 修改时注意

- **改 `Name`**：一般只影响显示，**不改变模型行为**。
- **改 `Id`、删条目**：若与 **moc** 或其它 JSON 不一致，可能导致工具列表、调试信息或部分功能对不上。

---

## 9. 参考文件

- 示例：`Demo/public/Resources/Xiaogou/Xiaogou.cdi3.json`
- 与模型入口的关联：见 `Demo/public/Resources/Xiaogou/Xiaogou.model3.json` 中的 `FileReferences.DisplayInfo`

更完整的资源目录约定见：[`Live2D模型资源标准文件格式.md`](./Live2D模型资源标准文件格式.md)。
