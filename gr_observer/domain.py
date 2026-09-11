"""Small Telegram/domain helpers shared by modules."""

from __future__ import annotations

from datetime import datetime, timezone

from telethon import types


def now():
    return datetime.now(timezone.utc)


def title_of(entity) -> str:
    return (
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or str(entity.id)
    )


def kind_of(entity) -> str:
    if isinstance(entity, types.Channel) and entity.broadcast:
        return "channel"
    if isinstance(entity, (types.Channel, types.Chat)):
        return "group"
    # Lightweight fakes can state their kind without importing Telethon types.
    return getattr(entity, "kind", "private")


def source_label(title, chat_id) -> str:
    return f"{title} ({chat_id})" if title else str(chat_id)
