"""
Live2D 数字人业务库 HTTP 接口（无鉴权，生产环境请加认证与限流）。

推荐暴露范围（核心业务表全开，与毕业设计文档一致）：
- user：注册与资料维护（响应永不返回 password）
- chat_session：对话落库与按用户查询
- long_memory：长期记忆 CRUD
- persona：人设列表与后台维护
- user_profile：画像（按 user  upsert）
- remind_trigger：主动关怀/待办
- live2d_model_asset：Demo 下 Resources/<包名> 内模型/动作/表情等文件索引
- background_image：背景图索引（MinIO URL）；GET /background-images/random 每次随机一行
- persona：人设（全局模板；user_id+package_key 非空时为某用户某模型包专属，character_desc 写入聊天 system）；支持 ``expand_with_llm`` / ``POST /personas/expand-from-hints`` 由 Ollama 据简短关键词扩写角色与语气
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import mimetypes
import os
import re
import zipfile
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Dict, List, Optional, Tuple

import pymysql
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from utils.audio_refer import ffmpeg_convert_bytes_to_wav, is_standard_riff_wav
from utils.live2d_catalog import invalidate_live2d_catalog_cache
from utils.persona_expand import PersonaExpandError, expand_persona_from_hints

from .connection import connection_ctx
from .deps import get_db
from .package_key_util import normalize_package_key as _normalize_package_key
from .package_key_util import _PACKAGE_KEY_INVALID_SEQ as _PACKAGE_KEY_PATTERN
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
from .http_schemas import (
    ChatSessionCreate,
    BackgroundImagePublic,
    ChatSessionPublic,
    ChatSessionUpdate,
    Live2dModelAssetCreate,
    Live2dModelAssetPublic,
    Live2dModelAssetUpdate,
    Live2dModelPackageInfo,
    Live2dModelZipUploadPublic,
    Live2dTtsReferPublic,
    Live2dTtsReferUploadPublic,
    LongMemoryConsolidateNowPublic,
    LongMemoryCreate,
    LongMemoryPublic,
    LongMemoryUpdate,
    CountResponse,
    DownloadUrlPublic,
    OkRows,
    PersonaCreate,
    PersonaExpandHintsBody,
    PersonaExpandHintsResponse,
    PersonaPackageUpsert,
    PersonaPublic,
    PersonaUpdate,
    RemindTriggerCreate,
    RemindTriggerPublic,
    RemindSchedulerScanNowPublic,
    RemindTriggerUpdate,
    UserCreate,
    UserProfilePublic,
    UserProfileUpsert,
    UserPublic,
    UserUpdate,
)
from .long_memory_fields import (
    long_memory_has_any_content,
    merge_long_memory_record_for_prompt,
)
from .minio_redis_cache import presigned_get_url_cached
from .minio_storage import get_bucket_name, get_public_base, upload_bytes
from .repositories import (
    BackgroundImageRepository,
    ChatSessionRepository,
    Live2dModelAssetRepository,
    Live2dTtsReferRepository,
    LongMemoryRepository,
    PersonaRepository,
    RemindTriggerRepository,
    UserProfileRepository,
    UserRepository,
)
from .scan_package import infer_asset_type
from . import memory_layers as _memory_layers
from .redis_factory import get_redis_client as _redis_factory_get_client


# UnifiedResponseRoute 继承了 APIRoute
class UnifiedResponseRoute(APIRoute):
    """将本路由下的 JSON 响应统一封装为 code/message/data。"""

    def get_route_handler(self):
        # super() 就是“去调用父类 APIRoute 的同名方法”，“请 APIRoute 先给我一个原版路由处理函数”
        # 所以 original_route_handler 就是 APIRoute 的原版路由处理函数
        original_route_handler = super().get_route_handler()
        
        # ==========自定义路由处理函数，用于封装 JSON 响应==============
        async def custom_route_handler(request: Request) -> Response:
            # 先执行原始处理逻辑，拿到标准 Response 对象
            response = await original_route_handler(request)
            content_type = (response.headers.get("content-type") or "").lower()
            # 仅封装 JSON；文件流、HTML、二进制等响应保持原样，避免破坏非 JSON 协议
            if "application/json" not in content_type:
                print(
                    f"[api-response:raw] {request.method} {request.url.path} "
                    f"status={response.status_code} content_type={content_type or 'unknown'} "
                    f"body=<non-json skipped>"
                )
                return response

            # 读取响应体并尽量还原成 Python 对象，后续统一塞到 data 或提取错误信息
            raw = response.body.decode("utf-8") if response.body else ""
            parsed = None
            
            # ==========解析响应体==============
            if raw:
                try:
                    # 
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    # 兜底：如果不是合法 JSON，按纯文本处理
                    parsed = raw

            # ==========封装响应体==============
            if 200 <= response.status_code < 300:
                # 成功约定：code 固定为 0，业务结果放入 data
                payload = {"code": 0, "message": "ok", "data": parsed}
            else:
                # 失败约定：code 使用 HTTP 状态码，message 优先使用 detail
                message = "请求失败"
                if isinstance(parsed, dict) and "detail" in parsed:
                    detail = parsed["detail"]
                    if isinstance(detail, str):
                        message = detail
                    else:
                        # detail 可能是 Pydantic 校验错误数组，转成字符串便于前端统一展示
                        message = json.dumps(detail, ensure_ascii=False)
                elif isinstance(parsed, str) and parsed:
                    message = parsed
                payload = {"code": response.status_code, "message": message, "data": None}

            print(
                f"[api-response:raw] {request.method} {request.url.path} "
                f"status={response.status_code} body={raw}"
            )
            print(
                f"[api-response:wrapped] {request.method} {request.url.path} "
                f"status={response.status_code} body={json.dumps(payload, ensure_ascii=False)}"
            )

            headers = dict(response.headers)
            # 内容重写后长度会变化，删除旧值让 Starlette 自动回填正确 content-length
            headers.pop("content-length", None)
            
            # 保留原始 HTTP 状态码，避免前端和网关丢失语义（如 404/422/500）
            # 返回封装后的 JSONResponse 对象
            return JSONResponse(status_code=response.status_code, content=payload, headers=headers)

        return custom_route_handler


router = APIRouter(prefix="/api", tags=["live2d-db"], route_class=UnifiedResponseRoute)
logger = logging.getLogger(__name__)

Db = Annotated[pymysql.connections.Connection, Depends(get_db)]


def _get_redis_client() -> Optional[object]:
    return _redis_factory_get_client(logger)


def _cache_recent_chat_sessions_on_login(db: Db, user_id: int) -> None:
    client = _get_redis_client()
    if client is None:
        return
    lookback_hours = max(
        1, int((os.environ.get("REDIS_CHAT_LOGIN_LOOKBACK_HOURS") or "24").strip() or "24")
    )
    max_rows = max(
        1, min(10_000, int((os.environ.get("REDIS_CHAT_LOGIN_MAX_ROWS") or "1000").strip() or "1000"))
    )
    rows = ChatSessionRepository.list_recent_by_user(
        db,
        user_id,
        hours=lookback_hours,
        limit=max_rows,
    )
    grouped_rows: dict[str, list[ChatSession]] = {}
    for r in rows:
        pkg = _normalize_package_key(r.package_key, fallback="default")
        grouped_rows.setdefault(pkg, []).append(r)

    total_turns = 0
    for pkg, rs in grouped_rows.items():
        ordered = list(reversed(rs))
        try:
            _memory_layers.seed_from_mysql_rows(client, user_id, pkg, ordered)
            total_turns += sum(
                1
                for x in ordered
                if (x.user_input or "").strip() or (x.ai_reply or "").strip()
            )
        except Exception:
            logger.exception(
                "登录预热双层记忆失败 user_id=%s package=%s",
                user_id,
                pkg,
            )
        try:
            with connection_ctx() as conn:
                lm = LongMemoryRepository.get_by_user_pkg(conn, user_id, pkg)
            txt = (
                merge_long_memory_record_for_prompt(lm)
                if lm and long_memory_has_any_content(lm)
                else ""
            )
            _memory_layers.write_long_memory_text(client, user_id, pkg, txt)
        except Exception:
            logger.exception(
                "登录预热长期记忆 Redis 失败 user_id=%s package=%s",
                user_id,
                pkg,
            )
    logger.info(
        "登录双层记忆预热完成 user_id=%s rows=%s packages=%s lookback_hours=%s",
        user_id,
        total_turns,
        len(grouped_rows),
        lookback_hours,
    )
    _warm_mimo_director_persona_redis(db, client, user_id, set(grouped_rows.keys()))


def _warm_mimo_director_persona_redis(
    db: Db, redis_cli: Optional[object], user_id: int, chat_packages: set[str]
) -> None:
    """登录时把各模型包人设写入 Redis，供 MiMo 导演【人设】【语气】高频读路径命中。"""
    if redis_cli is None or user_id <= 0:
        return
    if not _memory_layers.mimo_director_persona_redis_cache_enabled():
        return
    pkgs: set[str] = set(chat_packages)
    try:
        for p in PersonaRepository.list_package_personas_for_user(db, user_id):
            pk = (p.package_key or "").strip()
            if pk:
                pkgs.add(_normalize_package_key(pk))
        for pkg in pkgs:
            row = PersonaRepository.resolve_persona_for_package(db, user_id, pkg)
            role = (row.character_desc or "").strip() if row else ""
            tone = (row.tone_style or "").strip() if row else ""
            _memory_layers.set_mimo_director_persona_cached(
                redis_cli, user_id, pkg, role, tone
            )
    except Exception:
        logger.exception(
            "登录预热 MiMo 导演人设 Redis 失败 user_id=%s",
            user_id,
        )


def _safe_zip_path(name: str) -> Optional[str]:
    normalized = name.replace("\\", "/").strip("/")
    if not normalized:
        return None
    pp = PurePosixPath(normalized)
    if pp.is_absolute():
        return None
    parts = [part for part in pp.parts if part not in ("", ".")]
    if not parts:
        return None
    if any(part == ".." for part in parts):
        return None
    if parts[0].lower() == "__macosx":
        return None
    return "/".join(parts)


def _detect_root_prefix(entries: List[str]) -> Optional[str]:
    if not entries:
        return None
    first_seg = entries[0].split("/", 1)[0]
    for path in entries:
        parts = path.split("/", 1)
        if len(parts) < 2 or parts[0] != first_seg:
            return None
    return first_seg


def _pick_main_model3(candidates: List[str], package_key: str) -> Optional[str]:
    if not candidates:
        return None
    expect = f"{package_key}.model3.json".lower()
    for path in candidates:
        if PurePosixPath(path).name.lower() == expect:
            return path
    return sorted(candidates)[0]


def _extract_model3_refs(model3_raw: bytes) -> Dict[str, Any]:
    payload = json.loads(model3_raw.decode("utf-8"))
    refs = payload.get("FileReferences", {}) if isinstance(payload, dict) else {}
    return {
        "moc_path": refs.get("Moc"),
        "physics_path": refs.get("Physics"),
        "display_info_path": refs.get("DisplayInfo"),
        "textures": refs.get("Textures", []),
        "expressions": refs.get("Expressions", []),
        "motions": refs.get("Motions", {}),
        "hit_areas": payload.get("HitAreas", []),
        "groups": payload.get("Groups", []),
    }


def _build_model3_index(refs: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, str]], Dict[str, str], set[str]]:
    expressions_by_file: Dict[str, Dict[str, str]] = {}
    motions_by_file: Dict[str, str] = {}
    listed_files: set[str] = set()

    for item in refs.get("expressions", []) or []:
        if not isinstance(item, dict):
            continue
        file_path = str(item.get("File") or "").strip().lstrip("/")
        if not file_path:
            continue
        expressions_by_file[file_path] = {"name": str(item.get("Name") or "").strip()}
        listed_files.add(file_path)

    motions = refs.get("motions", {}) or {}
    if isinstance(motions, dict):
        for group, arr in motions.items():
            if not isinstance(arr, list):
                continue
            for item in arr:
                if not isinstance(item, dict):
                    continue
                file_path = str(item.get("File") or "").strip().lstrip("/")
                if not file_path:
                    continue
                motions_by_file[file_path] = str(group)
                listed_files.add(file_path)

    for key in ("moc_path", "physics_path", "display_info_path"):
        v = refs.get(key)
        if isinstance(v, str) and v.strip():
            listed_files.add(v.strip().lstrip("/"))
    for tex in refs.get("textures", []) or []:
        if isinstance(tex, str) and tex.strip():
            listed_files.add(tex.strip().lstrip("/"))
    return expressions_by_file, motions_by_file, listed_files


# ============================== 用户相关 ==============================
def _user_pub(u: User) -> UserPublic:
    assert u.user_id is not None
    return UserPublic(
        user_id=u.user_id,
        username=u.username,
        nickname=u.nickname,
        phone=u.phone,
        email=u.email,
        create_time=u.create_time,
        update_time=u.update_time,
        status=u.status,
    )


def _chat_pub(s: ChatSession) -> ChatSessionPublic:
    assert s.session_id is not None
    return ChatSessionPublic(
        session_id=s.session_id,
        user_id=s.user_id,
        package_key=s.package_key,
        user_input=s.user_input,
        ai_reply=s.ai_reply,
        emotion_tag=s.emotion_tag,
        session_key=s.session_key,
        create_time=s.create_time,
    )


def _mem_pub(m: LongMemory) -> LongMemoryPublic:
    assert m.memory_id is not None
    return LongMemoryPublic(
        memory_id=m.memory_id,
        user_id=m.user_id,
        package_key=m.package_key or "default",
        memory_type=m.memory_type or "long",
        period_overview=m.period_overview or "",
        create_time=m.create_time,
        update_time=m.update_time,
        last_consolidate_time=m.last_consolidate_time,
    )


def _persona_pub(p: Persona) -> PersonaPublic:
    return PersonaPublic(
        persona_id=p.persona_id,
        character_desc=p.character_desc,
        tone_style=p.tone_style,
        default_emotion=p.default_emotion,
        create_time=p.create_time,
        status=p.status,
        user_id=p.user_id,
        package_key=p.package_key,
    )


def _persona_package_placeholder(user_id: int, package_key: str) -> PersonaPublic:
    return PersonaPublic(
        persona_id=None,
        character_desc="",
        tone_style="",
        default_emotion=None,
        create_time=None,
        status=1,
        user_id=user_id,
        package_key=package_key,
    )


def _profile_pub(p: UserProfile) -> UserProfilePublic:
    assert p.profile_id is not None
    return UserProfilePublic(
        profile_id=p.profile_id,
        user_id=p.user_id,
        user_tags=p.user_tags,
        emotion_state=p.emotion_state,
        preferences=p.preferences,
        trouble_events=p.trouble_events,
        update_time=p.update_time,
    )


def _remind_pub(t: RemindTrigger) -> RemindTriggerPublic:
    assert t.trigger_id is not None
    return RemindTriggerPublic(
        trigger_id=t.trigger_id,
        user_id=t.user_id,
        trigger_type=t.trigger_type,
        trigger_time=t.trigger_time,
        session_id=t.session_id,
        trigger_content=t.trigger_content,
        delivery_message=None,
        is_triggered=t.is_triggered,
        create_time=t.create_time,
    )


def _model_asset_pub(a: Live2dModelAsset) -> Live2dModelAssetPublic:
    assert a.asset_id is not None
    return Live2dModelAssetPublic(
        asset_id=a.asset_id,
        user_id=a.user_id,
        package_key=a.package_key,
        relative_path=a.relative_path,
        file_name=a.file_name,
        asset_type=a.asset_type,
        public_url=a.public_url,
        object_key=a.object_key,
        mime_type=a.mime_type,
        logical_name=a.logical_name,
        motion_group=a.motion_group,
        is_listed_in_model3=a.is_listed_in_model3,
        is_entry_model=a.is_entry_model,
        file_size=a.file_size,
        sort_order=a.sort_order,
        remark=a.remark,
        create_time=a.create_time,
        update_time=a.update_time,
    )


def _tts_refer_pub(r: Live2dTtsRefer) -> Live2dTtsReferPublic:
    assert r.refer_id is not None
    return Live2dTtsReferPublic(
        refer_id=r.refer_id,
        user_id=r.user_id,
        package_key=r.package_key,
        audio_object_key=r.audio_object_key,
        audio_url=r.audio_url,
        audio_format=r.audio_format,
        prompt_text=r.prompt_text,
        prompt_language=r.prompt_language,
        create_time=r.create_time,
        update_time=r.update_time,
    )


def _object_key_from_background_public_url(url: str) -> Optional[str]:
    """从种子脚本写入的 path-style 公开 URL 还原 MinIO object key。"""
    u = (url or "").strip()
    if not u:
        return None
    base = get_public_base().rstrip("/")
    bucket = get_bucket_name()
    prefix = f"{base}/{bucket}/"
    if u.startswith(prefix):
        return u[len(prefix) :].lstrip("/")
    return None


def _background_url_for_client(stored_url: str, *, presign: bool, expires_in: int) -> Tuple[str, int]:
    if not presign:
        return stored_url, 0
    key = _object_key_from_background_public_url(stored_url)
    if not key:
        return stored_url, 0
    try:
        return presigned_get_url_cached(key, expires_in=expires_in), expires_in
    except Exception as exc:
        logger.warning("background presign failed: %s", exc)
        return stored_url, 0


def _background_image_public(row: BackgroundImage, *, presign: bool, expires_in: int) -> BackgroundImagePublic:
    assert row.id is not None
    url, ttl = _background_url_for_client(row.url, presign=presign, expires_in=expires_in)
    return BackgroundImagePublic(id=int(row.id), name=row.name, url=url, presigned_expires_in=ttl)


# ----- users -----
@router.post("/users", response_model=UserPublic)
def users_create(body: UserCreate, db: Db) -> UserPublic:
    existing = UserRepository.get_by_username(db, body.username)
    if existing:
        if existing.password != body.password:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        if existing.user_id:
            try:
                _cache_recent_chat_sessions_on_login(db, existing.user_id)
            except Exception:
                logger.exception("登录缓存 chat_session 失败 user_id=%s", existing.user_id)
        return _user_pub(existing)

    u = User(
        username=body.username,
        password=body.password,
        nickname=body.nickname,
        phone=body.phone,
        email=body.email,
        status=body.status,
    )
    uid = UserRepository.create(db, u)
    u.user_id = uid
    row = UserRepository.get_by_id(db, uid)
    if not row:
        raise HTTPException(status_code=500, detail="创建后读取失败")
    return _user_pub(row)


@router.get("/users", response_model=List[UserPublic])
def users_list(
    db: Db,
    status: Optional[int] = Query(None, description="1 正常 / 0 禁用；不传则全表分页"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> List[UserPublic]:
    if status is not None:
        if status not in (0, 1):
            raise HTTPException(status_code=400, detail="status 只能为 0 或 1")
        rows = UserRepository.list_by_status(db, status, limit=limit, offset=offset)
    else:
        rows = UserRepository.list_page(db, limit=limit, offset=offset)
    return [_user_pub(u) for u in rows]


@router.get("/users/count", response_model=CountResponse)
def users_count(
    db: Db,
    status: Optional[int] = Query(None, description="不传则用户总数；传 0/1 则按状态计数"),
) -> CountResponse:
    if status is None:
        total = UserRepository.count_all(db)
    else:
        if status not in (0, 1):
            raise HTTPException(status_code=400, detail="status 只能为 0 或 1")
        total = UserRepository.count_by_status(db, status)
    return CountResponse(total=total)


@router.get("/users/resolve", response_model=UserPublic)
def users_resolve(
    db: Db,
    phone: Optional[str] = Query(None, max_length=20),
    email: Optional[str] = Query(None, max_length=50),
) -> UserPublic:
    """按手机号或邮箱解析用户（二选一，用于注册前查重等）。"""
    if (phone is None) == (email is None):
        raise HTTPException(status_code=400, detail="请只传 phone 或 email 其中一个")
    u = UserRepository.get_by_phone(db, phone) if phone else UserRepository.get_by_email(db, email or "")
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    return _user_pub(u)


@router.get("/users/{user_id}", response_model=UserPublic)
def users_get(user_id: int, db: Db) -> UserPublic:
    u = UserRepository.get_by_id(db, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    return _user_pub(u)


@router.put("/users/{user_id}", response_model=UserPublic)
def users_update(user_id: int, body: UserUpdate, db: Db) -> UserPublic:
    u = UserRepository.get_by_id(db, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    if body.username is not None:
        u.username = body.username
    if body.password is not None:
        u.password = body.password
    if body.nickname is not None:
        u.nickname = body.nickname
    if body.phone is not None:
        u.phone = body.phone
    if body.email is not None:
        u.email = body.email
    if body.status is not None:
        u.status = body.status
    UserRepository.update(db, u)
    u2 = UserRepository.get_by_id(db, user_id)
    assert u2
    return _user_pub(u2)


@router.delete("/users/{user_id}", response_model=OkRows)
def users_delete(user_id: int, db: Db) -> OkRows:
    n = UserRepository.delete_by_id(db, user_id)
    if not n:
        raise HTTPException(status_code=404, detail="用户不存在或未删除")
    return OkRows(affected_rows=n)


# ----- chat_session -----
@router.post("/chat-sessions", response_model=ChatSessionPublic)
def chat_sessions_create(body: ChatSessionCreate, db: Db) -> ChatSessionPublic:
    s = ChatSession(
        user_id=body.user_id,
        package_key=body.package_key,
        user_input=body.user_input,
        ai_reply=body.ai_reply,
        emotion_tag=body.emotion_tag,
        session_key=body.session_key,
    )
    sid = ChatSessionRepository.create(db, s)
    s.session_id = sid
    got = ChatSessionRepository.get_by_id(db, sid)
    assert got
    return _chat_pub(got)


@router.get("/chat-sessions", response_model=List[ChatSessionPublic])
def chat_sessions_list(
    db: Db,
    user_id: int = Query(..., description="按用户筛选"),
    package_key: Optional[str] = Query(None, max_length=64, description="模型包键（区分 A/B 模型）"),
    session_key: Optional[str] = Query(None, max_length=64, description="若传则只查该会话窗口内消息"),
    page: int = Query(1, ge=1, le=100_000, description="页码，从 1 起"),
    size: int = Query(50, ge=1, le=500, description="每页条数"),
) -> List[ChatSessionPublic]:
    lim = max(1, min(int(size), 500))
    off = max(0, (int(page) - 1) * lim)
    off = min(off, 100_000)
    if session_key:
        rows = ChatSessionRepository.list_by_session_key(
            db, user_id, session_key, package_key=package_key, limit=lim, offset=off
        )
    else:
        rows = ChatSessionRepository.list_by_user(
            db, user_id, package_key=package_key, limit=lim, offset=off
        )
    return [_chat_pub(s) for s in rows]


@router.get("/chat-sessions/count", response_model=CountResponse)
def chat_sessions_count(
    db: Db,
    user_id: int = Query(...),
    package_key: Optional[str] = Query(None, max_length=64, description="模型包键（区分 A/B 模型）"),
    session_key: Optional[str] = Query(None, max_length=64, description="传则只统计该会话窗口内条数"),
) -> CountResponse:
    if session_key:
        total = ChatSessionRepository.count_by_user_and_session(
            db, user_id, session_key, package_key=package_key
        )
    else:
        total = ChatSessionRepository.count_by_user(db, user_id, package_key=package_key)
    return CountResponse(total=total)


@router.get("/chat-sessions/{session_id}", response_model=ChatSessionPublic)
def chat_sessions_get(session_id: int, db: Db) -> ChatSessionPublic:
    s = ChatSessionRepository.get_by_id(db, session_id)
    if not s:
        raise HTTPException(status_code=404, detail="对话记录不存在")
    return _chat_pub(s)


@router.put("/chat-sessions/{session_id}", response_model=ChatSessionPublic)
def chat_sessions_update(session_id: int, body: ChatSessionUpdate, db: Db) -> ChatSessionPublic:
    s = ChatSessionRepository.get_by_id(db, session_id)
    if not s:
        raise HTTPException(status_code=404, detail="对话记录不存在")
    s.user_id = body.user_id
    s.package_key = body.package_key
    s.user_input = body.user_input
    s.ai_reply = body.ai_reply
    s.emotion_tag = body.emotion_tag
    s.session_key = body.session_key
    ChatSessionRepository.update(db, s)
    got = ChatSessionRepository.get_by_id(db, session_id)
    assert got
    return _chat_pub(got)


@router.delete("/chat-sessions/{session_id}", response_model=OkRows)
def chat_sessions_delete(session_id: int, db: Db) -> OkRows:
    n = ChatSessionRepository.delete_by_id(db, session_id)
    if not n:
        raise HTTPException(status_code=404, detail="对话记录不存在")
    return OkRows(affected_rows=n)


# ----- long_memory -----
@router.post("/long-memories", response_model=LongMemoryPublic)
def long_memories_create(body: LongMemoryCreate, db: Db) -> LongMemoryPublic:
    m = LongMemory(
        user_id=body.user_id,
        package_key=body.package_key or "default",
        memory_type=body.memory_type or "long",
        period_overview=body.period_overview or "",
    )
    mid = LongMemoryRepository.create(db, m)
    got = LongMemoryRepository.get_by_id(db, mid)
    assert got
    return _mem_pub(got)


@router.get("/long-memories", response_model=List[LongMemoryPublic])
def long_memories_list(
    db: Db,
    user_id: int = Query(...),
    memory_type: Optional[str] = Query(None, max_length=20, description="瞬时/短期/长期等；传则按类型筛选"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> List[LongMemoryPublic]:
    if memory_type:
        rows = LongMemoryRepository.list_by_user_and_type(
            db, user_id, memory_type, limit=limit, offset=offset
        )
    else:
        rows = LongMemoryRepository.list_by_user(db, user_id, limit=limit, offset=offset)
    return [_mem_pub(m) for m in rows]


@router.get("/long-memories/count", response_model=CountResponse)
def long_memories_count(
    db: Db,
    user_id: int = Query(...),
    memory_type: Optional[str] = Query(None, max_length=20, description="传则只统计该类型"),
) -> CountResponse:
    total = LongMemoryRepository.count_by_user(db, user_id, memory_type=memory_type)
    return CountResponse(total=total)


@router.post("/long-memories/consolidate-now", response_model=LongMemoryConsolidateNowPublic)
async def long_memories_consolidate_now(
    user_id: int = Query(..., ge=1),
    package_key: Optional[str] = Query(
        None,
        max_length=64,
        description="当前模型包键，与聊天一致；省略则按 default",
    ),
) -> LongMemoryConsolidateNowPublic:
    """立即执行一轮周期概要更新（写入 long_memory.period_overview），不受后台定时任务 24h 最短间隔限制。"""
    from live2d_db.long_memory_consolidator import consolidate_one

    pkg_norm = _normalize_package_key((package_key or "").strip() or "default", fallback="default")
    redis_cli = _get_redis_client()

    def _run() -> bool:
        with connection_ctx() as conn:
            return consolidate_one(conn, redis_cli, user_id, pkg_norm)

    updated = await asyncio.to_thread(_run)
    return LongMemoryConsolidateNowPublic(ok=True, updated=updated)


@router.get("/long-memories/{memory_id}", response_model=LongMemoryPublic)
def long_memories_get(memory_id: int, db: Db) -> LongMemoryPublic:
    m = LongMemoryRepository.get_by_id(db, memory_id)
    if not m:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return _mem_pub(m)


@router.put("/long-memories/{memory_id}", response_model=LongMemoryPublic)
def long_memories_update(memory_id: int, body: LongMemoryUpdate, db: Db) -> LongMemoryPublic:
    m = LongMemoryRepository.get_by_id(db, memory_id)
    if not m:
        raise HTTPException(status_code=404, detail="记忆不存在")
    patch = body.model_dump(exclude_unset=True)
    for key, val in patch.items():
        if hasattr(m, key):
            setattr(m, key, val)
    LongMemoryRepository.update(db, m)
    got = LongMemoryRepository.get_by_id(db, memory_id)
    assert got
    return _mem_pub(got)


@router.delete("/long-memories/{memory_id}", response_model=OkRows)
def long_memories_delete(memory_id: int, db: Db) -> OkRows:
    n = LongMemoryRepository.delete_by_id(db, memory_id)
    if not n:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return OkRows(affected_rows=n)


# ----- persona -----
@router.post("/personas", response_model=PersonaPublic)
def personas_create(body: PersonaCreate, db: Db) -> PersonaPublic:
    p = Persona(
        character_desc=body.character_desc,
        tone_style=body.tone_style,
        default_emotion=body.default_emotion,
        status=body.status,
    )
    pid = PersonaRepository.create(db, p)
    got = PersonaRepository.get_by_id(db, pid)
    assert got
    return _persona_pub(got)


@router.get("/personas", response_model=List[PersonaPublic])
def personas_list(
    db: Db,
    enabled_only: bool = Query(False, description="为 true 时仅返回启用人设，忽略 status 分页参数"),
    status: Optional[int] = Query(None, description="1 启用 / 0 禁用；与 enabled_only 互斥，优先 enabled_only"),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> List[PersonaPublic]:
    if enabled_only:
        rows = PersonaRepository.list_enabled(db)
    elif status is not None:
        if status not in (0, 1):
            raise HTTPException(status_code=400, detail="status 只能为 0 或 1")
        rows = PersonaRepository.list_by_status(db, status, limit=limit, offset=offset)
    else:
        rows = PersonaRepository.list_all(db)
    return [_persona_pub(p) for p in rows]


@router.get("/personas/count", response_model=CountResponse)
def personas_count(
    db: Db,
    status: Optional[int] = Query(None, description="不传则全部人设数；传 0/1 按状态计数"),
) -> CountResponse:
    if status is None:
        total = PersonaRepository.count_all(db)
    else:
        if status not in (0, 1):
            raise HTTPException(status_code=400, detail="status 只能为 0 或 1")
        total = PersonaRepository.count_by_status(db, status)
    return CountResponse(total=total)


@router.get("/personas/by-package/{package_key}", response_model=PersonaPublic)
def personas_get_by_package(
    package_key: str,
    db: Db,
    user_id: int = Query(..., ge=1, description="用户 ID"),
) -> PersonaPublic:
    row = PersonaRepository.resolve_persona_for_package(db, user_id, package_key)
    pkg_norm = _normalize_package_key(package_key)
    if row is None:
        return _persona_package_placeholder(user_id, pkg_norm)
    return _persona_pub(row)


@router.get("/personas/package-bound", response_model=list[PersonaPublic])
def personas_list_package_bound(
    db: Db,
    user_id: int = Query(..., ge=1, description="用户 ID"),
) -> list[PersonaPublic]:
    """列出该用户已保存的全部「按模型包绑定」人设，便于前端展示或核对已填正文。"""
    rows = PersonaRepository.list_package_personas_for_user(db, user_id)
    return [_persona_pub(p) for p in rows]


@router.post("/personas/expand-from-hints", response_model=PersonaExpandHintsResponse)
def personas_expand_from_hints(body: PersonaExpandHintsBody) -> PersonaExpandHintsResponse:
    """根据简短关键词调用 LLM 扩写 ``character_desc`` / ``tone_style``（不入库，供表单预览）。"""
    try:
        desc, tone = expand_persona_from_hints(
            (body.character_hint or "").strip(),
            (body.tone_hint or "").strip(),
        )
    except PersonaExpandError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return PersonaExpandHintsResponse(character_desc=desc, tone_style=tone)


@router.put("/personas/by-package/{package_key}", response_model=PersonaPublic)
def personas_upsert_by_package(
    package_key: str,
    body: PersonaPackageUpsert,
    db: Db,
    user_id: int = Query(..., ge=1, description="用户 ID"),
) -> PersonaPublic:
    pkg = _normalize_package_key(package_key)
    desc_in = (body.character_desc or "").strip()
    tone_in = (body.tone_style or "").strip()
    if body.expand_with_llm:
        try:
            desc_in, tone_in = expand_persona_from_hints(desc_in, tone_in)
        except PersonaExpandError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
    got = PersonaRepository.upsert_package_persona(
        db, user_id, pkg, desc_in, tone_in
    )
    rc = _get_redis_client()
    if rc:
        _memory_layers.set_mimo_director_persona_cached(
            rc,
            user_id,
            pkg,
            (got.character_desc or "").strip(),
            (got.tone_style or "").strip(),
        )
    return _persona_pub(got)


@router.get("/personas/{persona_id}", response_model=PersonaPublic)
def personas_get(persona_id: int, db: Db) -> PersonaPublic:
    p = PersonaRepository.get_by_id(db, persona_id)
    if not p:
        raise HTTPException(status_code=404, detail="人设不存在")
    return _persona_pub(p)


@router.put("/personas/{persona_id}", response_model=PersonaPublic)
def personas_update(persona_id: int, body: PersonaUpdate, db: Db) -> PersonaPublic:
    p = PersonaRepository.get_by_id(db, persona_id)
    if not p:
        raise HTTPException(status_code=404, detail="人设不存在")
    p.character_desc = body.character_desc
    p.tone_style = body.tone_style
    p.default_emotion = body.default_emotion
    p.status = body.status
    PersonaRepository.update(db, p)
    got = PersonaRepository.get_by_id(db, persona_id)
    assert got
    rc = _get_redis_client()
    if rc and got.user_id and (got.package_key or "").strip():
        _memory_layers.set_mimo_director_persona_cached(
            rc,
            int(got.user_id),
            _normalize_package_key(str(got.package_key)),
            (got.character_desc or "").strip(),
            (got.tone_style or "").strip(),
        )
    return _persona_pub(got)


@router.delete("/personas/{persona_id}", response_model=OkRows)
def personas_delete(persona_id: int, db: Db) -> OkRows:
    prev = PersonaRepository.get_by_id(db, persona_id)
    n = PersonaRepository.delete_by_id(db, persona_id)
    if not n:
        raise HTTPException(status_code=404, detail="人设不存在")
    rc = _get_redis_client()
    if rc and prev and prev.user_id and (prev.package_key or "").strip():
        _memory_layers.delete_mimo_director_persona_cached(
            rc,
            int(prev.user_id),
            _normalize_package_key(str(prev.package_key)),
        )
    return OkRows(affected_rows=n)


# ----- user_profile -----
@router.get("/user-profiles/by-user/{user_id}", response_model=UserProfilePublic)
def user_profiles_get_by_user(user_id: int, db: Db) -> UserProfilePublic:
    p = UserProfileRepository.get_by_user_id(db, user_id)
    if not p:
        raise HTTPException(status_code=404, detail="画像不存在")
    return _profile_pub(p)


@router.put("/user-profiles/by-user/{user_id}", response_model=UserProfilePublic)
def user_profiles_upsert(user_id: int, body: UserProfileUpsert, db: Db) -> UserProfilePublic:
    p = UserProfile(
        user_id=user_id,
        user_tags=body.user_tags,
        emotion_state=body.emotion_state,
        preferences=body.preferences,
        trouble_events=body.trouble_events,
    )
    UserProfileRepository.upsert_by_user_id(db, p)
    got = UserProfileRepository.get_by_user_id(db, user_id)
    assert got
    return _profile_pub(got)


@router.get("/user-profiles/{profile_id}", response_model=UserProfilePublic)
def user_profiles_get(profile_id: int, db: Db) -> UserProfilePublic:
    p = UserProfileRepository.get_by_id(db, profile_id)
    if not p:
        raise HTTPException(status_code=404, detail="画像不存在")
    return _profile_pub(p)


@router.delete("/user-profiles/{profile_id}", response_model=OkRows)
def user_profiles_delete(profile_id: int, db: Db) -> OkRows:
    n = UserProfileRepository.delete_by_id(db, profile_id)
    if not n:
        raise HTTPException(status_code=404, detail="画像不存在")
    return OkRows(affected_rows=n)


# ----- remind_trigger -----
@router.post("/remind-triggers", response_model=RemindTriggerPublic)
def remind_triggers_create(body: RemindTriggerCreate, db: Db) -> RemindTriggerPublic:
    t = RemindTrigger(
        user_id=body.user_id,
        trigger_type=body.trigger_type,
        trigger_time=body.trigger_time,
        session_id=body.session_id,
        trigger_content=body.trigger_content,
        is_triggered=body.is_triggered,
    )
    tid = RemindTriggerRepository.create(db, t)
    got = RemindTriggerRepository.get_by_id(db, tid)
    assert got
    return _remind_pub(got)


@router.get("/remind-triggers", response_model=List[RemindTriggerPublic])
def remind_triggers_list(
    db: Db,
    user_id: int = Query(..., description="按用户筛选"),
    is_triggered: Optional[int] = Query(None, description="0 未触发 / 1 已触发；不传则该用户全部"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> List[RemindTriggerPublic]:
    if is_triggered is not None:
        if is_triggered not in (0, 1):
            raise HTTPException(status_code=400, detail="is_triggered 只能为 0 或 1")
        rows = RemindTriggerRepository.list_by_user_and_triggered(
            db, user_id, is_triggered, limit=limit, offset=offset
        )
    else:
        rows = RemindTriggerRepository.list_by_user(db, user_id, limit=limit, offset=offset)
    return [_remind_pub(t) for t in rows]


@router.get("/remind-triggers/count", response_model=CountResponse)
def remind_triggers_count(
    db: Db,
    user_id: int = Query(...),
    is_triggered: Optional[int] = Query(None, description="传 0/1 则只统计该状态"),
) -> CountResponse:
    if is_triggered is not None and is_triggered not in (0, 1):
        raise HTTPException(status_code=400, detail="is_triggered 只能为 0 或 1")
    total = RemindTriggerRepository.count_by_user(db, user_id, is_triggered=is_triggered)
    return CountResponse(total=total)


@router.get("/remind-triggers/pending-scan", response_model=List[RemindTriggerPublic])
def remind_triggers_pending_scan(
    db: Db,
    before: datetime = Query(..., description="截止时间（ISO8601），含未触发且 trigger_time<=before"),
    limit: int = Query(200, ge=1, le=1000),
) -> List[RemindTriggerPublic]:
    """定时任务扫描：待触发关怀（与 Repository.list_pending_before 一致）。"""
    rows = RemindTriggerRepository.list_pending_before(db, before, limit=limit)
    return [_remind_pub(t) for t in rows]


@router.get("/remind-triggers/pending-count", response_model=CountResponse)
def remind_triggers_pending_count(
    db: Db,
    before: datetime = Query(..., description="与 pending-scan 条件一致，返回待处理条数"),
) -> CountResponse:
    total = RemindTriggerRepository.count_pending_before(db, before)
    return CountResponse(total=total)


@router.post("/remind-triggers/scan-now", response_model=RemindSchedulerScanNowPublic)
async def remind_triggers_scan_now() -> RemindSchedulerScanNowPublic:
    """立即执行一轮定时关怀扫描（不等后台间隔）。无鉴权，生产环境请自行加认证。"""
    from live2d_db.remind_trigger_scheduler import run_scan_tick

    stats = await run_scan_tick()
    return RemindSchedulerScanNowPublic(ok=True, **stats)


@router.get("/remind-triggers/{trigger_id}", response_model=RemindTriggerPublic)
def remind_triggers_get(trigger_id: int, db: Db) -> RemindTriggerPublic:
    t = RemindTriggerRepository.get_by_id(db, trigger_id)
    if not t:
        raise HTTPException(status_code=404, detail="触发记录不存在")
    return _remind_pub(t)


@router.put("/remind-triggers/{trigger_id}", response_model=RemindTriggerPublic)
def remind_triggers_update(trigger_id: int, body: RemindTriggerUpdate, db: Db) -> RemindTriggerPublic:
    t = RemindTriggerRepository.get_by_id(db, trigger_id)
    if not t:
        raise HTTPException(status_code=404, detail="触发记录不存在")
    t.user_id = body.user_id
    t.trigger_type = body.trigger_type
    t.trigger_time = body.trigger_time
    t.session_id = body.session_id
    t.trigger_content = body.trigger_content
    t.is_triggered = body.is_triggered
    RemindTriggerRepository.update(db, t)
    got = RemindTriggerRepository.get_by_id(db, trigger_id)
    assert got
    return _remind_pub(got)


@router.delete("/remind-triggers/{trigger_id}", response_model=OkRows)
def remind_triggers_delete(trigger_id: int, db: Db) -> OkRows:
    n = RemindTriggerRepository.delete_by_id(db, trigger_id)
    if not n:
        raise HTTPException(status_code=404, detail="触发记录不存在")
    return OkRows(affected_rows=n)


# ----- live2d_model_asset（Resources 下模型包文件） -----
@router.post("/live2d-model-assets", response_model=Live2dModelAssetPublic)
def live2d_model_assets_create(body: Live2dModelAssetCreate, db: Db) -> Live2dModelAssetPublic:
    a = Live2dModelAsset(
        user_id=body.user_id,
        package_key=body.package_key,
        relative_path=body.relative_path,
        file_name=body.file_name,
        asset_type=body.asset_type,
        public_url=body.public_url,
        object_key=body.object_key,
        mime_type=body.mime_type,
        logical_name=body.logical_name,
        motion_group=body.motion_group,
        is_listed_in_model3=body.is_listed_in_model3,
        is_entry_model=body.is_entry_model,
        file_size=body.file_size,
        sort_order=body.sort_order,
        remark=body.remark,
    )
    aid = Live2dModelAssetRepository.create(db, a)
    got = Live2dModelAssetRepository.get_by_id(db, aid)
    assert got
    invalidate_live2d_catalog_cache(body.user_id, body.package_key)
    return _model_asset_pub(got)


@router.get("/live2d-model-assets", response_model=List[Live2dModelAssetPublic])
def live2d_model_assets_list(
    db: Db,
    user_id: int = Query(..., ge=1, description="用户 ID，与 user 表外键一致"),
    package_key: Optional[str] = Query(None, description="如 Xiaozi；不传则返回该用户下全部包文件"),
    asset_type: Optional[str] = Query(None, description="按类型筛选；仅当指定 package_key 时生效"),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> List[Live2dModelAssetPublic]:
    if package_key:
        rows = Live2dModelAssetRepository.list_by_package(
            db, user_id, package_key, asset_type=asset_type, limit=limit, offset=offset
        )
    else:
        if asset_type is not None:
            raise HTTPException(status_code=400, detail="按 asset_type 筛选时请同时传 package_key")
        rows = Live2dModelAssetRepository.list_by_user(db, user_id, limit=limit, offset=offset)
    return [_model_asset_pub(a) for a in rows]


@router.get("/live2d-model-assets/count", response_model=CountResponse)
def live2d_model_assets_count(
    db: Db,
    user_id: int = Query(..., ge=1),
    package_key: Optional[str] = Query(None, description="不传则统计该用户全部资源行"),
    asset_type: Optional[str] = Query(None, description="仅与 package_key 联用"),
) -> CountResponse:
    if package_key:
        total = Live2dModelAssetRepository.count_by_package(
            db, user_id, package_key, asset_type=asset_type
        )
    else:
        if asset_type is not None:
            raise HTTPException(status_code=400, detail="按 asset_type 统计时请同时传 package_key")
        total = Live2dModelAssetRepository.count_by_user(db, user_id)
    return CountResponse(total=total)


@router.get("/live2d-model-assets/packages", response_model=list[Live2dModelPackageInfo])
def live2d_model_assets_list_packages(
    db: Db,
    user_id: int = Query(..., ge=1, description="用户 ID"),
) -> list[Live2dModelPackageInfo]:
    assets = Live2dModelAssetRepository.list_by_user(db, user_id, limit=1000)
    tts_refers = Live2dTtsReferRepository.list_by_user(db, user_id)
    has_tts_refer = {r.package_key for r in tts_refers}
    packages: dict[str, dict] = {}
    for a in assets:
        if a.package_key not in packages:
            packages[a.package_key] = {
                "file_count": 0,
                "asset_types": set(),
                "has_entry_model": False,
            }
        packages[a.package_key]["file_count"] += 1
        packages[a.package_key]["asset_types"].add(a.asset_type)
        if a.is_entry_model:
            packages[a.package_key]["has_entry_model"] = True
    return [
        Live2dModelPackageInfo(
            package_key=pkg,
            file_count=info["file_count"],
            asset_types=sorted(info["asset_types"]),
            has_entry_model=info["has_entry_model"],
            has_tts_refer=(pkg in has_tts_refer),
        )
        for pkg, info in packages.items()
    ]


@router.delete("/live2d-model-assets/by-package/{package_key}", response_model=OkRows)
def live2d_model_assets_delete_package(
    package_key: str,
    db: Db,
    user_id: int = Query(..., ge=1, description="仅删除该用户在该包下的索引行"),
) -> OkRows:
    n = Live2dModelAssetRepository.delete_by_package_key(db, package_key, user_id)
    pkg_norm = _normalize_package_key(package_key)
    PersonaRepository.delete_by_user_and_package(db, user_id, package_key)
    rc = _get_redis_client()
    if rc:
        _memory_layers.delete_mimo_director_persona_cached(rc, user_id, pkg_norm)
    invalidate_live2d_catalog_cache(user_id, package_key)
    return OkRows(affected_rows=n)


@router.get("/live2d-tts-refers", response_model=list[Live2dTtsReferPublic])
def live2d_tts_refers_list(
    db: Db,
    user_id: int = Query(..., ge=1, description="用户 ID"),
    package_key: Optional[str] = Query(None, description="传则仅查该模型包"),
) -> list[Live2dTtsReferPublic]:
    if package_key:
        pkg = _normalize_package_key(package_key)
        row = Live2dTtsReferRepository.get_by_user_and_package(db, user_id, pkg)
        return [_tts_refer_pub(row)] if row else []
    rows = Live2dTtsReferRepository.list_by_user(db, user_id)
    return [_tts_refer_pub(r) for r in rows]


@router.post("/live2d-tts-refers/upload", response_model=Live2dTtsReferUploadPublic)
async def live2d_tts_refers_upload(
    db: Db,
    user_id: int = Form(..., ge=1, description="关联 user.user_id"),
    package_key: str = Form(..., description="模型包键，如 Xiaozi"),
    prompt_text: str = Form(..., description="参考文本"),
    prompt_language: str = Form("zh", description="参考语种，如 zh/en/ja"),
    refer_audio: UploadFile = File(..., description="参考音频（建议 wav）"),
) -> Live2dTtsReferUploadPublic:
    user = UserRepository.get_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    pkg = _normalize_package_key(package_key)
    ptxt = prompt_text.strip()
    plang = (prompt_language or "zh").strip() or "zh"
    if not ptxt:
        raise HTTPException(status_code=400, detail="prompt_text 不能为空")
    if not refer_audio.filename:
        raise HTTPException(status_code=400, detail="参考音频文件名为空")

    allowed_ext = {".wav", ".mp3", ".ogg", ".m4a", ".flac", ".aac"}
    ext = PurePosixPath(refer_audio.filename).suffix.lower()
    if ext not in allowed_ext:
        raise HTTPException(status_code=400, detail="仅支持 wav/mp3/ogg/m4a/flac/aac")

    max_bytes = int(os.environ.get("LIVE2D_TTS_REFER_MAX_BYTES", str(50 * 1024 * 1024)))
    blob = await refer_audio.read()
    if not blob:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(blob) > max_bytes:
        raise HTTPException(status_code=413, detail=f"上传文件过大，限制 {max_bytes} 字节")

    # 非标准 RIFF/WAVE 时统一经 ffmpeg 转为 PCM WAV 再入库（与 MiMo / GPT-SoVITS 兼容）
    if is_standard_riff_wav(blob):
        final_blob = blob
        content_type = "audio/wav"
    else:
        try:
            ff_timeout = float(os.environ.get("LIVE2D_TTS_REFER_FFMPEG_TIMEOUT", "120"))
        except ValueError:
            ff_timeout = 120.0
        converted = ffmpeg_convert_bytes_to_wav(
            blob,
            ext,
            timeout_s=ff_timeout,
            max_out_bytes=max_bytes,
        )
        if converted is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "音频不是标准 WAV（RIFF/WAVE PCM），且 ffmpeg 转码失败或未安装。"
                    "请安装 ffmpeg 并加入 PATH（Windows: winget install ffmpeg），"
                    "或上传可被 ffmpeg 识别的 wav/mp3/ogg/m4a/flac/aac。"
                ),
            )
        final_blob = converted
        content_type = "audio/wav"

    if len(final_blob) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"转码后文件超过限制 {max_bytes} 字节",
        )

    base_name = _PACKAGE_KEY_PATTERN.sub("_", PurePosixPath(refer_audio.filename).stem).strip("._-") or "refer"
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    stored_ext = ".wav"
    object_key = f"users/{user_id}/packages/{pkg}/tts_refer/{base_name}_{ts}{stored_ext}"
    _, audio_url = upload_bytes(
        final_blob,
        object_name=object_key,
        content_type=content_type,
    )

    row = Live2dTtsRefer(
        user_id=user_id,
        package_key=pkg,
        audio_object_key=object_key,
        audio_url=audio_url,
        audio_format="wav",
        prompt_text=ptxt,
        prompt_language=plang,
    )
    Live2dTtsReferRepository.upsert_by_user_package(db, row)
    return Live2dTtsReferUploadPublic(
        user_id=user_id,
        package_key=pkg,
        bucket=get_bucket_name(),
        object_key=object_key,
        audio_url=audio_url,
        audio_format="wav",
        prompt_text=ptxt,
        prompt_language=plang,
    )


# ----- background_image（须在 /background-images/{id} 等动态段之前注册具体路径）-----
@router.get("/background-images/count", response_model=CountResponse)
def background_images_count(db: Db) -> CountResponse:
    return CountResponse(total=BackgroundImageRepository.count_all(db))


@router.get("/background-images/random", response_model=BackgroundImagePublic)
def background_images_random(
    db: Db,
    presign: bool = Query(True, description="是否生成 MinIO 预签名 URL（私有桶建议开启）"),
    expires_in: Optional[int] = Query(
        None,
        ge=60,
        le=604800,
        description="预签名秒数；省略则使用环境变量 MINIO_PRESIGN_EXPIRES（默认 3600）",
    ),
) -> BackgroundImagePublic:
    row = BackgroundImageRepository.get_random_one(db)
    if not row:
        raise HTTPException(
            status_code=404,
            detail="暂无背景图，请先执行迁移并运行 PY/seed_demo_background_images_once.py",
        )
    ttl = expires_in if expires_in is not None else int(os.environ.get("MINIO_PRESIGN_EXPIRES", "3600"))
    return _background_image_public(row, presign=presign, expires_in=ttl)


@router.get("/background-images", response_model=List[BackgroundImagePublic])
def background_images_list(
    db: Db,
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    presign: bool = Query(False, description="为每条生成预签名（数据多时会慢）"),
    expires_in: Optional[int] = Query(None, ge=60, le=604800),
) -> List[BackgroundImagePublic]:
    rows = BackgroundImageRepository.list_all(db, limit=limit, offset=offset)
    ttl = expires_in if expires_in is not None else int(os.environ.get("MINIO_PRESIGN_EXPIRES", "3600"))
    return [_background_image_public(r, presign=presign, expires_in=ttl) for r in rows]


# 必须在 /live2d-model-assets/{asset_id} 之前注册，否则路径段 download-url 会被当成 asset_id 导致 422。
@router.get("/live2d-model-assets/download-url", response_model=DownloadUrlPublic)
def live2d_model_assets_download_url(
    db: Db,
    asset_id: int = Query(..., ge=1, description="资源 ID"),
    expires_in: Optional[int] = Query(None, ge=1, le=604800, description="临时链接有效期（秒）"),
) -> DownloadUrlPublic:
    a = Live2dModelAssetRepository.get_by_id(db, asset_id)
    if not a:
        raise HTTPException(status_code=404, detail="资源不存在")

    ttl = expires_in if expires_in is not None else int(os.environ.get("MINIO_PRESIGN_EXPIRES", "3600"))

    if a.object_key:
        try:
            url = presigned_get_url_cached(a.object_key, expires_in=ttl)
            return DownloadUrlPublic(url=url, expires_in=ttl)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"生成下载链接失败: {exc}") from exc

    # 兼容老数据：若 object_key 为空，则回退返回 public_url
    if a.public_url:
        return DownloadUrlPublic(url=a.public_url, expires_in=0)
    raise HTTPException(status_code=400, detail="资源缺少 object_key/public_url，无法生成下载链接")


@router.get("/live2d-model-assets/{asset_id}", response_model=Live2dModelAssetPublic)
def live2d_model_assets_get(asset_id: int, db: Db) -> Live2dModelAssetPublic:
    a = Live2dModelAssetRepository.get_by_id(db, asset_id)
    if not a:
        raise HTTPException(status_code=404, detail="资源不存在")
    return _model_asset_pub(a)


@router.put("/live2d-model-assets/{asset_id}", response_model=Live2dModelAssetPublic)
def live2d_model_assets_update(asset_id: int, body: Live2dModelAssetUpdate, db: Db) -> Live2dModelAssetPublic:
    a = Live2dModelAssetRepository.get_by_id(db, asset_id)
    if not a:
        raise HTTPException(status_code=404, detail="资源不存在")
    prev_uid = a.user_id
    prev_pkg = a.package_key
    a.user_id = body.user_id
    a.package_key = body.package_key
    a.relative_path = body.relative_path
    a.file_name = body.file_name
    a.asset_type = body.asset_type
    a.public_url = body.public_url
    a.object_key = body.object_key
    a.mime_type = body.mime_type
    a.logical_name = body.logical_name
    a.motion_group = body.motion_group
    a.is_listed_in_model3 = body.is_listed_in_model3
    a.is_entry_model = body.is_entry_model
    a.file_size = body.file_size
    a.sort_order = body.sort_order
    a.remark = body.remark
    Live2dModelAssetRepository.update(db, a)
    got = Live2dModelAssetRepository.get_by_id(db, asset_id)
    assert got
    invalidate_live2d_catalog_cache(prev_uid, prev_pkg)
    if prev_uid != body.user_id or prev_pkg != body.package_key:
        invalidate_live2d_catalog_cache(body.user_id, body.package_key)
    return _model_asset_pub(got)


@router.delete("/live2d-model-assets/{asset_id}", response_model=OkRows)
def live2d_model_assets_delete(asset_id: int, db: Db) -> OkRows:
    a = Live2dModelAssetRepository.get_by_id(db, asset_id)
    if not a:
        raise HTTPException(status_code=404, detail="资源不存在")
    n = Live2dModelAssetRepository.delete_by_id(db, asset_id)
    invalidate_live2d_catalog_cache(a.user_id, a.package_key or "")
    return OkRows(affected_rows=n)


@router.post("/live2d-model-assets/upload-zip", response_model=Live2dModelZipUploadPublic)
async def live2d_model_assets_upload_zip(
    db: Db,
    user_id: int = Form(..., ge=1, description="关联 user.user_id"),
    package_key: Optional[str] = Form(None, description="不传则从 zip 顶层目录或文件名推断"),
    model_zip: UploadFile = File(..., description="Live2D 模型 zip 文件"),
) -> Live2dModelZipUploadPublic:
    user = UserRepository.get_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    if not model_zip.filename or not model_zip.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="仅支持 .zip 文件")

    max_bytes = int(os.environ.get("LIVE2D_ZIP_MAX_BYTES", str(100 * 1024 * 1024)))
    payload = await model_zip.read()
    if not payload:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(payload) > max_bytes:
        raise HTTPException(status_code=413, detail=f"上传文件过大，限制 {max_bytes} 字节")

    filename_stem = (model_zip.filename.rsplit(".", 1)[0] or "uploaded").strip()
    skipped_files = 0
    main_model3_path: Optional[str] = None
    refs: Dict[str, Any] = {}
    src_zip_key: Optional[str] = None

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            infos = [info for info in zf.infolist() if not info.is_dir()]
            safe_to_info: dict[str, zipfile.ZipInfo] = {}
            normalized_paths: List[str] = []
            for info in infos:
                safe_path = _safe_zip_path(info.filename)
                if not safe_path:
                    skipped_files += 1
                    continue
                safe_to_info[safe_path] = info
                normalized_paths.append(safe_path)

            if not normalized_paths:
                raise HTTPException(status_code=400, detail="zip 中没有可导入文件")

            root_prefix = _detect_root_prefix(normalized_paths)
            pkg = _normalize_package_key(package_key, fallback=root_prefix or filename_stem)
            model3_candidates: List[str] = []
            for full_path in safe_to_info.keys():
                rel_candidate = full_path
                if root_prefix:
                    rel_candidate = full_path[len(root_prefix) + 1 :]
                rel_candidate = rel_candidate.strip("/")
                if rel_candidate.lower().endswith(".model3.json"):
                    model3_candidates.append(rel_candidate)
            main_model3_path = _pick_main_model3(model3_candidates, pkg)
            if not main_model3_path:
                raise HTTPException(status_code=400, detail="zip 中未找到 .model3.json 入口文件")

            main_model3_zip_path = main_model3_path
            if root_prefix:
                main_model3_zip_path = f"{root_prefix}/{main_model3_path}"
            refs = _extract_model3_refs(zf.read(safe_to_info[main_model3_zip_path]))
            expr_index, motion_index, listed_files = _build_model3_index(refs)

            assets: List[Live2dModelAsset] = []
            uploaded_files = 0
            sort_order = 0
            object_prefix = f"users/{user_id}/packages/{pkg}"
            for full_path in sorted(safe_to_info.keys()):
                rel_path = full_path
                if root_prefix:
                    rel_path = full_path[len(root_prefix) + 1 :]
                rel_path = rel_path.strip("/")
                if not rel_path:
                    skipped_files += 1
                    continue

                zip_info = safe_to_info[full_path]
                file_bytes = zf.read(zip_info)
                object_name = f"{object_prefix}/{rel_path}"
                content_type = mimetypes.guess_type(rel_path)[0] or "application/octet-stream"
                _, public_url = upload_bytes(file_bytes, object_name=object_name, content_type=content_type)
                logical_name = expr_index.get(rel_path, {}).get("name")
                motion_group = motion_index.get(rel_path)

                assets.append(
                    Live2dModelAsset(
                        user_id=user_id,
                        package_key=pkg,
                        relative_path=rel_path,
                        file_name=PurePosixPath(rel_path).name,
                        asset_type=infer_asset_type(rel_path),
                        public_url=public_url,
                        object_key=object_name,
                        mime_type=content_type,
                        logical_name=logical_name,
                        motion_group=motion_group,
                        is_listed_in_model3=1 if rel_path in listed_files else 0,
                        is_entry_model=1 if rel_path == main_model3_path else 0,
                        file_size=len(file_bytes),
                        sort_order=sort_order,
                        remark="uploaded_zip",
                    )
                )
                sort_order += 1
                uploaded_files += 1
            src_zip_key = f"users/{user_id}/packages/{pkg}/__source__/{model_zip.filename}"
            upload_bytes(payload, object_name=src_zip_key, content_type="application/zip")
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="zip 文件损坏或格式非法") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"处理上传失败: {exc}") from exc

    deleted_rows = Live2dModelAssetRepository.delete_by_package_key(db, pkg, user_id)
    inserted_rows = 0
    for asset in assets:
        Live2dModelAssetRepository.insert(db, asset)
        inserted_rows += 1
    invalidate_live2d_catalog_cache(user_id, pkg)
    return Live2dModelZipUploadPublic(
        user_id=user_id,
        package_key=pkg,
        bucket=get_bucket_name(),
        object_prefix=f"users/{user_id}/packages/{pkg}",
        deleted_rows=deleted_rows,
        inserted_rows=inserted_rows,
        uploaded_files=uploaded_files,
        skipped_files=skipped_files,
    )