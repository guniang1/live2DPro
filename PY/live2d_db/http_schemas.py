"""HTTP 请求/响应模型（与 ORM 实体分离，用户相关响应不含 password）。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ----- user -----
class UserCreate(BaseModel):
    username: str = Field(..., max_length=50)
    password: str = Field(..., max_length=100)
    nickname: Optional[str] = Field(None, max_length=50)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=50)
    status: int = 1


class UserUpdate(BaseModel):
    username: Optional[str] = Field(None, max_length=50)
    password: Optional[str] = Field(None, max_length=100)
    nickname: Optional[str] = Field(None, max_length=50)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=50)
    status: Optional[int] = None


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: int
    username: str
    nickname: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    status: int


# ----- chat_session -----
class ChatSessionCreate(BaseModel):
    user_id: int
    package_key: str = Field(..., max_length=64)
    user_input: str
    ai_reply: str
    emotion_tag: Optional[str] = Field(None, max_length=30)
    session_key: str = Field(..., max_length=64)


class ChatSessionUpdate(BaseModel):
    user_id: int
    package_key: str = Field(..., max_length=64)
    user_input: str
    ai_reply: str
    emotion_tag: Optional[str] = Field(None, max_length=30)
    session_key: str = Field(..., max_length=64)


class ChatSessionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: int
    user_id: int
    package_key: str
    user_input: str
    ai_reply: str
    emotion_tag: Optional[str] = None
    session_key: str
    create_time: Optional[datetime] = None


# ----- long_memory -----
class LongMemoryCreate(BaseModel):
    user_id: int
    package_key: str = Field("default", max_length=64)
    memory_type: str = Field("long", max_length=20)
    period_overview: str = ""


class LongMemoryUpdate(BaseModel):
    package_key: Optional[str] = Field(None, max_length=64)
    memory_type: Optional[str] = Field(None, max_length=20)
    period_overview: Optional[str] = None
    last_consolidate_time: Optional[datetime] = None


class LongMemoryPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    memory_id: int
    user_id: int
    package_key: str
    memory_type: str
    period_overview: str = ""
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    last_consolidate_time: Optional[datetime] = None


class LongMemoryConsolidateNowPublic(BaseModel):
    """手动触发周期概要更新后的应答（不走后台最短间隔限制）。"""

    ok: bool = True
    updated: bool = Field(
        ...,
        description="是否成功写入或更新了 period_overview（无可用对话或摘要为空时为 false）",
    )


# ----- persona -----
class PersonaCreate(BaseModel):
    character_desc: str
    tone_style: str = Field(..., max_length=50)
    default_emotion: Optional[str] = Field(None, max_length=20)
    status: int = 1


class PersonaUpdate(BaseModel):
    character_desc: str
    tone_style: str = Field(..., max_length=50)
    default_emotion: Optional[str] = Field(None, max_length=20)
    status: int


class PersonaPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    persona_id: Optional[int] = None
    character_desc: str = ""
    tone_style: str = ""
    default_emotion: Optional[str] = None
    create_time: Optional[datetime] = None
    status: int = 1
    user_id: Optional[int] = None
    package_key: Optional[str] = None


class PersonaPackageUpsert(BaseModel):
    """按模型包 upsert 人设：``character_desc``、``tone_style`` 写入 persona，并参与聊天 system 拼接。"""

    character_desc: str = Field(default="", max_length=65535)
    tone_style: str = Field(
        ...,
        description="语气风格（入库不超 50 字）；可与 expand_with_llm 联用，把本字段当作简短关键词（最长 200 字）",
    )
    expand_with_llm: bool = Field(
        default=False,
        description="为 true 时以 character_desc、tone_style 为提示词经 LLM 扩写后再写入（需 Ollama）",
    )

    @field_validator("tone_style")
    @classmethod
    def _tone_strip_nonempty(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("tone_style 不能为空")
        return s

    @model_validator(mode="after")
    def _tone_length_by_expand_mode(self) -> PersonaPackageUpsert:
        cap = 200 if self.expand_with_llm else 50
        if len(self.tone_style) > cap:
            if self.expand_with_llm:
                raise ValueError("expand_with_llm 模式下语气关键词最长 200 字符")
            raise ValueError("tone_style 最长 50 字符；开启 expand_with_llm 可传入更长语气关键词")
        return self


class PersonaExpandHintsBody(BaseModel):
    """仅扩写预览：不参与入库。"""

    character_hint: str = Field(default="", max_length=65535)
    tone_hint: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _hints_at_least_one(self) -> PersonaExpandHintsBody:
        if not (self.character_hint or "").strip() and not (self.tone_hint or "").strip():
            raise ValueError("character_hint 与 tone_hint 至少填写其一")
        return self


class PersonaExpandHintsResponse(BaseModel):
    character_desc: str
    tone_style: str


# ----- user_profile -----
class UserProfileUpsert(BaseModel):
    user_tags: Optional[str] = Field(None, max_length=255)
    emotion_state: Optional[str] = Field(None, max_length=30)
    preferences: Optional[str] = None
    trouble_events: Optional[str] = None


class UserProfilePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    profile_id: int
    user_id: int
    user_tags: Optional[str] = None
    emotion_state: Optional[str] = None
    preferences: Optional[str] = None
    trouble_events: Optional[str] = None
    update_time: Optional[datetime] = None


# ----- remind_trigger -----
class RemindTriggerCreate(BaseModel):
    user_id: int
    trigger_type: str = Field(..., max_length=30)
    trigger_time: datetime
    session_id: Optional[int] = Field(
        None,
        description="可选：绑定 chat_session.session_id；投递时用该轮对话作为语境（对话抽取会自动写入）",
    )
    trigger_content: str = Field(
        ...,
        description="情景概要（抽取时以 Redis 瞬时+短期记忆等为主料，含用户时间/地点/角色/事件/氛围）；投递时由 LLM 结合本字段与瞬时多轮对话等当场重写最终话术",
    )
    is_triggered: int = 0


class RemindTriggerUpdate(BaseModel):
    user_id: int
    trigger_type: str = Field(..., max_length=30)
    trigger_time: datetime
    session_id: Optional[int] = None
    trigger_content: str = Field(
        ...,
        description="情景概要（库内保存）；与 RemindTriggerCreate 一致",
    )
    is_triggered: int


class RemindTriggerPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trigger_id: int
    user_id: int
    trigger_type: str
    trigger_time: Optional[datetime] = None
    session_id: Optional[int] = None
    trigger_content: str = Field(
        ...,
        description="情景概要（与 MySQL 列一致）；REST 与 WebSocket remind_trigger 帧中本字段语义相同",
    )
    delivery_message: Optional[str] = Field(
        None,
        description="仅 WebSocket 投递帧给出：当场生成的面向用户台词；REST 响应中为 null",
    )
    is_triggered: int
    create_time: Optional[datetime] = None


class RemindSchedulerScanNowPublic(BaseModel):
    """手动触发一轮 remind_trigger 扫描后的应答。"""

    ok: bool = True
    pending_fetched: int = 0
    claimed: int = 0
    delivered: int = 0
    released_no_ws: int = 0
    stale_reclaimed: int = 0


class OkRows(BaseModel):
    affected_rows: int


class CountResponse(BaseModel):
    """列表查询配套的总条数（与对应筛选条件一致）。"""

    total: int


# ----- live2d_model_asset（Resources 下模型包文件索引） -----
class Live2dModelAssetCreate(BaseModel):
    user_id: int = Field(..., ge=1, description="关联 user.user_id，外键")
    package_key: str = Field(..., max_length=64, description="目录名，如 Xiaozi")
    relative_path: str = Field(..., max_length=512, description="包内相对路径，正斜杠")
    file_name: str = Field(..., max_length=255)
    asset_type: str = Field(
        ...,
        max_length=32,
        description="model3/motion3/exp3/physics3/cdi3/vtube/json_other",
    )
    public_url: str = Field(..., max_length=768, description="如 /Resources/Xiaozi/motions/xx.motion3.json")
    object_key: Optional[str] = Field(None, max_length=768, description="对象存储 key")
    mime_type: Optional[str] = Field(None, max_length=64)
    logical_name: Optional[str] = Field(None, max_length=128, description="逻辑名：Expressions[].Name 等")
    motion_group: Optional[str] = Field(None, max_length=64, description="动作组：Idle/TapBody 等")
    is_listed_in_model3: int = Field(0, ge=0, le=1)
    is_entry_model: int = Field(0, ge=0, le=1)
    file_size: Optional[int] = Field(None, ge=0)
    sort_order: int = 0
    remark: Optional[str] = Field(None, max_length=255)


class Live2dModelAssetUpdate(BaseModel):
    user_id: int = Field(..., ge=1)
    package_key: str = Field(..., max_length=64)
    relative_path: str = Field(..., max_length=512)
    file_name: str = Field(..., max_length=255)
    asset_type: str = Field(..., max_length=32)
    public_url: str = Field(..., max_length=768)
    object_key: Optional[str] = Field(None, max_length=768)
    mime_type: Optional[str] = Field(None, max_length=64)
    logical_name: Optional[str] = Field(None, max_length=128)
    motion_group: Optional[str] = Field(None, max_length=64)
    is_listed_in_model3: int = Field(0, ge=0, le=1)
    is_entry_model: int = Field(0, ge=0, le=1)
    file_size: Optional[int] = Field(None, ge=0)
    sort_order: int = 0
    remark: Optional[str] = Field(None, max_length=255)


class Live2dModelAssetPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    asset_id: int
    user_id: int
    package_key: str
    relative_path: str
    file_name: str
    asset_type: str
    public_url: str
    object_key: Optional[str] = None
    mime_type: Optional[str] = None
    logical_name: Optional[str] = None
    motion_group: Optional[str] = None
    is_listed_in_model3: int
    is_entry_model: int
    file_size: Optional[int] = None
    sort_order: int
    remark: Optional[str] = None
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None


class Live2dModelZipUploadPublic(BaseModel):
    """上传并导入模型压缩包后的摘要结果。"""

    user_id: int
    package_key: str
    bucket: str
    object_prefix: str
    deleted_rows: int
    inserted_rows: int
    uploaded_files: int
    skipped_files: int


class DownloadUrlPublic(BaseModel):
    url: str
    expires_in: int


class BackgroundImagePublic(BaseModel):
    """背景图元数据；``url`` 在 ``presign=true`` 时为临时访问链接。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str
    presigned_expires_in: int = Field(
        0,
        ge=0,
        description="预签名有效期（秒）；0 表示未使用预签名（直链或推导 object_key 失败）",
    )


class Live2dModelPackageInfo(BaseModel):
    """模型包信息摘要。"""
    package_key: str
    file_count: int
    asset_types: list[str]
    has_entry_model: bool
    has_tts_refer: bool = False


class Live2dTtsReferPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    refer_id: int
    user_id: int
    package_key: str
    audio_object_key: Optional[str] = None
    audio_url: Optional[str] = None
    audio_format: Optional[str] = None
    prompt_text: str
    prompt_language: str
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None


class Live2dTtsReferUploadPublic(BaseModel):
    user_id: int
    package_key: str
    bucket: str
    object_key: str
    audio_url: str
    audio_format: str
    prompt_text: str
    prompt_language: str