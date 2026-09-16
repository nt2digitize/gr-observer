"""Small delivery helper for generic PV balloons.

Text-only rows deliberately keep the established runtime path. This helper is
used only when a row carries an operator-catalogued Telegram media reference,
so the single Writer and TelegramEffects remain the only mutation boundary.
"""

from __future__ import annotations

from .pv_message_steps import has_link


def _row_value(row, key: str):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def media_reference(row) -> tuple[int, int] | None:
    source_peer = _row_value(row, "media_source_peer")
    source_message_id = _row_value(row, "media_source_message_id")
    if source_peer is None or source_message_id is None:
        return None
    return int(source_peer), int(source_message_id)


def has_media(row) -> bool:
    return media_reference(row) is not None


async def send_media_balloon(
    *,
    effects,
    peer: int,
    row,
    text: str,
    effect_key: str,
) -> dict:
    """Deliver one catalogued media balloon through existing safe effects.

    The media copy is the primary message. Editable text, when present, is sent
    immediately afterwards as the balloon's companion text. This intentionally
    avoids adding a new Telegram mutation primitive or Outbox action type.
    """
    reference = media_reference(row)
    if reference is None:
        raise ValueError("balão sem referência de mídia")
    source_peer, source_message_id = reference
    media = await effects.send_catalogued_media(
        source_peer,
        source_message_id,
        int(peer),
        f"{effect_key}:media",
        spoiler=False,
        ttl_seconds=None,
    )
    result = {
        "media_message_id": media.get("message_id"),
        "media": media,
    }
    if text:
        sender = effects.send_text_preview if has_link(text) else effects.send_text
        companion = await sender(int(peer), text, f"{effect_key}:text")
        result["text_message_id"] = companion.get("message_id")
        result["text"] = companion
    return result
