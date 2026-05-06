"""FastAPI 依赖：数据库连接（请求结束自动 commit/close）。"""

from __future__ import annotations

from typing import Generator

import pymysql

from .connection import connection_ctx
from .db_config import DbConfig


def get_db() -> Generator[pymysql.connections.Connection, None, None]:
    with connection_ctx(DbConfig.from_env()) as conn:
        yield conn
