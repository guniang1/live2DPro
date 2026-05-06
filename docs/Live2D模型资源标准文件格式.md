# Live2D 模型资源标准文件格式

本文档说明本仓库 **Cubism Web Demo** 中，`Demo/public/Resources/` 下每个模型目录应遵循的目录结构、命名约定与核心 JSON 字段，便于与 `lappdefine.js` 中的 `ModelDir` 及运行时加载逻辑一致。

适用范围：**Cubism Model 3**（`.model3.json`、`.moc3`、`.exp3.json` 等）。

---

## 1. 目录与命名约定

### 1.1 模型目录名

- 每个模型占用 **`Resources/` 下的一个子目录**，目录名**必须与**该目录内主配置文件 **`{名称}.model3.json` 的文件名（不含扩展名）完全一致**。
- 在 `Demo/src/lappdefine.js` 的 `ModelDir` 数组中填写的字符串即为该子目录名（例如 `Xiaozi`、`Xiaogou`）。

### 1.2 资源文件前缀（推荐）

为减少混淆、便于维护，建议**同一模型**的下列文件使用**统一前缀**（与目录名或 Cubism 导出的模型名一致）：

| 类型 | 示例 |
|------|------|
| MOC | `Xiaogou.moc3` |
| 物理 | `Xiaogou.physics3.json` |
| 显示信息（CDI） | `Xiaogou.cdi3.json` |
| 贴图目录 | `Xiaogou.8192/` 或 `Xiaogou.4096/`（分辨率因工程而异） |

贴图目录内一般为 `texture_00.png`、`texture_01.png` 等，路径写在 `model3.json` 的 `Textures` 数组中。

---

## 2. 目录结构（标准布局）

```
Resources/
└── {ModelName}/                 # 与 {ModelName}.model3.json 同名
    ├── {ModelName}.model3.json  # 必选：模型入口配置
    ├── {ModelName}.moc3         # 必选：模型二进制
    ├── {ModelName}.physics3.json
    ├── {ModelName}.cdi3.json
    ├── {分辨率}/                # 必选：贴图文件夹，名称与 model3 中 Textures 一致
    │   └── texture_00.png
    ├── expressions/             # 推荐：表情 *.exp3.json
    │   └── *.exp3.json
    ├── motions/                 # 可选：动作 *.motion3.json（若使用 Idle/TapBody 等需在 model3 中声明）
    │   └── *.motion3.json
    ├── {ModelName}.vtube.json   # 可选：VTube Studio 导出，Web Demo 可不加载
    ├── items_pinned_to_model.json
    └── 图标.png                 # 可选：与 vtube 中 Icon 字段一致时便于 VTS 使用
```

说明：

- **`motions/`** 为空时，不要在 `model3.json` 里写 `Motions`，或只写空对象，避免引用缺失文件。
- **`expressions/`** 中的每个 `*.exp3.json` 若要在运行时作为**可切换表情**列出，须在 **`model3.json` 的 `FileReferences.Expressions`** 中逐项登记（见下文）。

---

## 3. `*.model3.json`（模型入口）

### 3.1 顶层字段

| 字段 | 说明 |
|------|------|
| `Version` | 固定为 **3**（Model3）。 |
| `FileReferences` | 指向 moc、贴图、物理、显示信息、表情、动作等相对路径。 |
| `Groups` | 眨眼、口型等参数分组（`EyeBlink`、`LipSync` 等）。 |
| `HitAreas` | 可选；命中区域，需与代码中 `HitAreaNameHead` / `HitAreaNameBody` 等一致时再依赖。 |

### 3.2 `FileReferences` 常用键

| 键 | 必选 | 说明 |
|----|------|------|
| `Moc` | 是 | 相对当前目录的 `.moc3` 路径。 |
| `Textures` | 是 | 字符串数组，每项为相对路径的 PNG。 |
| `Physics` | 通常需要 | `.physics3.json`。 |
| `DisplayInfo` | 通常需要 | `.cdi3.json`。 |
| `Expressions` | 推荐 | 数组：`{ "Name": "显示名", "File": "expressions/xxx.exp3.json" }`。未列出时，运行时可能不会加载对应表情。 |
| `Motions` | 可选 | 对象，键为动作组名（如 `Idle`、`TapBody`），值为动作文件列表；与 `lappdefine.js` 中 `MotionGroupIdle`、`MotionGroupTapBody` 对应。 |

### 3.3 `Expressions` 条目示例

```json
"Expressions": [
  { "Name": "微笑", "File": "expressions/smile.exp3.json" }
]
```

`Name` 为逻辑名称；`File` 为相对于模型目录的路径。

### 3.4 `Motions` 示例（与 Xiaozi 一致）

```json
"Motions": {
  "Idle": [
    { "File": "motions/待机动画.motion3.json" }
  ],
  "TapBody": [
    { "File": "motions/泡泡动画.motion3.json" }
  ]
}
```

组名需与 Demo 常量一致（见 `lappdefine.js`）。

---

## 4. `*.exp3.json`（表情）

- 顶层通常包含 `"Type": "Live2D Expression"` 与 `"Parameters"` 数组。
- 每项参数含 `Id`（Cubism 参数 ID）、`Value`、`Blend`（如 `Add`）等。
- 文件名与 `model3.json` 中 `Expressions[].File` 保持一致。

---

## 5. `*.motion3.json`（动作）

- 放在 **`motions/`** 下（或 `model3` 中写的相对路径对应位置）。
- 在 `model3.json` 的 `Motions` 中按组注册后，Demo 才能通过 `MotionGroupIdle` / `MotionGroupTapBody` 等播放。

---

## 6. 可选：VTube Studio 相关

| 文件 | 说明 |
|------|------|
| `{ModelName}.vtube.json` | VTube Studio 模型配置；`FileReferences.Model` 应指向本目录的 `{ModelName}.model3.json`；`Icon` 为同目录下图标文件名。 |
| `items_pinned_to_model.json` | VTS 场景钉选项导出，Web Demo 一般不读取。 |

Web 端运行时**只依赖** `model3.json` 及其引用链；`vtube.json` 便于与 VTS 工作流对齐。

---

## 7. 与本 Demo 代码的对应关系

| 配置位置 | 作用 |
|----------|------|
| `lappdefine.js` → `ResourcesPath` | 资源根 URL 前缀，一般为 `/Resources/`。 |
| `lappdefine.js` → `ModelDir` | 要加载的模型子目录名列表，须与目录名、`*.model3.json` 主文件名一致。 |
| `lappdefine.js` → `MotionGroupIdle` / `MotionGroupTapBody` | 与 `model3.json` 里 `Motions` 的键名一致。 |

加载流程：根据 `ModelDir` 请求 `{ResourcesPath}{目录名}/{目录名}.model3.json`，再按 `FileReferences` 拉取 moc、贴图、物理、表情与动作。

---

## 8. 新增模型检查清单

1. 在 `Resources/` 下新建目录，目录名 = `{ModelName}`。
2. 放入 `{ModelName}.model3.json`，且其中 `Moc`、`Textures`、`Physics`、`DisplayInfo` 路径均存在。
3. 所有需要在界面使用的表情，已写入 `FileReferences.Expressions`。
4. 若需要待机/点击身体动作，已放置 `motions/*.motion3.json` 并在 `Motions` 中注册。
5. 在 `lappdefine.js` 的 `ModelDir` 中追加 `{ModelName}`。
6. 浏览器强缓存时，若更新资源后未生效，可尝试硬刷新或清缓存。

---

## 9. 参考目录

本仓库中的示例：

- **`Resources/Xiaozi/`**：含完整 `Expressions`、`Motions`（`motions/`）、`Xiaozi.model3.json` 聚合入口（引用 `Purple紫.*` 资源文件）。
- **`Resources/Xiaogou/`**：`Xiaogou.model3.json` 与 `Xiaogou.moc3`、`Xiaogou.8192/` 等同前缀；`expressions/` 已登记；含 **`HitAreas`**（`Head` / `Body`，详见 [`Live2D_model3模型设置说明.md`](./Live2D_model3模型设置说明.md)）。

以上为项目内约定与最佳实践；Cubism 官方格式细节以 Live2D Cubism SDK 文档为准。
