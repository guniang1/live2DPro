"""数据库表对应的 Python 实体（dataclass）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Optional


class RemindDeliverOutcome(IntEnum):
    """定时关怀 WebSocket 投递结果（用于决定是否将 is_triggered 置为 1）。"""

    JSON_SENT = 1  # 已成功下发 remind_trigger JSON（含非空 delivery_message）
    TRANSPORT_FAILED = 2  # 无在线连接或 JSON 发送失败
    SKIPPED_NO_PAYLOAD = 3  # 未下发 JSON（类型为空或正文为空等）；占用应从 2 释放回 0，不得标为已触发


@dataclass
class User:
    user_id: Optional[int] = None
    username: str = ""
    password: str = ""
    nickname: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    status: int = 1


@dataclass
class ChatSession:
    session_id: Optional[int] = None
    user_id: int = 0
    package_key: str = ""
    user_input: str = ""
    ai_reply: str = ""
    emotion_tag: Optional[str] = None
    session_key: str = ""
    create_time: Optional[datetime] = None


@dataclass
class LongMemory:
    memory_id: Optional[int] = None
    user_id: int = 0
    package_key: str = "default"
    memory_type: str = "long"
    period_overview: str = ""
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    last_consolidate_time: Optional[datetime] = None


@dataclass
class Persona:
    persona_id: Optional[int] = None
    character_desc: str = ""
    tone_style: str = ""
    default_emotion: Optional[str] = None
    user_id: Optional[int] = None
    package_key: Optional[str] = None
    create_time: Optional[datetime] = None
    status: int = 1


@dataclass
class UserProfile:
    profile_id: Optional[int] = None
    user_id: int = 0
    user_tags: Optional[str] = None
    emotion_state: Optional[str] = None
    preferences: Optional[str] = None
    trouble_events: Optional[str] = None
    update_time: Optional[datetime] = None


@dataclass
class RemindTrigger:
    trigger_id: Optional[int] = None
    user_id: int = 0
    trigger_type: str = ""
    trigger_time: Optional[datetime] = None
    session_id: Optional[int] = None
    trigger_content: str = ""
    is_triggered: int = 0
    delivery_started_at: Optional[datetime] = None
    create_time: Optional[datetime] = None


@dataclass
class BackgroundImage:
    """表 ``background_image``：Demo 背景图（MinIO URL 索引）。"""

    id: Optional[int] = None
    name: str = ""
    url: str = ""
    create_time: Optional[datetime] = None


@dataclass
class Live2dModelAsset:
    """Demo 下 Resources/<package_key> 中单个文件的索引（模型/动作/表情等）。"""

    asset_id: Optional[int] = None
    user_id: int = 0
    package_key: str = ""
    relative_path: str = ""
    file_name: str = ""
    asset_type: str = ""
    public_url: str = ""
    object_key: Optional[str] = None
    mime_type: Optional[str] = None
    logical_name: Optional[str] = None
    motion_group: Optional[str] = None
    is_listed_in_model3: int = 0
    is_entry_model: int = 0
    file_size: Optional[int] = None
    sort_order: int = 0
    remark: Optional[str] = None
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None


@dataclass
class Live2dTtsRefer:
    """模型包级 GPT-SoVITS 参考音频绑定（user_id + package_key 唯一）。"""

    refer_id: Optional[int] = None
    user_id: int = 0
    package_key: str = ""
    audio_object_key: Optional[str] = None
    audio_url: Optional[str] = None
    audio_format: Optional[str] = None
    prompt_text: str = ""
    prompt_language: str = "zh"
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None

