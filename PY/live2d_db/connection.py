"""MySQL 连接封装（PyMySQL）。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator, Iterator, Optional

import pymysql
from pymysql.cursors import DictCursor

from .db_config import DbConfig


def get_connection(config: Optional[DbConfig] = None, **kwargs: Any) -> pymysql.connections.Connection:
    cfg = config or DbConfig.from_env()
    return pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        charset=cfg.charset,
        cursorclass=DictCursor,
        autocommit=False,
        **kwargs,
    )


@contextmanager
def connection_ctx(config: Optional[DbConfig] = None, **kwargs: Any) -> Iterator[pymysql.connections.Connection]:
    conn = get_connection(config, **kwargs)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
