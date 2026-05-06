"""模型包键规范化：与对象存储路径、MySQL、Redis 键一致。

允许 ASCII 字母数字、下划线、连字符，以及常用汉字（CJK 统一表意文字 U+4E00–U+9FFF）。
纯 ASCII 的旧逻辑会把「懒羊羊3」中非 ASCII 段替换为 `_`，再经 strip 后变成 ``3``。
"""

from __future__ import annotations

import re
from typing import Optional

_PACKAGE_KEY_INVALID_SEQ = re.compile(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+")


def normalize_package_key(raw: Optional[str], fallback: str = "uploaded") -> str:
    base = (raw or "").strip()
    if not base:
        base = fallback
    normalized = _PACKAGE_KEY_INVALID_SEQ.sub("_", base).strip("._-")
    if not normalized:
        normalized = fallback
    return normalized[:64]
