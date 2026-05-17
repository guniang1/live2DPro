"""各表 CRUD 与查询（DAO 层）。

约定
----
- 第一个参数 ``conn`` 为 PyMySQL 连接；由调用方控制事务（如 FastAPI ``connection_ctx`` 在请求结束 commit）。
- 所有 SQL 使用 ``%s`` 参数化占位，禁止拼接用户输入。
- ``limit`` / ``offset``：分页；``count_*`` 与对应 ``list_*`` 条件一致，便于前端 total/分页条。
- 返回值：查询单条返回 ``Optional[实体]``；列表返回 ``List``；``delete``/``update`` 返回受影响行数 ``int``；
  ``insert``/``create`` 返回 ``lastrowid``。

命名
----
- ``get_by_id`` / ``get_by_*``：主键或业务唯一键查一条。
- ``list_*`` / ``find_*``：多条；``find`` 多用于非主键组合条件。
- ``count_*``：与列表查询同条件的行数统计。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pymysql

from .long_memory_fields import LONG_MEMORY_DB_TEXT_COLUMNS
from .package_key_util import normalize_package_key as _normalize_pkg_key
from .entities import (
    BackgroundImage,
    ChatSession,
    Live2dModelAsset,
    Live2dTtsRefer,
    LongMemory,
    Persona,
    RemindTrigger,
    User,
    UserProfile,
)


def _parse_dt(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    return v


def _count(conn: pymysql.connections.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    """执行 ``SELECT COUNT(*) AS cnt ...``，返回整数行数。"""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        if not row:
            return 0
        return int(row["cnt"])


def _page(limit: int, offset: int) -> Tuple[int, int]:
    """将分页参数规范为非负；避免异常 LIMIT。"""
    lim = max(1, min(int(limit), 10_000))
    off = max(0, int(offset))
    return lim, off


class UserRepository:
    """表 ``user``：账号与基础资料。"""

    TABLE = "user"

    @staticmethod
    def insert(conn: pymysql.connections.Connection, u: User) -> int:
        sql = (
            "INSERT INTO `user` (username, password, nickname, phone, email, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (u.username, u.password, u.nickname, u.phone, u.email, u.status),
            )
            return int(cur.lastrowid)

    @staticmethod
    def get_by_id(conn: pymysql.connections.Connection, user_id: int) -> Optional[User]:
        sql = "SELECT * FROM `user` WHERE user_id = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (user_id,))
            row = cur.fetchone()
        return UserRepository._row_to_user(row) if row else None

    create = insert

    @staticmethod
    def get_by_username(conn: pymysql.connections.Connection, username: str) -> Optional[User]:
        sql = "SELECT * FROM `user` WHERE username = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (username,))
            row = cur.fetchone()
        return UserRepository._row_to_user(row) if row else None

    @staticmethod
    def get_by_phone(conn: pymysql.connections.Connection, phone: str) -> Optional[User]:
        """按手机号查询（唯一约束字段）。"""
        sql = "SELECT * FROM `user` WHERE phone = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (phone,))
            row = cur.fetchone()
        return UserRepository._row_to_user(row) if row else None

    @staticmethod
    def get_by_email(conn: pymysql.connections.Connection, email: str) -> Optional[User]:
        """按邮箱查询（唯一约束字段）。"""
        sql = "SELECT * FROM `user` WHERE email = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (email,))
            row = cur.fetchone()
        return UserRepository._row_to_user(row) if row else None

    @staticmethod
    def update(conn: pymysql.connections.Connection, u: User) -> int:
        sql = (
            "UPDATE `user` SET username=%s, password=%s, nickname=%s, phone=%s, email=%s, status=%s "
            "WHERE user_id=%s"
        )
        with conn.cursor() as cur:
            n = cur.execute(
                sql,
                (u.username, u.password, u.nickname, u.phone, u.email, u.status, u.user_id),
            )
        return int(n)

    @staticmethod
    def delete_by_id(conn: pymysql.connections.Connection, user_id: int) -> int:
        sql = "DELETE FROM `user` WHERE user_id = %s"
        with conn.cursor() as cur:
            return int(cur.execute(sql, (user_id,)))

    @staticmethod
    def list_page(
        conn: pymysql.connections.Connection, *, limit: int = 50, offset: int = 0
    ) -> List[User]:
        """按 ``user_id`` 升序分页；与 :meth:`count_all` 配合做全表分页。"""
        lim, off = _page(limit, offset)
        sql = "SELECT * FROM `user` ORDER BY user_id ASC LIMIT %s OFFSET %s"
        with conn.cursor() as cur:
            cur.execute(sql, (lim, off))
            rows = cur.fetchall()
        return [UserRepository._row_to_user(r) for r in rows]

    @staticmethod
    def count_all(conn: pymysql.connections.Connection) -> int:
        """用户总条数。"""
        return _count(conn, "SELECT COUNT(*) AS cnt FROM `user`")

    @staticmethod
    def count_by_status(conn: pymysql.connections.Connection, status: int) -> int:
        """指定 ``status`` 的用户数（与 :meth:`list_by_status` 条件一致）。"""
        return _count(conn, "SELECT COUNT(*) AS cnt FROM `user` WHERE status = %s", (status,))

    @staticmethod
    def list_by_status(
        conn: pymysql.connections.Connection, status: int, *, limit: int = 100, offset: int = 0
    ) -> List[User]:
        """按账号状态筛选（``status``：1 正常 / 0 禁用）。"""
        lim, off = _page(limit, offset)
        sql = (
            "SELECT * FROM `user` WHERE status = %s ORDER BY user_id ASC LIMIT %s OFFSET %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (status, lim, off))
            rows = cur.fetchall()
        return [UserRepository._row_to_user(r) for r in rows]

    @staticmethod
    def _row_to_user(row: Dict[str, Any]) -> User:
        return User(
            user_id=row["user_id"],
            username=row["username"],
            password=row["password"],
            nickname=row.get("nickname"),
            phone=row.get("phone"),
            email=row.get("email"),
            create_time=_parse_dt(row.get("create_time")),
            update_time=_parse_dt(row.get("update_time")),
            status=int(row["status"]),
        )


class ChatSessionRepository:
    """表 ``chat_session``：单轮对话记录；可按 ``user_id``、``session_key`` 检索。"""

    @staticmethod
    def insert(conn: pymysql.connections.Connection, s: ChatSession) -> int:
        sql = (
            "INSERT INTO chat_session (user_id, package_key, user_input, ai_reply, emotion_tag, session_key) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    s.user_id,
                    s.package_key,
                    s.user_input,
                    s.ai_reply,
                    s.emotion_tag,
                    s.session_key,
                ),
            )
            return int(cur.lastrowid)

    create = insert

    @staticmethod
    def update(conn: pymysql.connections.Connection, s: ChatSession) -> int:
        sql = (
            "UPDATE chat_session SET user_id=%s, package_key=%s, user_input=%s, ai_reply=%s, emotion_tag=%s, session_key=%s "
            "WHERE session_id=%s"
        )
        with conn.cursor() as cur:
            return int(
                cur.execute(
                    sql,
                    (
                        s.user_id,
                        s.package_key,
                        s.user_input,
                        s.ai_reply,
                        s.emotion_tag,
                        s.session_key,
                        s.session_id,
                    ),
                )
            )

    @staticmethod
    def delete_by_id(conn: pymysql.connections.Connection, session_id: int) -> int:
        sql = "DELETE FROM chat_session WHERE session_id = %s"
        with conn.cursor() as cur:
            return int(cur.execute(sql, (session_id,)))

    @staticmethod
    def delete_by_user_package_keys(
        conn: pymysql.connections.Connection, user_id: int, package_keys: Sequence[str]
    ) -> int:
        keys = [str(k) for k in package_keys if str(k).strip()]
        if not keys:
            return 0
        placeholders = ", ".join(["%s"] * len(keys))
        sql = f"DELETE FROM chat_session WHERE user_id = %s AND package_key IN ({placeholders})"
        with conn.cursor() as cur:
            return int(cur.execute(sql, (user_id, *keys)))

    @staticmethod
    def get_by_id(conn: pymysql.connections.Connection, session_id: int) -> Optional[ChatSession]:
        sql = "SELECT * FROM chat_session WHERE session_id = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (session_id,))
            row = cur.fetchone()
        return ChatSessionRepository._row(row) if row else None

    @staticmethod
    def list_by_user(
        conn: pymysql.connections.Connection,
        user_id: int,
        package_key: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ChatSession]:
        """某用户全部会话记录，按对话时间倒序。"""
        lim, off = _page(limit, offset)
        if package_key is None:
            sql = (
                "SELECT * FROM chat_session WHERE user_id = %s ORDER BY create_time DESC LIMIT %s OFFSET %s"
            )
            params: Sequence[Any] = (user_id, lim, off)
        else:
            sql = (
                "SELECT * FROM chat_session WHERE user_id = %s AND package_key = %s "
                "ORDER BY create_time DESC LIMIT %s OFFSET %s"
            )
            params = (user_id, package_key, lim, off)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [ChatSessionRepository._row(r) for r in rows]

    @staticmethod
    def count_by_user(
        conn: pymysql.connections.Connection, user_id: int, package_key: Optional[str] = None
    ) -> int:
        """某用户对话条数（与 :meth:`list_by_user` 条件一致）。"""
        if package_key is None:
            return _count(
                conn,
                "SELECT COUNT(*) AS cnt FROM chat_session WHERE user_id = %s",
                (user_id,),
            )
        return _count(
            conn,
            "SELECT COUNT(*) AS cnt FROM chat_session WHERE user_id = %s AND package_key = %s",
            (user_id, package_key),
        )

    @staticmethod
    def list_by_session_key(
        conn: pymysql.connections.Connection,
        user_id: int,
        session_key: str,
        package_key: Optional[str] = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ChatSession]:
        """同一 ``session_key`` 下的多轮消息（同一会话窗口）。"""
        lim, off = _page(limit, offset)
        if package_key is None:
            sql = (
                "SELECT * FROM chat_session WHERE user_id = %s AND session_key = %s "
                "ORDER BY create_time ASC LIMIT %s OFFSET %s"
            )
            params: Sequence[Any] = (user_id, session_key, lim, off)
        else:
            sql = (
                "SELECT * FROM chat_session WHERE user_id = %s AND package_key = %s AND session_key = %s "
                "ORDER BY create_time ASC LIMIT %s OFFSET %s"
            )
            params = (user_id, package_key, session_key, lim, off)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [ChatSessionRepository._row(r) for r in rows]

    @staticmethod
    def count_by_user_and_session(
        conn: pymysql.connections.Connection,
        user_id: int,
        session_key: str,
        package_key: Optional[str] = None,
    ) -> int:
        """某用户某 ``session_key`` 下消息条数。"""
        if package_key is None:
            return _count(
                conn,
                "SELECT COUNT(*) AS cnt FROM chat_session WHERE user_id = %s AND session_key = %s",
                (user_id, session_key),
            )
        return _count(
            conn,
            "SELECT COUNT(*) AS cnt FROM chat_session WHERE user_id = %s AND package_key = %s AND session_key = %s",
            (user_id, package_key, session_key),
        )

    @staticmethod
    def list_recent_by_user(
        conn: pymysql.connections.Connection,
        user_id: int,
        *,
        hours: int = 24,
        package_key: Optional[str] = None,
        limit: int = 1000,
    ) -> List[ChatSession]:
        """某用户近 N 小时会话，按 create_time 倒序。"""
        lim = max(1, min(int(limit), 10_000))
        safe_hours = max(1, int(hours))
        cutoff = datetime.now() - timedelta(hours=safe_hours)
        if package_key is None:
            sql = (
                "SELECT * FROM chat_session WHERE user_id = %s AND create_time >= %s "
                "ORDER BY create_time DESC LIMIT %s"
            )
            params: Sequence[Any] = (user_id, cutoff, lim)
        else:
            sql = (
                "SELECT * FROM chat_session WHERE user_id = %s AND package_key = %s AND create_time >= %s "
                "ORDER BY create_time DESC LIMIT %s"
            )
            params = (user_id, package_key, cutoff, lim)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [ChatSessionRepository._row(r) for r in rows]

    @staticmethod
    def list_for_long_memory_window(
        conn: pymysql.connections.Connection,
        user_id: int,
        package_keys: Sequence[str],
        *,
        since_exclusive: Optional[datetime],
        window_start: datetime,
        limit: int = 5000,
    ) -> List[ChatSession]:
        """周期概要更新专用：仅从表 ``chat_session`` 取轮次（不使用 Redis）。多 ``package_key``（同逻辑包不同写法）下，
        取时间窗内对话，按 ``create_time`` 正序。"""
        keys = [str(k) for k in package_keys if str(k).strip()]
        if not keys:
            return []
        lim = max(1, min(int(limit), 20_000))
        placeholders = ",".join(["%s"] * len(keys))
        if since_exclusive is None:
            sql = (
                f"SELECT * FROM chat_session WHERE user_id = %s AND package_key IN ({placeholders}) "
                "AND create_time >= %s ORDER BY create_time ASC LIMIT %s"
            )
            params = (user_id, *keys, window_start, lim)
        else:
            sql = (
                f"SELECT * FROM chat_session WHERE user_id = %s AND package_key IN ({placeholders}) "
                "AND create_time > %s AND create_time >= %s ORDER BY create_time ASC LIMIT %s"
            )
            params = (user_id, *keys, since_exclusive, window_start, lim)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [ChatSessionRepository._row(r) for r in rows]

    @staticmethod
    def distinct_package_keys_for_user(conn: pymysql.connections.Connection, user_id: int) -> List[str]:
        """该用户在 chat_session 中出现过的所有 ``package_key``（去重）。"""
        sql = "SELECT DISTINCT package_key FROM chat_session WHERE user_id = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (user_id,))
            rows = cur.fetchall()
        return [str(r.get("package_key") or "") for r in rows if str(r.get("package_key") or "").strip()]

    @staticmethod
    def distinct_user_normalized_packages_in_window(
        conn: pymysql.connections.Connection, activity_window_seconds: int
    ) -> List[Tuple[int, str]]:
        """近 ``activity_window_seconds`` 秒内 ``chat_session`` 有过会话的 ``(user_id, 规范化 package_key)``，去重。"""
        safe_period = max(60, min(int(activity_window_seconds), 86400 * 14))
        sql = (
            "SELECT DISTINCT user_id, package_key FROM chat_session "
            "WHERE create_time >= NOW() - INTERVAL %s SECOND"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (safe_period,))
            rows = cur.fetchall()
        seen: set[Tuple[int, str]] = set()
        out: List[Tuple[int, str]] = []
        for r in rows:
            uid = int(r["user_id"])
            raw_pkg = str(r.get("package_key") or "default")
            pkg_norm = _normalize_pkg_key(raw_pkg, fallback="default")
            key = (uid, pkg_norm)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    @staticmethod
    def _row(row: Dict[str, Any]) -> ChatSession:
        return ChatSession(
            session_id=row["session_id"],
            user_id=row["user_id"],
            package_key=row.get("package_key") or "",
            user_input=row["user_input"],
            ai_reply=row["ai_reply"],
            emotion_tag=row.get("emotion_tag"),
            session_key=row["session_key"],
            create_time=_parse_dt(row.get("create_time")),
        )


class LongMemoryRepository:
    """表 ``long_memory``：长期记忆；可按用户、记忆类型筛选；(user_id, package_key) 对应唯一一行周期概要。"""

    @staticmethod
    def _dim_tuple(m: LongMemory) -> tuple[str, ...]:
        return tuple(str(getattr(m, k, "") or "").strip() for k in LONG_MEMORY_DB_TEXT_COLUMNS)

    @staticmethod
    def _dim_sql_placeholders() -> str:
        return ", ".join(["%s"] * len(LONG_MEMORY_DB_TEXT_COLUMNS))

    @staticmethod
    def _dim_sql_columns() -> str:
        return ", ".join(LONG_MEMORY_DB_TEXT_COLUMNS)

    @staticmethod
    def _row_dim_cells(row: Dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for k in LONG_MEMORY_DB_TEXT_COLUMNS:
            v = row.get(k)
            out[k] = str(v).strip() if v is not None else ""
        return out

    @staticmethod
    def insert(conn: pymysql.connections.Connection, m: LongMemory) -> int:
        cols = LongMemoryRepository._dim_sql_columns()
        ph = LongMemoryRepository._dim_sql_placeholders()
        sql = (
            f"INSERT INTO long_memory (user_id, package_key, memory_type, {cols}, last_consolidate_time) "
            f"VALUES (%s, %s, %s, {ph}, %s)"
        )
        params: tuple[Any, ...] = (
            m.user_id,
            m.package_key or "default",
            m.memory_type or "long",
            *LongMemoryRepository._dim_tuple(m),
            m.last_consolidate_time,
        )
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return int(cur.lastrowid)

    create = insert

    @staticmethod
    def get_by_id(conn: pymysql.connections.Connection, memory_id: int) -> Optional[LongMemory]:
        sql = "SELECT * FROM long_memory WHERE memory_id = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (memory_id,))
            row = cur.fetchone()
        return LongMemoryRepository._row(row) if row else None

    @staticmethod
    def list_by_user(
        conn: pymysql.connections.Connection, user_id: int, limit: int = 100, offset: int = 0
    ) -> List[LongMemory]:
        """某用户记忆列表，按 ``update_time`` 倒序。"""
        lim, off = _page(limit, offset)
        sql = (
            "SELECT * FROM long_memory WHERE user_id = %s ORDER BY update_time DESC LIMIT %s OFFSET %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, lim, off))
            rows = cur.fetchall()
        return [LongMemoryRepository._row(r) for r in rows]

    @staticmethod
    def list_by_user_and_type(
        conn: pymysql.connections.Connection,
        user_id: int,
        memory_type: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[LongMemory]:
        """按用户 + 记忆类型（瞬时/短期/长期等）筛选。"""
        lim, off = _page(limit, offset)
        sql = (
            "SELECT * FROM long_memory WHERE user_id = %s AND memory_type = %s "
            "ORDER BY update_time DESC LIMIT %s OFFSET %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, memory_type, lim, off))
            rows = cur.fetchall()
        return [LongMemoryRepository._row(r) for r in rows]

    @staticmethod
    def count_by_user(
        conn: pymysql.connections.Connection, user_id: int, *, memory_type: Optional[str] = None
    ) -> int:
        """记忆条数；若给 ``memory_type`` 则与 :meth:`list_by_user_and_type` 条件一致。"""
        if memory_type is None:
            return _count(conn, "SELECT COUNT(*) AS cnt FROM long_memory WHERE user_id = %s", (user_id,))
        return _count(
            conn,
            "SELECT COUNT(*) AS cnt FROM long_memory WHERE user_id = %s AND memory_type = %s",
            (user_id, memory_type),
        )

    @staticmethod
    def update(conn: pymysql.connections.Connection, m: LongMemory) -> int:
        sets = ", ".join([f"{k}=%s" for k in LONG_MEMORY_DB_TEXT_COLUMNS])
        sql = (
            f"UPDATE long_memory SET package_key=%s, memory_type=%s, {sets}, last_consolidate_time=%s "
            "WHERE memory_id=%s"
        )
        params = (
            m.package_key or "default",
            m.memory_type or "long",
            *LongMemoryRepository._dim_tuple(m),
            m.last_consolidate_time,
            m.memory_id,
        )
        with conn.cursor() as cur:
            n = cur.execute(sql, params)
        return int(n)

    @staticmethod
    def delete_by_id(conn: pymysql.connections.Connection, memory_id: int) -> int:
        sql = "DELETE FROM long_memory WHERE memory_id = %s"
        with conn.cursor() as cur:
            return int(cur.execute(sql, (memory_id,)))

    @staticmethod
    def delete_by_user_package_keys(
        conn: pymysql.connections.Connection, user_id: int, package_keys: Sequence[str]
    ) -> int:
        keys = [str(k) for k in package_keys if str(k).strip()]
        if not keys:
            return 0
        placeholders = ", ".join(["%s"] * len(keys))
        sql = f"DELETE FROM long_memory WHERE user_id = %s AND package_key IN ({placeholders})"
        with conn.cursor() as cur:
            return int(cur.execute(sql, (user_id, *keys)))

    @staticmethod
    def _row(row: Dict[str, Any]) -> LongMemory:
        cells = LongMemoryRepository._row_dim_cells(row)
        return LongMemory(
            memory_id=row["memory_id"],
            user_id=row["user_id"],
            package_key=str(row.get("package_key") or "default"),
            memory_type=str(row.get("memory_type") or "long"),
            period_overview=cells["period_overview"],
            create_time=_parse_dt(row.get("create_time")),
            update_time=_parse_dt(row.get("update_time")),
            last_consolidate_time=_parse_dt(row.get("last_consolidate_time")),
        )

    @staticmethod
    def get_by_user_pkg(
        conn: pymysql.connections.Connection, user_id: int, package_key: str
    ) -> Optional[LongMemory]:
        sql = "SELECT * FROM long_memory WHERE user_id = %s AND package_key = %s LIMIT 1"
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, package_key))
            row = cur.fetchone()
        return LongMemoryRepository._row(row) if row else None

    @staticmethod
    def upsert_by_user_pkg(conn: pymysql.connections.Connection, m: LongMemory) -> None:
        """按 (user_id, package_key) 插入或更新一行（周期概要）。"""
        cols = LongMemoryRepository._dim_sql_columns()
        ph = LongMemoryRepository._dim_sql_placeholders()
        dup_sets = ", ".join([f"{k}=VALUES({k})" for k in LONG_MEMORY_DB_TEXT_COLUMNS])
        sql = (
            f"INSERT INTO long_memory (user_id, package_key, memory_type, {cols}, last_consolidate_time) "
            f"VALUES (%s, %s, %s, {ph}, %s) ON DUPLICATE KEY UPDATE memory_type=VALUES(memory_type), "
            f"{dup_sets}, last_consolidate_time=VALUES(last_consolidate_time), update_time=CURRENT_TIMESTAMP"
        )
        params: tuple[Any, ...] = (
            m.user_id,
            m.package_key or "default",
            m.memory_type or "long",
            *LongMemoryRepository._dim_tuple(m),
            m.last_consolidate_time,
        )
        with conn.cursor() as cur:
            cur.execute(sql, params)

    @staticmethod
    def list_candidates_for_consolidation(
        conn: pymysql.connections.Connection,
        activity_window_seconds: int,
        min_gap_since_last_consolidate_seconds: int,
    ) -> List[Tuple[int, str, Optional[datetime]]]:
        """周期概要更新的候选任务。

        - ``activity_window_seconds``：在 ``chat_session`` 中出现过会话的时间跨度（用于发现「近期活跃」的 user×包）。
        - ``min_gap_since_last_consolidate_seconds``：距上次 ``last_consolidate_time`` 至少间隔多久才再次触发周期概要更新（与拉取窗口无关）。
        """
        safe_activity = max(60, min(int(activity_window_seconds), 86400 * 14))
        safe_gap = max(60, min(int(min_gap_since_last_consolidate_seconds), 86400 * 14))
        sql_recent = (
            "SELECT DISTINCT user_id, package_key FROM chat_session "
            "WHERE create_time >= NOW() - INTERVAL %s SECOND"
        )
        with conn.cursor() as cur:
            cur.execute(sql_recent, (safe_activity,))
            recent_rows = cur.fetchall()
        out: List[Tuple[int, str, Optional[datetime]]] = []
        processed: set[Tuple[int, str]] = set()
        cutoff = datetime.now() - timedelta(seconds=safe_gap)
        for r in recent_rows:
            uid = int(r["user_id"])
            raw_pkg = str(r.get("package_key") or "default")
            pkg_norm = _normalize_pkg_key(raw_pkg, fallback="default")
            key = (uid, pkg_norm)
            if key in processed:
                continue
            processed.add(key)
            lm_row = LongMemoryRepository.get_by_user_pkg(conn, uid, pkg_norm)
            last_ts = lm_row.last_consolidate_time if lm_row else None
            if last_ts is not None and last_ts > cutoff:
                continue
            out.append((uid, pkg_norm, last_ts))
        return out


class PersonaRepository:
    """表 ``persona``：数字人人设。"""

    @staticmethod
    def insert(conn: pymysql.connections.Connection, p: Persona) -> int:
        sql = (
            "INSERT INTO persona (character_desc, tone_style, default_emotion, status, user_id, package_key) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    p.character_desc,
                    p.tone_style,
                    p.default_emotion,
                    p.status,
                    p.user_id,
                    p.package_key,
                ),
            )
            return int(cur.lastrowid)

    create = insert

    @staticmethod
    def update(conn: pymysql.connections.Connection, p: Persona) -> int:
        sql = (
            "UPDATE persona SET character_desc=%s, tone_style=%s, default_emotion=%s, status=%s, "
            "user_id=%s, package_key=%s WHERE persona_id=%s"
        )
        with conn.cursor() as cur:
            return int(
                cur.execute(
                    sql,
                    (
                        p.character_desc,
                        p.tone_style,
                        p.default_emotion,
                        p.status,
                        p.user_id,
                        p.package_key,
                        p.persona_id,
                    ),
                )
            )

    @staticmethod
    def delete_by_id(conn: pymysql.connections.Connection, persona_id: int) -> int:
        sql = "DELETE FROM persona WHERE persona_id = %s"
        with conn.cursor() as cur:
            return int(cur.execute(sql, (persona_id,)))

    @staticmethod
    def get_by_id(conn: pymysql.connections.Connection, persona_id: int) -> Optional[Persona]:
        sql = "SELECT * FROM persona WHERE persona_id = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (persona_id,))
            row = cur.fetchone()
        return PersonaRepository._row(row) if row else None

    @staticmethod
    def list_enabled(conn: pymysql.connections.Connection) -> List[Persona]:
        """仅 ``status=1`` 的全局人设模板（不含按模型包绑定的行）。"""
        sql = (
            "SELECT * FROM persona WHERE status = 1 AND user_id IS NULL AND package_key IS NULL "
            "ORDER BY persona_id"
        )
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return [PersonaRepository._row(r) for r in rows]

    @staticmethod
    def list_all(conn: pymysql.connections.Connection) -> List[Persona]:
        """全部人设（含禁用）。"""
        sql = "SELECT * FROM persona ORDER BY persona_id"
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return [PersonaRepository._row(r) for r in rows]

    @staticmethod
    def list_by_status(
        conn: pymysql.connections.Connection, status: int, *, limit: int = 200, offset: int = 0
    ) -> List[Persona]:
        """按状态分页（``status``：1 启用 / 0 禁用）。"""
        lim, off = _page(limit, offset)
        sql = "SELECT * FROM persona WHERE status = %s ORDER BY persona_id ASC LIMIT %s OFFSET %s"
        with conn.cursor() as cur:
            cur.execute(sql, (status, lim, off))
            rows = cur.fetchall()
        return [PersonaRepository._row(r) for r in rows]

    @staticmethod
    def count_all(conn: pymysql.connections.Connection) -> int:
        return _count(conn, "SELECT COUNT(*) AS cnt FROM persona")

    @staticmethod
    def count_by_status(conn: pymysql.connections.Connection, status: int) -> int:
        """与 :meth:`list_by_status` 条件一致。"""
        return _count(conn, "SELECT COUNT(*) AS cnt FROM persona WHERE status = %s", (status,))

    @staticmethod
    def _row(row: Dict[str, Any]) -> Persona:
        uid = row.get("user_id")
        pkg = row.get("package_key")
        return Persona(
            persona_id=row["persona_id"],
            character_desc=row["character_desc"],
            tone_style=row["tone_style"],
            default_emotion=row.get("default_emotion"),
            user_id=int(uid) if uid is not None else None,
            package_key=(pkg if pkg is None else str(pkg)),
            create_time=_parse_dt(row.get("create_time")),
            status=int(row["status"]),
        )

    @staticmethod
    def get_by_user_and_package(
        conn: pymysql.connections.Connection, user_id: int, package_key: str
    ) -> Optional[Persona]:
        sql = "SELECT * FROM persona WHERE user_id = %s AND package_key = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, package_key))
            row = cur.fetchone()
        return PersonaRepository._row(row) if row else None

    @staticmethod
    def list_package_personas_for_user(
        conn: pymysql.connections.Connection, user_id: int
    ) -> List[Persona]:
        """该用户下所有「按模型包绑定」的人设（``package_key`` 非空）。"""
        sql = (
            "SELECT * FROM persona WHERE user_id = %s AND package_key IS NOT NULL "
            "AND TRIM(package_key) <> '' ORDER BY package_key ASC, persona_id ASC"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (user_id,))
            rows = cur.fetchall()
        return [PersonaRepository._row(r) for r in rows]

    @staticmethod
    def resolve_persona_for_package(
        conn: pymysql.connections.Connection, user_id: int, package_key_query: str
    ) -> Optional[Persona]:
        """按用户 + 模型包解析人设：先精确键，再规范化键，再按规范化结果扫表（避免资源包名与入库键不一致导致读不到）。"""
        q = (package_key_query or "").strip()
        if not q:
            return None
        direct = PersonaRepository.get_by_user_and_package(conn, user_id, q)
        if direct is not None:
            return direct
        target = _normalize_pkg_key(q)
        if target != q:
            alt = PersonaRepository.get_by_user_and_package(conn, user_id, target)
            if alt is not None:
                return alt
        for p in PersonaRepository.list_package_personas_for_user(conn, user_id):
            pk = (p.package_key or "").strip()
            if not pk:
                continue
            if _normalize_pkg_key(pk) == target:
                return p
        return None

    @staticmethod
    def upsert_package_persona(
        conn: pymysql.connections.Connection,
        user_id: int,
        package_key: str,
        character_desc: str,
        tone_style: str,
    ) -> Persona:
        """某用户某模型包唯一人设：写入 ``character_desc``、``tone_style``（聊天 system 追加）。"""
        desc = (character_desc or "").strip()
        tone = (tone_style or "").strip()
        canonical = _normalize_pkg_key(package_key)
        existing = PersonaRepository.resolve_persona_for_package(conn, user_id, package_key)
        if existing is not None and existing.persona_id is not None:
            existing.character_desc = desc
            existing.tone_style = tone
            PersonaRepository.update(conn, existing)
            got = PersonaRepository.get_by_id(conn, int(existing.persona_id))
            assert got is not None
            return got
        p = Persona(
            character_desc=desc,
            tone_style=tone,
            default_emotion=None,
            status=1,
            user_id=user_id,
            package_key=canonical,
        )
        PersonaRepository.insert(conn, p)
        got = PersonaRepository.resolve_persona_for_package(conn, user_id, canonical)
        assert got is not None
        return got

    @staticmethod
    def delete_by_user_and_package(conn: pymysql.connections.Connection, user_id: int, package_key: str) -> int:
        sql = "DELETE FROM persona WHERE user_id = %s AND package_key = %s"
        with conn.cursor() as cur:
            return int(cur.execute(sql, (user_id, package_key)))


class UserProfileRepository:
    """表 ``user_profile``：用户画像；``user_id`` 唯一。"""

    @staticmethod
    def insert(conn: pymysql.connections.Connection, p: UserProfile) -> int:
        sql = (
            "INSERT INTO user_profile (user_id, display_name, user_tags, emotion_state, preferences, trouble_events) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)"
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    p.user_id,
                    p.display_name,
                    p.user_tags,
                    p.emotion_state,
                    p.preferences,
                    p.trouble_events,
                ),
            )
            return int(cur.lastrowid)

    create = insert

    @staticmethod
    def get_by_id(conn: pymysql.connections.Connection, profile_id: int) -> Optional[UserProfile]:
        sql = "SELECT * FROM user_profile WHERE profile_id = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (profile_id,))
            row = cur.fetchone()
        return UserProfileRepository._row(row) if row else None

    @staticmethod
    def get_by_user_id(conn: pymysql.connections.Connection, user_id: int) -> Optional[UserProfile]:
        sql = "SELECT * FROM user_profile WHERE user_id = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (user_id,))
            row = cur.fetchone()
        return UserProfileRepository._row(row) if row else None

    @staticmethod
    def update(conn: pymysql.connections.Connection, p: UserProfile) -> int:
        sql = (
            "UPDATE user_profile SET display_name=%s, user_tags=%s, emotion_state=%s, preferences=%s, trouble_events=%s "
            "WHERE profile_id=%s"
        )
        with conn.cursor() as cur:
            n = cur.execute(
                sql,
                (
                    p.display_name,
                    p.user_tags,
                    p.emotion_state,
                    p.preferences,
                    p.trouble_events,
                    p.profile_id,
                ),
            )
        return int(n)

    @staticmethod
    def list_candidates_for_profile_refresh(
        conn: pymysql.connections.Connection,
        activity_window_seconds: int,
        min_gap_since_last_refresh_seconds: int,
    ) -> List[int]:
        """24h 画像刷新候选 ``user_id``：近窗内有会话，且距上次 ``update_time`` 已满最短间隔。"""
        safe_activity = max(60, min(int(activity_window_seconds), 86400 * 14))
        safe_gap = max(60, min(int(min_gap_since_last_refresh_seconds), 86400 * 14))
        sql = (
            "SELECT DISTINCT cs.user_id AS user_id FROM chat_session cs "
            "LEFT JOIN user_profile up ON up.user_id = cs.user_id "
            "WHERE cs.create_time >= NOW() - INTERVAL %s SECOND "
            "AND (up.update_time IS NULL OR up.update_time <= NOW() - INTERVAL %s SECOND)"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (safe_activity, safe_gap))
            rows = cur.fetchall()
        out: List[int] = []
        seen: set[int] = set()
        for r in rows:
            uid = int(r["user_id"])
            if uid in seen:
                continue
            seen.add(uid)
            out.append(uid)
        return out

    @staticmethod
    def upsert_by_user_id(conn: pymysql.connections.Connection, p: UserProfile) -> int:
        existing = UserProfileRepository.get_by_user_id(conn, p.user_id)
        if existing is None:
            return UserProfileRepository.insert(conn, p)
        p.profile_id = existing.profile_id
        return UserProfileRepository.update(conn, p)

    @staticmethod
    def delete_by_id(conn: pymysql.connections.Connection, profile_id: int) -> int:
        sql = "DELETE FROM user_profile WHERE profile_id = %s"
        with conn.cursor() as cur:
            return int(cur.execute(sql, (profile_id,)))

    @staticmethod
    def delete_by_user_id(conn: pymysql.connections.Connection, user_id: int) -> int:
        sql = "DELETE FROM user_profile WHERE user_id = %s"
        with conn.cursor() as cur:
            return int(cur.execute(sql, (user_id,)))

    @staticmethod
    def _row(row: Dict[str, Any]) -> UserProfile:
        return UserProfile(
            profile_id=row["profile_id"],
            user_id=row["user_id"],
            display_name=row.get("display_name"),
            user_tags=row.get("user_tags"),
            emotion_state=row.get("emotion_state"),
            preferences=row.get("preferences"),
            trouble_events=row.get("trouble_events"),
            update_time=_parse_dt(row.get("update_time")),
        )


class RemindTriggerRepository:
    """表 ``remind_trigger``：关怀/待办触发；定时任务扫 :meth:`list_pending_before`。"""

    @staticmethod
    def insert(conn: pymysql.connections.Connection, t: RemindTrigger) -> int:
        sql = (
            "INSERT INTO remind_trigger (user_id, trigger_type, trigger_time, session_id, trigger_content, is_triggered) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    t.user_id,
                    t.trigger_type,
                    t.trigger_time,
                    t.session_id,
                    t.trigger_content,
                    t.is_triggered,
                ),
            )
            return int(cur.lastrowid)

    create = insert

    @staticmethod
    def update(conn: pymysql.connections.Connection, t: RemindTrigger) -> int:
        sql = (
            "UPDATE remind_trigger SET user_id=%s, trigger_type=%s, trigger_time=%s, session_id=%s, "
            "trigger_content=%s, is_triggered=%s WHERE trigger_id=%s"
        )
        with conn.cursor() as cur:
            return int(
                cur.execute(
                    sql,
                    (
                        t.user_id,
                        t.trigger_type,
                        t.trigger_time,
                        t.session_id,
                        t.trigger_content,
                        t.is_triggered,
                        t.trigger_id,
                    ),
                )
            )

    @staticmethod
    def delete_by_id(conn: pymysql.connections.Connection, trigger_id: int) -> int:
        sql = "DELETE FROM remind_trigger WHERE trigger_id = %s"
        with conn.cursor() as cur:
            return int(cur.execute(sql, (trigger_id,)))

    @staticmethod
    def delete_by_user_package_keys(
        conn: pymysql.connections.Connection, user_id: int, package_keys: Sequence[str]
    ) -> int:
        """删除关联到该包 ``chat_session`` 的提醒行（先于会话删除调用）。"""
        keys = [str(k) for k in package_keys if str(k).strip()]
        if not keys:
            return 0
        placeholders = ", ".join(["%s"] * len(keys))
        sql = (
            f"DELETE FROM remind_trigger WHERE session_id IN ("
            f"SELECT session_id FROM chat_session WHERE user_id = %s AND package_key IN ({placeholders})"
            f")"
        )
        with conn.cursor() as cur:
            return int(cur.execute(sql, (user_id, *keys)))

    @staticmethod
    def get_by_id(conn: pymysql.connections.Connection, trigger_id: int) -> Optional[RemindTrigger]:
        sql = "SELECT * FROM remind_trigger WHERE trigger_id = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (trigger_id,))
            row = cur.fetchone()
        return RemindTriggerRepository._row(row) if row else None

    @staticmethod
    def list_by_user(
        conn: pymysql.connections.Connection, user_id: int, limit: int = 100, offset: int = 0
    ) -> List[RemindTrigger]:
        """某用户全部触发记录，按 ``trigger_time`` 倒序。"""
        lim, off = _page(limit, offset)
        sql = (
            "SELECT * FROM remind_trigger WHERE user_id = %s ORDER BY trigger_time DESC LIMIT %s OFFSET %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, lim, off))
            rows = cur.fetchall()
        return [RemindTriggerRepository._row(r) for r in rows]

    @staticmethod
    def list_by_user_and_triggered(
        conn: pymysql.connections.Connection,
        user_id: int,
        is_triggered: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> List[RemindTrigger]:
        """按用户 + 是否已触发（0/1）筛选。"""
        lim, off = _page(limit, offset)
        sql = (
            "SELECT * FROM remind_trigger WHERE user_id = %s AND is_triggered = %s "
            "ORDER BY trigger_time DESC LIMIT %s OFFSET %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, is_triggered, lim, off))
            rows = cur.fetchall()
        return [RemindTriggerRepository._row(r) for r in rows]

    @staticmethod
    def count_by_user(
        conn: pymysql.connections.Connection, user_id: int, *, is_triggered: Optional[int] = None
    ) -> int:
        """触发记录数；若给 ``is_triggered``（0/1）则与 :meth:`list_by_user_and_triggered` 一致。"""
        if is_triggered is None:
            return _count(conn, "SELECT COUNT(*) AS cnt FROM remind_trigger WHERE user_id = %s", (user_id,))
        return _count(
            conn,
            "SELECT COUNT(*) AS cnt FROM remind_trigger WHERE user_id = %s AND is_triggered = %s",
            (user_id, is_triggered),
        )

    @staticmethod
    def list_pending_before(
        conn: pymysql.connections.Connection, before: datetime, limit: int = 200
    ) -> List[RemindTrigger]:
        """定时扫描：未触发且 ``trigger_time`` 不晚于 ``before``，按时间升序取前 ``limit`` 条。"""
        lim = max(1, min(int(limit), 10_000))
        sql = (
            "SELECT * FROM remind_trigger WHERE is_triggered = 0 AND trigger_time <= %s "
            "ORDER BY trigger_time ASC LIMIT %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (before, lim))
            rows = cur.fetchall()
        return [RemindTriggerRepository._row(r) for r in rows]

    @staticmethod
    def count_pending_before(conn: pymysql.connections.Connection, before: datetime) -> int:
        """与 :meth:`list_pending_before` 条件一致的待处理条数。"""
        return _count(
            conn,
            "SELECT COUNT(*) AS cnt FROM remind_trigger WHERE is_triggered = 0 AND trigger_time <= %s",
            (before,),
        )

    @staticmethod
    def mark_triggered(conn: pymysql.connections.Connection, trigger_id: int) -> int:
        sql = "UPDATE remind_trigger SET is_triggered = 1 WHERE trigger_id = %s"
        with conn.cursor() as cur:
            n = cur.execute(sql, (trigger_id,))
        return int(n)

    @staticmethod
    def reclaim_stale_delivering(
        conn: pymysql.connections.Connection, *, stale_seconds: int
    ) -> int:
        """将长时间卡在 ``is_triggered=2`` 的记录收回为待投递（进程崩溃或异常中断时）。"""
        sec = max(60, int(stale_seconds))
        sql = (
            "UPDATE remind_trigger SET is_triggered = 0, delivery_started_at = NULL "
            "WHERE is_triggered = 2 AND delivery_started_at IS NOT NULL "
            "AND delivery_started_at < DATE_SUB(NOW(), INTERVAL %s SECOND)"
        )
        with conn.cursor() as cur:
            n = cur.execute(sql, (sec,))
        return int(n)

    @staticmethod
    def begin_remind_delivery(conn: pymysql.connections.Connection, trigger_id: int) -> bool:
        """待投递 ``0→2``，并记下投递开始时刻；仅当仍为待投递时返回 True（互斥占用，尚未视为已触发）。"""
        sql = (
            "UPDATE remind_trigger SET is_triggered = 2, delivery_started_at = NOW() "
            "WHERE trigger_id = %s AND is_triggered = 0"
        )
        with conn.cursor() as cur:
            n = cur.execute(sql, (trigger_id,))
        return int(n) == 1

    @staticmethod
    def finish_remind_delivery(conn: pymysql.connections.Connection, trigger_id: int) -> bool:
        """已向客户端成功下发 ``remind_trigger`` JSON 正文后 ``2→1``（清空投递时刻）。"""
        sql = (
            "UPDATE remind_trigger SET is_triggered = 1, delivery_started_at = NULL "
            "WHERE trigger_id = %s AND is_triggered = 2"
        )
        with conn.cursor() as cur:
            n = cur.execute(sql, (trigger_id,))
        return int(n) == 1

    @staticmethod
    def release_remind_delivery(conn: pymysql.connections.Connection, trigger_id: int) -> int:
        """WebSocket 发送失败等：``2→0``，下次扫描或重连可再试。"""
        sql = (
            "UPDATE remind_trigger SET is_triggered = 0, delivery_started_at = NULL "
            "WHERE trigger_id = %s AND is_triggered = 2"
        )
        with conn.cursor() as cur:
            n = cur.execute(sql, (trigger_id,))
        return int(n)

    @staticmethod
    def claim_pending_trigger(conn: pymysql.connections.Connection, trigger_id: int) -> bool:
        """兼容旧名：等同于 :meth:`begin_remind_delivery`。"""
        return RemindTriggerRepository.begin_remind_delivery(conn, trigger_id)

    @staticmethod
    def release_trigger_claim(conn: pymysql.connections.Connection, trigger_id: int) -> int:
        """兼容旧名：等同于 :meth:`release_remind_delivery`。"""
        return RemindTriggerRepository.release_remind_delivery(conn, trigger_id)

    @staticmethod
    def list_pending_for_user_before(
        conn: pymysql.connections.Connection,
        user_id: int,
        before: datetime,
        *,
        limit: int = 100,
    ) -> List[RemindTrigger]:
        """某用户未触发且 ``trigger_time <= before``，按时间升序。"""
        lim = max(1, min(int(limit), 10_000))
        sql = (
            "SELECT * FROM remind_trigger WHERE user_id = %s AND is_triggered = 0 "
            "AND trigger_time <= %s ORDER BY trigger_time ASC LIMIT %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, before, lim))
            rows = cur.fetchall()
        return [RemindTriggerRepository._row(r) for r in rows]

    @staticmethod
    def _row(row: Dict[str, Any]) -> RemindTrigger:
        return RemindTrigger(
            trigger_id=row["trigger_id"],
            user_id=row["user_id"],
            trigger_type=row["trigger_type"],
            trigger_time=_parse_dt(row.get("trigger_time")),
            session_id=row.get("session_id"),
            trigger_content=row["trigger_content"],
            is_triggered=int(row["is_triggered"]),
            delivery_started_at=_parse_dt(row.get("delivery_started_at")),
            create_time=_parse_dt(row.get("create_time")),
        )


class BackgroundImageRepository:
    """表 ``background_image``：背景显示名与 MinIO 公开 URL（可按 URL 推导 object_key 做预签名）。"""

    TABLE = "background_image"

    @staticmethod
    def count_all(conn: pymysql.connections.Connection) -> int:
        return _count(conn, f"SELECT COUNT(*) AS cnt FROM `{BackgroundImageRepository.TABLE}`")

    @staticmethod
    def get_random_one(conn: pymysql.connections.Connection) -> Optional[BackgroundImage]:
        sql = f"SELECT * FROM `{BackgroundImageRepository.TABLE}` ORDER BY RAND() LIMIT 1"
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        return BackgroundImageRepository._row(row) if row else None

    @staticmethod
    def list_all(
        conn: pymysql.connections.Connection, *, limit: int = 500, offset: int = 0
    ) -> List[BackgroundImage]:
        lim, off = _page(limit, offset)
        sql = (
            f"SELECT * FROM `{BackgroundImageRepository.TABLE}` "
            "ORDER BY id ASC LIMIT %s OFFSET %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (lim, off))
            rows = cur.fetchall()
        return [BackgroundImageRepository._row(r) for r in rows]

    @staticmethod
    def _row(row: Dict[str, Any]) -> BackgroundImage:
        return BackgroundImage(
            id=int(row["id"]),
            name=str(row["name"] or ""),
            url=str(row["url"] or ""),
            create_time=_parse_dt(row.get("create_time")),
        )


class Live2dModelAssetRepository:
    """表 ``live2d_model_asset``：Resources 下模型包文件索引；与 ``user`` 外键，唯一 (user_id, package_key, relative_path)。"""

    @staticmethod
    def insert(conn: pymysql.connections.Connection, a: Live2dModelAsset) -> int:
        sql = (
            "INSERT INTO live2d_model_asset (user_id, package_key, relative_path, file_name, asset_type, public_url, "
            "object_key, mime_type, logical_name, motion_group, is_listed_in_model3, is_entry_model, "
            "file_size, sort_order, remark) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    a.user_id,
                    a.package_key,
                    a.relative_path,
                    a.file_name,
                    a.asset_type,
                    a.public_url,
                    a.object_key,
                    a.mime_type,
                    a.logical_name,
                    a.motion_group,
                    a.is_listed_in_model3,
                    a.is_entry_model,
                    a.file_size,
                    a.sort_order,
                    a.remark,
                ),
            )
            return int(cur.lastrowid)

    create = insert

    @staticmethod
    def update(conn: pymysql.connections.Connection, a: Live2dModelAsset) -> int:
        sql = (
            "UPDATE live2d_model_asset SET user_id=%s, package_key=%s, relative_path=%s, file_name=%s, asset_type=%s, "
            "public_url=%s, object_key=%s, mime_type=%s, logical_name=%s, motion_group=%s, "
            "is_listed_in_model3=%s, is_entry_model=%s, file_size=%s, sort_order=%s, remark=%s WHERE asset_id=%s"
        )
        with conn.cursor() as cur:
            return int(
                cur.execute(
                    sql,
                    (
                        a.user_id,
                        a.package_key,
                        a.relative_path,
                        a.file_name,
                        a.asset_type,
                        a.public_url,
                        a.object_key,
                        a.mime_type,
                        a.logical_name,
                        a.motion_group,
                        a.is_listed_in_model3,
                        a.is_entry_model,
                        a.file_size,
                        a.sort_order,
                        a.remark,
                        a.asset_id,
                    ),
                )
            )

    @staticmethod
    def delete_by_id(conn: pymysql.connections.Connection, asset_id: int) -> int:
        sql = "DELETE FROM live2d_model_asset WHERE asset_id = %s"
        with conn.cursor() as cur:
            return int(cur.execute(sql, (asset_id,)))

    @staticmethod
    def delete_by_package_key(
        conn: pymysql.connections.Connection, package_key: str, user_id: int
    ) -> int:
        sql = "DELETE FROM live2d_model_asset WHERE package_key = %s AND user_id = %s"
        with conn.cursor() as cur:
            return int(cur.execute(sql, (package_key, user_id)))

    @staticmethod
    def get_by_id(conn: pymysql.connections.Connection, asset_id: int) -> Optional[Live2dModelAsset]:
        sql = "SELECT * FROM live2d_model_asset WHERE asset_id = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (asset_id,))
            row = cur.fetchone()
        return Live2dModelAssetRepository._row(row) if row else None

    @staticmethod
    def get_by_package_and_rel(
        conn: pymysql.connections.Connection, user_id: int, package_key: str, relative_path: str
    ) -> Optional[Live2dModelAsset]:
        sql = (
            "SELECT * FROM live2d_model_asset WHERE user_id = %s AND package_key = %s AND relative_path = %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, package_key, relative_path))
            row = cur.fetchone()
        return Live2dModelAssetRepository._row(row) if row else None

    @staticmethod
    def list_by_package(
        conn: pymysql.connections.Connection,
        user_id: int,
        package_key: str,
        *,
        asset_type: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[Live2dModelAsset]:
        """某用户某包下文件列表；可选 ``asset_type`` 过滤。"""
        lim, off = _page(limit, offset)
        if asset_type is None:
            sql = (
                "SELECT * FROM live2d_model_asset WHERE user_id = %s AND package_key = %s "
                "ORDER BY sort_order ASC, asset_id ASC LIMIT %s OFFSET %s"
            )
            params: Sequence[Any] = (user_id, package_key, lim, off)
        else:
            sql = (
                "SELECT * FROM live2d_model_asset WHERE user_id = %s AND package_key = %s AND asset_type = %s "
                "ORDER BY sort_order ASC, asset_id ASC LIMIT %s OFFSET %s"
            )
            params = (user_id, package_key, asset_type, lim, off)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [Live2dModelAssetRepository._row(r) for r in rows]

    @staticmethod
    def count_by_package(
        conn: pymysql.connections.Connection,
        user_id: int,
        package_key: str,
        *,
        asset_type: Optional[str] = None,
    ) -> int:
        """与 :meth:`list_by_package` 同条件的行数。"""
        if asset_type is None:
            sql = (
                "SELECT COUNT(*) AS cnt FROM live2d_model_asset WHERE user_id = %s AND package_key = %s"
            )
            params: Sequence[Any] = (user_id, package_key)
        else:
            sql = (
                "SELECT COUNT(*) AS cnt FROM live2d_model_asset "
                "WHERE user_id = %s AND package_key = %s AND asset_type = %s"
            )
            params = (user_id, package_key, asset_type)
        return _count(conn, sql, params)

    @staticmethod
    def list_by_user(
        conn: pymysql.connections.Connection,
        user_id: int,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> List[Live2dModelAsset]:
        """某用户全部模型包文件（跨 ``package_key``），按包名、排序字段、主键排序。"""
        lim, off = _page(limit, offset)
        sql = (
            "SELECT * FROM live2d_model_asset WHERE user_id = %s "
            "ORDER BY package_key ASC, sort_order ASC, asset_id ASC LIMIT %s OFFSET %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, lim, off))
            rows = cur.fetchall()
        return [Live2dModelAssetRepository._row(r) for r in rows]

    @staticmethod
    def count_by_user(conn: pymysql.connections.Connection, user_id: int) -> int:
        """与 :meth:`list_by_user` 同条件的条数。"""
        return _count(conn, "SELECT COUNT(*) AS cnt FROM live2d_model_asset WHERE user_id = %s", (user_id,))

    @staticmethod
    def _row(row: Dict[str, Any]) -> Live2dModelAsset:
        return Live2dModelAsset(
            asset_id=row["asset_id"],
            user_id=int(row["user_id"]),
            package_key=row["package_key"],
            relative_path=row["relative_path"],
            file_name=row["file_name"],
            asset_type=row["asset_type"],
            public_url=row["public_url"],
            object_key=row.get("object_key"),
            mime_type=row.get("mime_type"),
            logical_name=row.get("logical_name"),
            motion_group=row.get("motion_group"),
            is_listed_in_model3=int(row.get("is_listed_in_model3") or 0),
            is_entry_model=int(row.get("is_entry_model") or 0),
            file_size=row.get("file_size"),
            sort_order=int(row["sort_order"]),
            remark=row.get("remark"),
            create_time=_parse_dt(row.get("create_time")),
            update_time=_parse_dt(row.get("update_time")),
        )


class Live2dTtsReferRepository:
    """表 ``live2d_tts_refer``：模型包级参考音频绑定（每用户每包唯一）。"""

    @staticmethod
    def insert(conn: pymysql.connections.Connection, r: Live2dTtsRefer) -> int:
        sql = (
            "INSERT INTO live2d_tts_refer (user_id, package_key, audio_object_key, audio_url, audio_format, prompt_text, prompt_language) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)"
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    r.user_id,
                    r.package_key,
                    r.audio_object_key,
                    r.audio_url,
                    r.audio_format,
                    r.prompt_text,
                    r.prompt_language,
                ),
            )
            return int(cur.lastrowid)

    create = insert

    @staticmethod
    def update(conn: pymysql.connections.Connection, r: Live2dTtsRefer) -> int:
        sql = (
            "UPDATE live2d_tts_refer SET user_id=%s, package_key=%s, audio_object_key=%s, audio_url=%s, "
            "audio_format=%s, prompt_text=%s, prompt_language=%s WHERE refer_id=%s"
        )
        with conn.cursor() as cur:
            return int(
                cur.execute(
                    sql,
                    (
                        r.user_id,
                        r.package_key,
                        r.audio_object_key,
                        r.audio_url,
                        r.audio_format,
                        r.prompt_text,
                        r.prompt_language,
                        r.refer_id,
                    ),
                )
            )

    @staticmethod
    def upsert_by_user_package(conn: pymysql.connections.Connection, r: Live2dTtsRefer) -> int:
        existing = Live2dTtsReferRepository.get_by_user_and_package(conn, r.user_id, r.package_key)
        if existing is None:
            return Live2dTtsReferRepository.insert(conn, r)
        r.refer_id = existing.refer_id
        Live2dTtsReferRepository.update(conn, r)
        return int(existing.refer_id or 0)

    @staticmethod
    def get_by_id(conn: pymysql.connections.Connection, refer_id: int) -> Optional[Live2dTtsRefer]:
        sql = "SELECT * FROM live2d_tts_refer WHERE refer_id = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (refer_id,))
            row = cur.fetchone()
        return Live2dTtsReferRepository._row(row) if row else None

    @staticmethod
    def get_by_user_and_package(
        conn: pymysql.connections.Connection, user_id: int, package_key: str
    ) -> Optional[Live2dTtsRefer]:
        sql = "SELECT * FROM live2d_tts_refer WHERE user_id = %s AND package_key = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, package_key))
            row = cur.fetchone()
        return Live2dTtsReferRepository._row(row) if row else None

    @staticmethod
    def list_by_user(conn: pymysql.connections.Connection, user_id: int) -> List[Live2dTtsRefer]:
        sql = "SELECT * FROM live2d_tts_refer WHERE user_id = %s ORDER BY package_key ASC, refer_id ASC"
        with conn.cursor() as cur:
            cur.execute(sql, (user_id,))
            rows = cur.fetchall()
        return [Live2dTtsReferRepository._row(r) for r in rows]

    @staticmethod
    def delete_by_user_and_package(conn: pymysql.connections.Connection, user_id: int, package_key: str) -> int:
        sql = "DELETE FROM live2d_tts_refer WHERE user_id = %s AND package_key = %s"
        with conn.cursor() as cur:
            return int(cur.execute(sql, (user_id, package_key)))

    @staticmethod
    def _row(row: Dict[str, Any]) -> Live2dTtsRefer:
        return Live2dTtsRefer(
            refer_id=row["refer_id"],
            user_id=int(row["user_id"]),
            package_key=row["package_key"],
            audio_object_key=row.get("audio_object_key"),
            audio_url=row.get("audio_url"),
            audio_format=row.get("audio_format"),
            prompt_text=row.get("prompt_text") or "",
            prompt_language=row.get("prompt_language") or "zh",
            create_time=_parse_dt(row.get("create_time")),
            update_time=_parse_dt(row.get("update_time")),
        )
