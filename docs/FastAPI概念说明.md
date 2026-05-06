# FastAPI 概念说明：router、app、APIRouter、FastAPI

## FastAPI

**FastAPI** 是一个用于构建 Web API 的 Python 框架。

- 提供装饰器（如 `@app.get()`）定义 HTTP 接口
- 自动生成 OpenAPI 文档
- 支持异步（async/await）

---

## app（应用实例）

**app** 是 FastAPI 的**主应用对象**，一般这样创建：

```python
from fastapi import FastAPI
app = FastAPI()
```

- 代表整个 Web 应用
- 所有路由最终都要挂到这个 `app` 上
- 启动时用：`uvicorn main:app`，这里的 `app` 就是主应用实例

---

## APIRouter（路由组）

**APIRouter** 用来把一组相关路由打包成一个「子应用」：

```python
from fastapi import APIRouter
router = APIRouter()

@router.get("/users")
def get_users():
    return {"users": []}

@router.websocket("/ws/chat")
async def chat(websocket: WebSocket):
    ...
```

- 相当于一个「路由集合」
- 可以按功能拆分（如用户、聊天、订单等）
- 本身不能单独运行，需要挂到 `app` 上

---

## router（路由）

**router** 通常指：

1. **APIRouter 实例**：上面定义的 `router`
2. **路由本身**：某个 URL 路径和对应处理函数的映射，例如 `/ws/chat` → `chat_websocket`

---

## 它们之间的关系

```
FastAPI（框架）
    │
    └── app（主应用实例）
            │
            ├── 直接定义路由：@app.get("/")
            │
            └── 挂载 APIRouter：app.include_router(router)
                    │
                    └── router（APIRouter 实例）
                            │
                            └── 包含多个路由：@router.get()、@router.websocket()
```

---

## 简单类比

| 概念 | 类比 |
|------|------|
| **FastAPI** | 整套「建网站」的工具 |
| **app** | 你的网站本身 |
| **APIRouter** | 网站里的一个模块（如「用户中心」「聊天」等） |
| **router** | 模块里的具体页面或接口 |

---

## 在本项目中的用法

- `main.py` 里创建 `app = FastAPI()`，作为主应用
- `wschat.py` 里创建 `router = APIRouter()`，定义 WebSocket 等路由
- 在 `main.py` 里用 `app.include_router(router)` 把聊天相关路由挂到主应用上
