"""长期记忆语义维度与库表文本列。

一行 ``long_memory`` 对应一个模型角色 (user_id + package_key)；Redis 缓存合并后的 prompt 片段。
摘要依据来自表 ``chat_session``。

**产品约定**：LLM **滚动合并** ``period_overview``（条目列表整体替换）；入库正文每条为 **【时间】人物、事件；**，时间为 **YYYY年M月D日上午/下午**（由 ``chat_session.create_time`` 标注）；稳定用户信息见 ``user_profile``。
注入聊天 system 时仅 ``LONG_MEMORY_DIMENSIONS``（周期概要，超长保留文尾）。
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
    body = "\n\n".join(parts).strip()
    if not body:
        return ""
    return (
        "以下为近期对话脉络（称呼、长期偏好等稳定信息见用户画像，此处不重复）：\n"
        + body
    )


def long_memory_has_any_content(record: Any) -> bool:
    return bool(merge_long_memory_record_for_prompt(record))


def blank_long_memory_fields_dict() -> dict[str, str]:
    return {k: "" for k in LONG_MEMORY_DB_TEXT_COLUMNS}
