"""长期记忆语义维度与库表文本列。

一行 ``long_memory`` 对应一个模型角色 (user_id + package_key)；Redis 缓存合并后的 prompt 片段。
摘要依据来自表 ``chat_session``。

**产品约定**：LLM 摘要环节 **只维护** ``period_overview``（新摘要追加到原有内容之后）。
注入聊天 system 时仅 ``LONG_MEMORY_DIMENSIONS``（周期概要）。
"""

from __future__ import annotations

from typing import Any, Iterator

# 合并进模型 system 的维度（仅周期概要；与 LLM 摘要输出 JSON 键一致）
LONG_MEMORY_DIMENSIONS: tuple[tuple[str, str], ...] = (("period_overview", "周期概要"),)

LONG_MEMORY_JSON_KEYS: tuple[str, ...] = tuple(k for k, _ in LONG_MEMORY_DIMENSIONS)

# 表 long_memory 中文本列（与当前 schema 一致；仅 period_overview）
LONG_MEMORY_DB_TEXT_COLUMNS: tuple[str, ...] = ("period_overview",)


def iter_long_memory_field_values(record: Any) -> Iterator[tuple[str, str]]:
    """从实体或 dict 读取各维度非空前的原始字符串。"""
    for attr, _label in LONG_MEMORY_DIMENSIONS:
        if isinstance(record, dict):
            raw = record.get(attr)
        else:
            raw = getattr(record, attr, None)
        yield attr, (str(raw).strip() if raw is not None else "")


def merge_long_memory_record_for_prompt(record: Any) -> str:
    """将多字段合并为一段 system 正文（仅包含有内容的维度）。"""
    parts: list[str] = []
    for attr, label in LONG_MEMORY_DIMENSIONS:
        if isinstance(record, dict):
            raw = record.get(attr)
        else:
            raw = getattr(record, attr, None)
        text = (str(raw).strip() if raw is not None else "")
        if text:
            parts.append(f"【{label}】\n{text}")
    return "\n\n".join(parts).strip()


def long_memory_has_any_content(record: Any) -> bool:
    return bool(merge_long_memory_record_for_prompt(record))


def blank_long_memory_fields_dict() -> dict[str, str]:
    return {k: "" for k in LONG_MEMORY_DB_TEXT_COLUMNS}
