"""用户长时间无互动时的随机话题闲聊（复用定时关怀 WebSocket 投递链路）。"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime
from typing import Dict

from live2d_db.entities import RemindTrigger

TRIGGER_TYPE_IDLE_CHITCHAT = "随机闲聊"

# 话题种子：投递 LLM 据此生成 1～3 句口语化开场
_TOPIC_SEEDS: tuple[str, ...] = (
    "最近有没有在追的剧、番或综艺",
    "今天过得怎么样、心情如何",
    "最近喜欢听什么歌或什么类型的音乐",
    "周末或假期有什么打算",
    "最近有没有吃到或想尝试的好吃的",
    "学习或工作上有没有一件小事想吐槽或分享",
    "最近天气变来变去，出门习惯带伞吗",
    "有没有想练的技能或兴趣爱好",
    "睡前一般会刷手机还是看书放松",
    "如果突然多出一小时自由时间你会用来做什么",
    "最近有没有印象深刻的梦或奇怪的想法",
    "校园里或通勤路上有没有撞见有趣的事",
)

_user_last_activity: Dict[int, float] = {}
_user_idle_chitchat_sent: Dict[int, bool] = {}


def idle_chitchat_enabled() -> bool:
    raw = (os.getenv("IDLE_CHITCHAT_ENABLED") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def idle_chitchat_seconds() -> int:
    try:
        n = int((os.getenv("IDLE_CHITCHAT_IDLE_SECONDS") or "300").strip() or "300")
    except ValueError:
        n = 300
    return max(60, n)


def touch_user_activity(user_id: int) -> None:
    """用户主动发消息或刚建立对话连接时调用，重置空闲计时。"""
    if user_id < 1:
        return
    _user_last_activity[user_id] = time.monotonic()
    _user_idle_chitchat_sent[user_id] = False


def mark_idle_chitchat_sent(user_id: int) -> None:
    _user_idle_chitchat_sent[user_id] = True


def should_offer_idle_chitchat(user_id: int) -> bool:
    if not idle_chitchat_enabled() or user_id < 1:
        return False
    if _user_idle_chitchat_sent.get(user_id):
        return False
    last = _user_last_activity.get(user_id)
    if last is None:
        return False
    return (time.monotonic() - last) >= float(idle_chitchat_seconds())


def pick_random_topic_seed() -> str:
    return random.choice(_TOPIC_SEEDS)


def build_idle_chitchat_trigger(user_id: int) -> RemindTrigger:
    topic = pick_random_topic_seed()
    content = (
        f"随机话题方向：{topic}。"
        "请用轻松、自然的口吻主动找用户闲聊，可以提问或分享想法，"
        "不要像定时提醒、通知或客服话术。"
    )
    return RemindTrigger(
        user_id=user_id,
        trigger_type=TRIGGER_TYPE_IDLE_CHITCHAT,
        trigger_time=datetime.now(),
        session_id=None,
        trigger_content=content,
        is_triggered=0,
    )
