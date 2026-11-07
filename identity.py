"""AstrBot adapter boundary: turn QQ events into trusted Owner values."""

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent

from .models import Owner
from .services.access import read_owner


def get_actor(event: AstrMessageEvent) -> Owner:
    """Only group messages with a trustworthy sender may operate on holdings."""
    platform_id = str(event.get_platform_id() or "").strip()
    group_id = str(event.get_group_id() or "").strip()
    user_id = str(event.get_sender_id() or "").strip()
    if not group_id:
        raise ValueError("本插件仅支持群聊中的持仓操作")
    if not platform_id or not user_id:
        raise ValueError("无法确认发送者身份，操作已拒绝")
    return Owner(platform_id, group_id, user_id)


def resolve_read_target(event: AstrMessageEvent, raw_target: str, actor: Owner) -> Owner:
    """Only the read path may choose an owner different from the sender."""
    mentions = [str(part.qq) for part in (
        getattr(event.message_obj, "message", None) or []
    ) if isinstance(part, Comp.At)]
    return read_owner(actor, raw_target, mentions, str(event.get_self_id() or ""))
