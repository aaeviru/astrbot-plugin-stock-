"""Pure authorization helpers: no AstrBot or database imports."""

from collections.abc import Iterable

from ..models import Owner


def read_owner(
    actor: Owner,
    raw_target: str = "",
    mentioned_ids: Iterable[str] = (),
    bot_id: str = "",
) -> Owner:
    """Resolve a public read target *within the actor's own group*.

    This helper MUST NOT be used for write operations. All writes always
    use the owner derived from the trusted event sender instead.
    """
    if not all((actor.platform_id, actor.group_id, actor.user_id)):
        raise ValueError("消息发送者身份不完整")

    mentions = []
    for raw_uid in mentioned_ids:
        uid = str(raw_uid).strip()
        if uid and uid not in ("all", "qq_official", str(bot_id)):
            mentions.append(uid)
    mentions = list(dict.fromkeys(mentions))
    if len(mentions) > 1:
        raise ValueError("一次只能查询一位群友")

    raw_target = str(raw_target or "").strip()
    if mentions:
        if raw_target and not raw_target.startswith("@") and raw_target != mentions[0]:
            raise ValueError("指令中指定了不同的查询目标")
        target = mentions[0]
    elif raw_target.startswith("@"):
        raise ValueError("无法识别该 @ 用户，请使用 /持仓 名单 后按 #编号 查询")
    else:
        target = raw_target or actor.user_id

    if (
        not target
        or len(target) > 256
        or any(ch.isspace() or ord(ch) < 32 for ch in target)
    ):
        raise ValueError("用户 ID 格式错误")

    return Owner(actor.platform_id, actor.group_id, target)
