import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# 在导入 router 之前加载 PY/.env，供 Ollama、DashScope 等读取
load_dotenv(Path(__file__).resolve().parent / ".env")

from fastapi import FastAPI

from utils.live2d_catalog import init_catalog
from router import asr_ws, wschat
from live2d_db.http_api import router as live2d_db_router
from live2d_db.long_memory_consolidator import (
    start_long_memory_consolidator,
    stop_long_memory_consolidator,
)
from live2d_db.remind_trigger_scheduler import (
    start_remind_trigger_scheduler,
    stop_remind_trigger_scheduler,
)
from fastapi.middleware.cors import CORSMiddleware




@asynccontextmanager
async def lifespan(app: FastAPI):
    # 扫描 Resources 下表情/动作，供 LLM 系统提示与 chunk 附带字段
    init_catalog()
    await start_long_memory_consolidator()
    await start_remind_trigger_scheduler()
    try:
        yield
    finally:
        await stop_remind_trigger_scheduler()
        await stop_long_memory_consolidator()


app = FastAPI(lifespan=lifespan)

app.include_router(wschat.router, tags=["chat"])
app.include_router(asr_ws.router, tags=["asr"])
app.include_router(live2d_db_router)

def _cors_allow_origins() -> list[str]:
    defaults = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5000",
        "http://127.0.0.1:5000",
    ]
    extra = (os.environ.get("CORS_ORIGINS") or "").strip()
    if not extra:
        return defaults
    merged = list(defaults)
    for o in extra.split(","):
        u = o.strip()
        if u and u not in merged:
            merged.append(u)
    return merged


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_credentials=True,
    allow_methods=["*"],  # 包含 OPTIONS
    allow_headers=["*"],
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)