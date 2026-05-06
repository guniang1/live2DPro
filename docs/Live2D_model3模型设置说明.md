# Live2D `*.model3.json` 模型设置说明

本文以 `Demo/public/Resources/Xiaogou/Xiaogou.model3.json` 为例，说明 **Cubism Model3** 入口配置中各 **顶层字段**、**FileReferences** 子项及 **Groups** 的含义与作用。

---

## 1. 文件角色

- **`*.model3.json`** 是运行时加载模型的 **入口**：SDK 先读它，再按其中的相对路径去加载 moc、贴图、物理、显示信息、表情、动作等。
- 路径均 **相对于该 `model3.json` 所在目录**（本例为 `Xiaogou/`）。

---

## 2. 顶层字段

| 字段 | 含义 |
|------|------|
| `Version` | 模型设置格式版本，**Model3 固定为 `3`**。 |
| `FileReferences` | **资源引用表**：列出本模型依赖的所有外部文件。 |
| `Groups` | **参数分组声明**：告诉 SDK 哪些参数用于**自动眨眼**、**口型同步**等特殊逻辑。 |
| `HitAreas` | **点击区域**：将逻辑名 `Head` / `Body` 映射到若干 **Drawable ID**（如 `ArtMesh0`），供 `hitTest` 使用。 |

### 2.1 `HitAreas`（与 `lappdefine.js` 中的命中名一致）

- 每项为 `{ "Id": "<DrawableId>", "Name": "Head" | "Body" }`。同一 `Name` 可重复多条，命中时只要**任一** Drawable 在屏幕坐标下被点中即成立（见 `LAppModel.hitTest`）。
- **`Name`** 须与 `HitAreaNameHead` / `HitAreaNameBody`（默认 `Head`、`Body`）一致。
- **`Id`** 必须是本模型 **moc 中真实存在的 Drawable ID**（运行时调试用日志可打印全部 ID，见 `LAppModel` 纹理加载完成后的 `[APP] 模型 Drawable IDs`）。

**`Xiaogou` 当前配置**：根据 `Xiaogou.moc3` 中出现的 `ArtMesh*` 编号划分——**编号小于 50** 的 Drawable 归为 `Head`，**50～120** 归为 `Body`（与 `Xiaozi.model3.json` 按网格勾选导出不同，属**按编号区间的近似划分**；若点击区域不理想，应在 Cubism Editor 中设置 Hit Area 后重新导出，或按编辑器导出的 ID 列表替换本数组）。

---

## 3. `FileReferences` 详解

### 3.1 `Moc`

```json
"Moc": "Xiaogou.moc3"
```

| 作用 | 指向 **模型核心二进制**（网格、参数、变形等），扩展名 **`.moc3`**。 |
|------|----------------------------------------------------------------------|
| 说明 | 无此文件则无法实例化模型；路径错误会导致加载失败。 |

---

### 3.2 `Textures`

```json
"Textures": [
  "Xiaogou.8192/texture_00.png"
]
```

| 作用 | **贴图 PNG 列表**，按绘制顺序排列；多张时依次写多条路径。 |
|------|----------------------------------------------------------|
| 说明 | `8192` 表示导出时的最大边长等约定目录名；**必须与磁盘上文件夹一致**。 |

---

### 3.3 `Physics`

```json
"Physics": "Xiaogou.physics3.json"
```

| 作用 | **物理模拟**（头发、衣服、挂饰等摇摆），由 **`.physics3.json`** 描述。 |
|------|------------------------------------------------------------------------|
| 说明 | 可不填则无物理；填写后 SDK 每帧会按该文件计算附加变形。 |

---

### 3.4 `DisplayInfo`

```json
"DisplayInfo": "Xiaogou.cdi3.json"
```

| 作用 | **显示信息（CDI）**：参数/部件的**可读名称与分组**，供编辑器与部分运行时用途。 |
|------|-------------------------------------------------------------------------------|
| 说明 | 详见 [`Live2D_cdi3显示信息说明.md`](./Live2D_cdi3显示信息说明.md)。 |

---

### 3.5 `Expressions`

数组中每一项为 **`{ "Name": "逻辑名", "File": "相对路径" }`**：

```json
{"Name": "1", "File": "expressions/1.exp3.json"}
```

| 字段 | 作用 |
|------|------|
| `Name` | 表情在 **SDK / 应用中的逻辑名称**，用于按名加载或切换表情。 |
| `File` | **`*.exp3.json`** 相对 `model3.json` 所在目录的路径。 |

本例登记了 `1`～`22` 以及两个带书名号名称的表情文件。**未在数组中出现的 `exp3` 文件**通常不会被当作该模型的「官方表情列表」加载（依实现而定）。

**本文件未包含 `Motions`**：若需待机、点击身体等动作，需增加 `motions/` 下 `*.motion3.json`，并在 `model3.json` 里写 `Motions` 对象（见 [`Live2D模型资源标准文件格式.md`](./Live2D模型资源标准文件格式.md)）。

---

## 4. `Groups`：特殊参数组

用于声明 **SDK 内置行为**要操作哪些参数，不是普通「文件夹」分组（普通分组在 **cdi3** 的 `ParameterGroups`）。

### 4.1 `EyeBlink`（自动眨眼）

```json
{
  "Target": "Parameter",
  "Name": "EyeBlink",
  "Ids": [
    "ParamEyeLOpen",
    "ParamEyeROpen"
  ]
}
```

| 字段 | 含义 |
|------|------|
| `Target` | 固定为 **`Parameter`**，表示下面 `Ids` 是**参数 ID**。 |
| `Name` | 固定为 **`EyeBlink`**，SDK 据此识别为**眨眼用参数**。 |
| `Ids` | **左眼、右眼开闭**参数在 moc 中的 ID；Demo 中 `LAppModel` 会对这些参数做自动眨眼。 |

若 `Ids` 为空或与模型不匹配，自动眨眼可能无效。

---

### 4.2 `LipSync`（口型同步）

```json
{
  "Target": "Parameter",
  "Name": "LipSync",
  "Ids": []
}
```

| 字段 | 含义 |
|------|------|
| `Name` | 固定为 **`LipSync`**，用于**口型**相关逻辑。 |
| `Ids` | **本例为空数组**：表示未在 model3 层指定口型用参数；口型可能由其它方式驱动或保持默认。 |

若填入嘴部相关参数 ID，SDK 可将音频分析结果映射到这些参数（依应用与 Cubism 版本而定）。

---

## 5. 与本 Demo 代码的对应

| 配置 | 代码位置（概念） |
|------|------------------|
| 加载 `Xiaogou/Xiaogou.model3.json` | `lappdefine.js` 中 `ModelDir` 含 `Xiaogou`，与目录名一致。 |
| `MotionGroupIdle` / `TapBody` | 本 `model3` **未配置 `Motions`**，相关随机动作可能无文件可播或跳过。 |
| `EyeBlink` | `Groups` 中已指定双眼开闭参数，与示例工程的自动眨眼逻辑一致。 |

---

## 6. 参考路径

- 示例文件：`Demo/public/Resources/Xiaogou/Xiaogou.model3.json`
- 含动作与按编辑器导出的 `HitAreas`：`Demo/public/Resources/Xiaozi/Xiaozi.model3.json`
- 含表情与按 Drawable 编号区间生成的 `HitAreas`：`Demo/public/Resources/Xiaogou/Xiaogou.model3.json`
