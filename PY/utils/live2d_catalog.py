"""
进程内按「user_id + package_key」缓存 Live2dCatalog；WebSocket 通过 query ?package= + ?user_id 取候选。

数据来源（命中顺序）：
1. 进程内 dict ``_catalog_by_package``
2. **Redis** 字符串 JSON（键前缀默认 ``live2d:catalog``，TTL 默认 7 天，见环境变量）
3. **MySQL** 表 ``live2d_model_asset``（成功后回写 Redis）

表情：asset_type=exp3 或 relative_path 后缀 .exp3.json
动作：asset_type=motion3 或 relative_path 后缀 .motion3.json

本地 Resources 目录不再扫描；``resources_root`` 字段仅为兼容保留。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import logging
from live2d_db.connection import connection_ctx
from live2d_db.db_config import DbConfig
from live2d_db.package_key_util import normalize_package_key
from live2d_db.repositories import Live2dModelAssetRepository

logger = logging.getLogger(__name__)

# 提示词示例用语；小模型常会原样输出，需在规范化阶段视为「未选择」以免误判为合法 ID。
_PLACEHOLDER_EXPRESSION_PICKS = frozenset({"某表情标识名"})
_PLACEHOLDER_MOTION_PICKS = frozenset({"某动作标识名"})


@dataclass(frozen=True)
class Live2dCatalog:
    """单次扫描结果。"""

    package_key: str
    resources_root: Path
    # 相对 package 根目录的路径，便于前端与模型约定一致
    expression_paths: list[str]
    motion_paths: list[str]
    llm_context_text: str

    def ws_catalog_message(self) -> dict:
        """连接建立后发送一次：可用表情/动作列表（与 LLM 系统提示一致）。"""
        return {
            "type": "catalog",
            "package_key": self.package_key,
            "expression": self.expression_names,
            "motion": self.motion_names,
            "expression_paths": list(self.expression_paths),
            "motion_paths": list(self.motion_paths),
        }

    @property
    def expression_names(self) -> list[str]:
        # 去掉「.exp3.json」，与 Cubism 文件名规则一致；勿用 Path.stem（会得到 xxx.exp3）
        return [_expression_id_from_rel(p) for p in self.expression_paths]

    @property
    def motion_names(self) -> list[str]:
        return [_motion_id_from_rel(p) for p in self.motion_paths]

    @property
    def action_llm_system_text(self) -> str:
        """专用于「动作/表情决策」模型（与聊天模型分离），要求只输出 JSON。"""
        exn, mon = self.expression_names, self.motion_names
        if exn and mon:
            sample_line = json.dumps(
                {
                    "reason": "……",
                    "expression": exn[0],
                    "motion": mon[0],
                },
                ensure_ascii=False,
            )
            sample_block = (
                "JSON 键须包含 **reason、expression、motion**。\n"
                "下方示例仅演示**合法字段名与 JSON 形状**；其中 expression、motion 各取自本包真实标识名一条。"
                "**禁止**输出「某表情标识名」「某动作标识名」等占位词，也**禁止**照抄示例里具体的表情/动作（须按对话情境为**虚拟角色**另选上表中的项）。\n"
                f"形状示例：\n{sample_line}"
            )
        else:
            sample_block = (
                "JSON 须含 **reason、expression、motion**；"
                "expression / motion 必须为上表中存在的标识名，禁止输出「某表情标识名」「某动作标识名」。\n"
                '{"reason":"……","expression":"<从【表情】表复制标识名>","motion":"<从【动作】表复制标识名>"}'
            )
        return (
            self.llm_context_text
            + "\n\n你的任务：综合消息里的「人设参考」「最近对话（若有）」与「本轮用户输入」，判断 **Live2D 虚拟角色（助手）** "
            "在当下对话中应表现出的神态与肢体，从上述标识名中为该**角色**的**表情、动作各选一个**。\n"
            "**选题对象**：expression / motion **只描述角色自身的演绎资源**，表现角色对用户话语与会谈氛围的反应；"
            "**绝不**表示真人用户此刻的表情或动作，也不要把「用户在做的事」直接映射成角色的 motion。\n"
            "**硬性规则**：键 **expression** 与 **motion** 的值都必须是各自表中存在的标识名字符串；"
            "**禁止**只填一侧、另一侧写空字符串 \"\"；也**禁止**用「与表情无关」「只选动作」等理由故意留空 expression。"
            "若对话内容与某一侧较难对应，仍须为该**角色**从表中选一个**中性、通用**的项（例如偏日常基础脸型类表情 + 待机/轻量类动作），不得留空，不得选择身体部位（头不是表情）。\n"
            "重要：expression **只能**来自【表情】表；motion **只能**来自【动作】表；不要把动作路径/文件名写进 expression，"
            "不要把表情名写进 motion；motion 只写标识名（与表中一致），不要写带 motions/ 的路径。\n"
            "只输出**一个** JSON 对象（不要 markdown 代码块、不要 JSON 外的文字）。\n"
            + sample_block
        )


def _expression_id_from_rel(rel: str) -> str:
    """例如 expressions/1发饰1.exp3.json → 1发饰1"""
    name = Path(rel).name
    lower = name.lower()
    if lower.endswith(".exp3.json"):
        return name[: -len(".exp3.json")]
    return Path(rel).stem


def _motion_id_from_rel(rel: str) -> str:
    """例如 motions/待机动画.motion3.json → 待机动画"""
    name = Path(rel).name
    lower = name.lower()
    if lower.endswith(".motion3.json"):
        return name[: -len(".motion3.json")]
    return Path(rel).stem


def normalize_expression_pick(val: str | None) -> str:
    """将动作 LLM 可能带上的 .exp3 / .exp3.json 等后缀去掉，便于与 expression_names 比对。"""
    if val is None:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    lower = s.lower()
    if lower.endswith(".exp3.json"):
        s = s[: -len(".exp3.json")]
    elif lower.endswith(".exp3"):
        s = s[: -len(".exp3")]
    if s in _PLACEHOLDER_EXPRESSION_PICKS:
        return ""
    return s


def normalize_motion_pick(val: str | None) -> str:
    """将动作 LLM 可能带上的 .motion3 / .motion3.json 去掉。"""
    if val is None:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    lower = s.lower()
    if lower.endswith(".motion3.json"):
        s = s[: -len(".motion3.json")]
    elif lower.endswith(".motion3"):
        s = s[: -len(".motion3")]
    if s in _PLACEHOLDER_MOTION_PICKS:
        return ""
    return s


def resolve_expression_id(normalized: str, allowed: frozenset[str]) -> str:
    """精确匹配；否则在唯一命中时用「标识名包含模型输出」或「模型输出包含标识名」消歧。"""
    if not normalized:
        return ""
    if normalized in allowed:
        return normalized
    inner = [a for a in allowed if normalized in a]
    if len(inner) == 1:
        return inner[0]
    if len(normalized) >= 2:
        outer = [a for a in allowed if a in normalized]
        if len(outer) == 1:
            return outer[0]
    return ""


def resolve_motion_id(normalized: str, allowed: frozenset[str]) -> str:
    if not normalized:
        return ""
    if normalized in allowed:
        return normalized
    inner = [a for a in allowed if normalized in a]
    if len(inner) == 1:
        return inner[0]
    if len(normalized) >= 2:
        outer = [a for a in allowed if a in normalized]
        if len(outer) == 1:
            return outer[0]
    return ""


def default_resources_root() -> Path:
    # 保留字段兼容；当前 catalog 不再从本地目录扫描资源。
    return Path(__file__).resolve().parents[2] / "Demo" / "public" / "Resources"


def _scan_package_from_mysql(user_id: int, package_key: str) -> tuple[list[str], list[str]]:
    """仅从 MySQL live2d_model_asset 读取指定用户+模型包的表情/动作路径。"""
    with connection_ctx(DbConfig.from_env()) as conn:
        rows = Live2dModelAssetRepository.list_by_package(
            conn,
            user_id=user_id,
            package_key=package_key,
            limit=5000,
            offset=0,
        )
    expressions: list[str] = []
    motions: list[str] = []
    for r in rows:
        rel = (r.relative_path or "").strip().lstrip("/")
        if not rel:
            continue
        rel_low = rel.lower()
        typ = (r.asset_type or "").strip().lower()
        if typ == "exp3" or rel_low.endswith(".exp3.json"):
            expressions.append(rel)
        elif typ == "motion3" or rel_low.endswith(".motion3.json"):
            motions.append(rel)
    return sorted(set(expressions)), sorted(set(motions))


def _build_llm_text(
    package_key: str,
    expression_paths: list[str],
    motion_paths: list[str],
) -> str:
    lines: list[str] = [
        f"当前 Live2D 模型包为「{package_key}」。以下为该包内可引用的表情与动作资源（名称不含扩展名，路径为包内相对路径）。",
        "",
        "【表情】对应文件后缀 .exp3.json：",
    ]
    if not expression_paths:
        lines.append("（无）")
    else:
        for rel in expression_paths:
            lines.append(f"- {rel} → 标识名: {_expression_id_from_rel(rel)}")

    lines.extend(["", "【动作】对应文件后缀 .motion3.json："])
    if not motion_paths:
        lines.append("（无）")
    else:
        for rel in motion_paths:
            lines.append(f"- {rel} → 标识名: {_motion_id_from_rel(rel)}")

    lines.extend(
        [
            "",
            "说明：expression 与 motion 的值必须各为上表中某一行的「标识名」；不得编造表中不存在的名称，也不得留空（须从表中择一）。",
        ]
    )
    return "\n".join(lines)


def build_catalog(
    user_id: int,
    package_key: str,
    *,
    resources_root: Path | None = None,
) -> Live2dCatalog:
    base = resources_root if resources_root is not None else default_resources_root()
    expr_paths, mot_paths = _scan_package_from_mysql(user_id, package_key)
    text = _build_llm_text(package_key, expr_paths, mot_paths)
    return Live2dCatalog(
        package_key=package_key,
        resources_root=base.resolve(),
        expression_paths=expr_paths,
        motion_paths=mot_paths,
        llm_context_text=text,
    )


# 按 (user_id, package_key) 缓存，避免重复查库
_catalog_by_package: dict[tuple[int, str], Live2dCatalog] = {}


def _catalog_redis_enabled() -> bool:
    return (os.getenv("LIVE2D_CATALOG_REDIS_ENABLED") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _catalog_redis_ttl_seconds() -> int:
    try:
        n = int((os.getenv("LIVE2D_CATALOG_REDIS_TTL_SECONDS") or "604800").strip() or "604800")
    except ValueError:
        return 604800
    return max(300, min(86400 * 30, n))


def _catalog_redis_key(user_id: int, package_key: str) -> str:
    pk = normalize_package_key(package_key, fallback="default")
    pfx = (os.getenv("LIVE2D_CATALOG_REDIS_PREFIX") or "live2d:catalog").strip() or "live2d:catalog"
    return f"{pfx}:{user_id}:{pk}"


def _try_load_catalog_from_redis(
    redis_cli: Any,
    user_id: int,
    package_key: str,
    resources_root: Path,
) -> Live2dCatalog | None:
    try:
        blob = redis_cli.get(_catalog_redis_key(user_id, package_key))
        if not blob:
            return None
        data = json.loads(blob)
        expr = data.get("expression_paths")
        mot = data.get("motion_paths")
        if not isinstance(expr, list) or not isinstance(mot, list):
            return None
        expr_paths = sorted({str(x).strip().lstrip("/") for x in expr if str(x).strip()})
        mot_paths = sorted({str(x).strip().lstrip("/") for x in mot if str(x).strip()})
        text = _build_llm_text(package_key, expr_paths, mot_paths)
        return Live2dCatalog(
            package_key=package_key,
            resources_root=resources_root.resolve(),
            expression_paths=expr_paths,
            motion_paths=mot_paths,
            llm_context_text=text,
        )
    except json.JSONDecodeError:
        logger.warning(
            "Live2D catalog Redis JSON 无效，将回退 MySQL user_id=%s package=%s",
            user_id,
            package_key,
        )
        return None
    except Exception:
        logger.exception(
            "Live2D catalog Redis 读取异常，将回退 MySQL user_id=%s package=%s",
            user_id,
            package_key,
        )
        return None


def _write_catalog_to_redis(redis_cli: Any, user_id: int, package_key: str, cat: Live2dCatalog) -> None:
    try:
        payload = json.dumps(
            {"expression_paths": cat.expression_paths, "motion_paths": cat.motion_paths},
            ensure_ascii=False,
        )
        redis_cli.setex(
            _catalog_redis_key(user_id, package_key),
            _catalog_redis_ttl_seconds(),
            payload,
        )
    except Exception:
        logger.exception("Live2D catalog 写 Redis 失败 user_id=%s package=%s", user_id, package_key)


def invalidate_live2d_catalog_cache(user_id: int, package_key: str) -> None:
    """在 ``live2d_model_asset`` 变更后调用：清理进程内缓存并删除 Redis catalog 键。"""
    pk_norm = normalize_package_key(package_key, fallback="default")
    for ck in list(_catalog_by_package.keys()):
        if ck[0] != user_id:
            continue
        if normalize_package_key(ck[1], fallback="default") != pk_norm:
            continue
        _catalog_by_package.pop(ck, None)
    if not _catalog_redis_enabled():
        return
    from live2d_db.redis_factory import get_redis_client

    rc = get_redis_client(logger)
    if rc is None:
        return
    try:
        rc.delete(_catalog_redis_key(user_id, package_key))
    except Exception:
        logger.exception("Live2D catalog 删除 Redis 失败 user_id=%s package=%s", user_id, package_key)


def get_catalog_for_package(
    package_key: str | None,
    *,
    user_id: int | None = None,
    resources_root: Path | None = None,
) -> Live2dCatalog:
    """
    返回指定模型包下的表情/动作索引（懒加载并缓存）。
    package_key：逻辑模型包键（如 Xiaozi、Xiaogou）。
    user_id：资源归属用户（live2d_model_asset.user_id）。
    """
    uid_raw = user_id if user_id is not None else int(os.getenv("LIVE2D_DEFAULT_USER_ID", "1"))
    uid = max(1, int(uid_raw))
    key = (package_key or "").strip() or os.getenv("LIVE2D_PACKAGE", "Xiaozi")
    cache_key = (uid, key)
    if cache_key in _catalog_by_package:
        return _catalog_by_package[cache_key]

    root_env = os.getenv("LIVE2D_RESOURCES_ROOT")
    root: Path | None = resources_root
    if root is None and root_env:
        root = Path(root_env)
    base_root = (root or default_resources_root()).resolve()

    rc = None
    if _catalog_redis_enabled():
        from live2d_db.redis_factory import get_redis_client

        rc = get_redis_client(logger)
        if rc is not None:
            redis_cat = _try_load_catalog_from_redis(rc, uid, key, base_root)
            if redis_cat is not None:
                logger.info(
                    "Live2D 资源已从 Redis 加载: user_id=%s package=%s expressions=%d motions=%d",
                    uid,
                    key,
                    len(redis_cat.expression_paths),
                    len(redis_cat.motion_paths),
                )
                _catalog_by_package[cache_key] = redis_cat
                return redis_cat

    try:
        cat = build_catalog(uid, key, resources_root=root)
        logger.info(
            "Live2D 资源已从 MySQL 加载: user_id=%s package=%s expressions=%d motions=%d db=%s",
            uid,
            key,
            len(cat.expression_paths),
            len(cat.motion_paths),
            DbConfig.from_env().database,
        )
        if rc is not None:
            _write_catalog_to_redis(rc, uid, key, cat)
    except Exception as e:
        logger.warning("Live2D 资源加载失败 user_id=%s package=%s: %s", uid, key, e)
        cat = Live2dCatalog(
            package_key=key,
            resources_root=base_root,
            expression_paths=[],
            motion_paths=[],
            llm_context_text=(
                f"（MySQL 中未找到 user_id={uid}、package={key} 的表情/动作索引。）"
            ),
        )
    _catalog_by_package[cache_key] = cat
    return cat


def init_catalog(
    package_key: str | None = None,
    *,
    user_id: int | None = None,
    resources_root: Path | None = None,
) -> Live2dCatalog:
    """在 FastAPI lifespan 中调用：预热默认用户+默认包。"""
    key = package_key or os.getenv("LIVE2D_PACKAGE", "Xiaozi")
    uid = user_id if user_id is not None else int(os.getenv("LIVE2D_DEFAULT_USER_ID", "1"))
    return get_catalog_for_package(key, user_id=uid, resources_root=resources_root)


def get_catalog() -> Live2dCatalog:
    """兼容旧代码：等价于默认 user + 默认 package。"""
    return get_catalog_for_package(
        os.getenv("LIVE2D_PACKAGE", "Xiaozi"),
        user_id=int(os.getenv("LIVE2D_DEFAULT_USER_ID", "1")),
    )
