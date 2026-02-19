"""Thin wrapper around the LINE Bot SDK v3."""

from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent

from app.config import LINE_CHANNEL_ACCESS_TOKEN

_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)


def get_api() -> MessagingApi:
    client = ApiClient(_config)
    return MessagingApi(client)


def reply_text(api: MessagingApi, event: MessageEvent, text: str) -> None:
    """Reply to a LINE event with a text message. Splits if > 5000 chars."""
    from linebot.v3.messaging import ReplyMessageRequest

    chunks = [text[i : i + 5000] for i in range(0, len(text), 5000)]
    api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=c) for c in chunks[:5]],
        )
    )


def push_text(api: MessagingApi, to: str, text: str) -> None:
    """Push a message to a group/user."""
    chunks = [text[i : i + 5000] for i in range(0, len(text), 5000)]
    api.push_message(
        PushMessageRequest(
            to=to,
            messages=[TextMessage(text=c) for c in chunks[:5]],
        )
    )


async def get_display_name(api: MessagingApi, group_id: str, user_id: str) -> str:
    """Get user display name in a group. Falls back to 'Unknown'."""
    try:
        profile = api.get_group_member_profile(group_id, user_id)
        return profile.display_name
    except Exception:
        return "Unknown"
