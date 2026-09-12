"""Small persistence contract for the PV photo-preference branch.

"Duas telas" is only the conversational name of this branch.  This module does
not control screens/camera/device state.  It only reduces a private reply to one
of three photo slots and creates a durable Outbox intent.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable

from .catalog import normalize_text

PHOTO_SLOTS = ("peitos", "buceta", "cu")
EXTRA_PHOTO_DELAY_RANGE_SECONDS = (25 * 60, 35 * 60)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def classify_photo_preference(text: str) -> str | None:
    """Return the best matching configured slot, or ``None`` when ambiguous."""
    normalized = normalize_text(text)
    aliases = {
        "peitos": ("peito", "peitos", "peitao", "peitão", "peitinho", "teta", "tetas"),
        "buceta": ("buceta", "xereca", "xana", "ppk", "vagina"),
        "cu": ("cu", "cuzinho", "rabinho", "rabo", "bunda", "bundinha", "anal"),
    }
    best: tuple[int, int, str] | None = None
    for slot, values in aliases.items():
        for alias in values:
            matches = list(re.finditer(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized))
            if not matches:
                continue
            last = matches[-1]
            candidate = (last.end(), len(alias), slot)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    return best[2] if best else None


def caption_for_slot(slot: str) -> str:
    return {
        "peitos": "Goza aí nesse peitão.",
        "buceta": "Goza aí nessa buceta.",
        "cu": "Goza aí nesse cuzinho.",
    }[slot]


def stable_delay_seconds(key: str, minimum: int, maximum: int) -> int:
    low, high = sorted((int(minimum), int(maximum)))
    width = (high - low) + 1
    return low + (int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") % width)


def choose_unsent_slot(user_id: int, message_id: int, remaining: Iterable[str]) -> str | None:
    choices = tuple(slot for slot in PHOTO_SLOTS if slot in set(remaining))
    if not choices:
        return None
    digest = hashlib.sha256(f"pv-photo:{user_id}:{message_id}".encode()).digest()
    return choices[int.from_bytes(digest[:8], "big") % len(choices)]


class PvPhotoFlowStore:
    """Durable choice/queue contract using existing Inbox/Outbox tables only."""

    def __init__(self, pool):
        self.pool = pool

    async def is_waiting_choice(self, user_id: int) -> bool:
        return bool(
            await self.pool.fetchval(
                """SELECT EXISTS(
                   SELECT 1 FROM pv_two_screens_sessions
                   WHERE user_id=$1 AND status='awaiting_choice')""",
                user_id,
            )
        )

    async def accept_choice(
        self,
        *,
        event_key: str,
        user_id: int,
        message_id: int,
        username: str | None,
        display_name: str,
        requested_slot: str | None,
        first_photo_delay_seconds: int,
    ) -> str:
        """Persist one inbound choice and queue at most one photo atomically."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """INSERT INTO inbox_events(source,event_key,module_id,payload)
                       VALUES('telegram-user',$1,'pv_reply',$2::jsonb)
                       ON CONFLICT DO NOTHING RETURNING event_key""",
                    event_key,
                    _json(
                        {
                            "kind": "two_screens_photo_choice",
                            "sender_id": user_id,
                            "message_id": message_id,
                            "requested_slot": requested_slot,
                        }
                    ),
                )
                if not inserted:
                    return "duplicate"

                row = await conn.fetchrow(
                    """SELECT status FROM pv_two_screens_sessions
                       WHERE user_id=$1 FOR UPDATE""",
                    user_id,
                )
                if not row or row["status"] != "awaiting_choice":
                    return "ignored"

                await conn.execute(
                    """UPDATE pv_reply_contacts SET username=$2,display_name=$3,
                       last_inbound_at=NOW(),last_inbound_message_id=$4,updated_at=NOW()
                       WHERE user_id=$1""",
                    user_id,
                    username,
                    display_name,
                    message_id,
                )

                used_rows = await conn.fetch(
                    """SELECT DISTINCT payload->>'slot' AS slot
                       FROM outbox_actions
                       WHERE module_id='pv_reply'
                         AND action_type='send_two_screens_photo'
                         AND payload->>'peer'=$1
                         AND status IN ('pending','processing','succeeded','review')""",
                    str(user_id),
                )
                used = {str(item["slot"]) for item in used_rows if item["slot"]}
                remaining = [slot for slot in PHOTO_SLOTS if slot not in used]
                if not remaining:
                    await conn.execute(
                        """UPDATE pv_two_screens_sessions
                           SET status='completed',completed_at=NOW(),updated_at=NOW()
                           WHERE user_id=$1""",
                        user_id,
                    )
                    return "completed"

                slot = requested_slot if requested_slot in remaining else None
                if slot is None:
                    slot = choose_unsent_slot(user_id, message_id, remaining)
                if slot is None:
                    return "completed"

                first_photo = not used
                delay_seconds = (
                    int(first_photo_delay_seconds)
                    if first_photo
                    else stable_delay_seconds(
                        f"pv-photo-extra:{user_id}:{message_id}",
                        *EXTRA_PHOTO_DELAY_RANGE_SECONDS,
                    )
                )
                final = len(remaining) == 1

                await conn.execute(
                    """UPDATE pv_two_screens_sessions
                       SET status='photo_queued',selected_slot=$2,updated_at=NOW()
                       WHERE user_id=$1""",
                    user_id,
                    slot,
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(
                       action_key,module_id,action_type,payload,available_at)
                       VALUES($1,'pv_reply','send_two_screens_photo',$2::jsonb,
                         NOW()+($3::double precision*INTERVAL '1 second'))
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"pv_reply:two-screens:photo:{user_id}:{message_id}",
                    _json({"peer": user_id, "slot": slot, "final": final}),
                    delay_seconds,
                )
                return "photo_queued"

    async def mark_photo_sent(self, user_id: int, slot: str, *, final: bool) -> bool:
        next_status = "completed" if final else "awaiting_choice"
        updated = await self.pool.fetchval(
            """UPDATE pv_two_screens_sessions
               SET status=$3,
                   completed_at=CASE WHEN $3='completed' THEN NOW() ELSE NULL END,
                   updated_at=NOW()
               WHERE user_id=$1 AND status='photo_queued' AND selected_slot=$2
               RETURNING user_id""",
            user_id,
            slot,
            next_status,
        )
        return updated is not None
