# live2DPro

情感交互 Live2D 数字人相关演示工程（Cubism Web 前端、Python 后端与文档）。

## 目录概要

| 路径 | 说明 |
|------|------|
| `Demo/` | Vite 前端；进入目录后执行 `npm install`、`npm run build` |
| `PY/` | FastAPI 服务与数据库/记忆等脚本 |
| `docs/` | 架构、接口与论文说明类文档 |
| `Core/`、`Framework/`、`Samples/` | Cubism SDK 与示例（遵循各自许可证） |

## 未纳入版本库的内容

以下目录体积过大或由本地环境生成，已通过根目录 `.gitignore` 排除；克隆仓库后请按需自行准备：

- `PY/GPT-SoVITS-beta0706/` — GPT-SoVITS 环境与权重，按项目文档部署
- `PY/ffmpeg-8.1.1/` — 请安装系统 FFmpeg 并配置 `PATH`
- `_internal/` — PyInstaller 等打包产物
- `minio_data/` — 本地 MinIO 数据目录
- `**/node_modules/`、`Demo/dist/`、`**/.env` — 依赖、构建输出与密钥

更细的架构与接口说明见 `docs/`。
