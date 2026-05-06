"""执行 SQL 脚本与参数化查询的辅助模块（初始化库、批量执行 DDL）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, Union

import pymysql
from pymysql.cursors import DictCursor

from .connection import get_connection
from .db_config import DbConfig

# 按分号拆分 SQL 文件（忽略字符串内的分号简化处理：建表脚本通常无嵌套引号分号问题）
_STATEMENT_SPLIT = re.compile(r";\s*\n|;\s*$")


def split_sql_statements(sql_text: str) -> List[str]:
    """将多语句 SQL 文本拆成单条语句列表（跳过纯注释/空块）。"""
    parts: List[str] = []
    for chunk in _STATEMENT_SPLIT.split(sql_text):
        s = chunk.strip()
        if not s or s.startswith("--"):
            continue
        parts.append(s)
    return parts


def execute_sql_file(
    path: Union[str, Path],
    config: Optional[DbConfig] = None,
    *,
    commit: bool = True,
) -> int:
    """
    读取 .sql 文件并逐条执行。
    返回成功执行的语句条数。
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    statements = split_sql_statements(text)
    conn = get_connection(config)
    n_ok = 0
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
                n_ok += 1
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return n_ok


def execute_many(
    conn: pymysql.connections.Connection,
    sql: str,
    params_seq: Sequence[Tuple[Any, ...]],
) -> int:
    """同一 SQL 批量执行多组参数。"""
    with conn.cursor() as cur:
        return int(cur.executemany(sql, list(params_seq)))


def fetch_all(
    conn: pymysql.connections.Connection,
    sql: str,
    params: Optional[Tuple[Any, ...]] = None,
) -> List[dict]:
    """参数化查询，返回字典行列表。"""
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return list(cur.fetchall())


def init_database_from_package_schema(config: Optional[DbConfig] = None) -> int:
    """执行本包内 schema.sql（需已创建数据库并设置 MYSQL_DATABASE）。"""
    schema = Path(__file__).resolve().parent / "schema.sql"
    return execute_sql_file(schema, config=config)
