"""Lightweight homage capture for the existing two-screens PV branch.

No media bytes are persisted. The existing inbox stores only derived metadata and
the Telegram message reference remains the source of truth.
"""

from __future__ import annotations

import json


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class PvHomageStore:
    def __init__(self, pool):
        self.pool = pool

    async def can_accept(self, user_id: int) -> bool:
        """Accept homage only soon after this user actually received two-screens media."""
        return bool(
            await self.pool.fetchval(
                """SELECT EXISTS(
                   SELECT 1 FROM pv_two_screens_sessions s
                   WHERE s.user_id=$1
                     AND s.status IN ('awaiting_choice','completed')
                     AND s.updated_at >= NOW()-INTERVAL '24 hours'
                     AND EXISTS(
                       SELECT 1 FROM outbox_actions a
                       WHERE a.module_id='pv_reply'
                         AND a.action_type='send_two_screens_photo'
                         AND a.payload->>'peer'=$2
                         AND a.status='succeeded'
                     )
                   )""",
                int(user_id),
                str(int(user_id)),
            )
        )

    async def record(
        self,
        *,
        event_key: str,
        user_id: int,
        message_id: int,
        media_kind: str,
        username: str | None,
        display_name: str,
    ) -> bool:
        """Persist only metadata/reference and refresh the existing PV contact clock."""
        if media_kind not in {"photo", "video", "gif"}:
            return False
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                session = await conn.fetchrow(
                    """SELECT status FROM pv_two_screens_sessions
                       WHERE user_id=$1
                         AND status IN ('awaiting_choice','completed')
                         AND updated_at >= NOW()-INTERVAL '24 hours'
                       FOR UPDATE""",
                    int(user_id),
                )
                if not session:
                    return False
                inserted = await conn.fetchval(
                    """INSERT INTO inbox_events(source,event_key,module_id,payload)
                       VALUES('telegram-user-homage',$1,'pv_reply',$2::jsonb)
                       ON CONFLICT DO NOTHING RETURNING event_key""",
                    event_key,
                    _json(
                        {
                            "kind": "two_screens_homage",
                            "sender_id": int(user_id),
                            "message_id": int(message_id),
                            "media_kind": media_kind,
                        }
                    ),
                )
                if not inserted:
                    return False
                await conn.execute(
                    """UPDATE pv_reply_contacts
                       SET username=$2,display_name=$3,last_inbound_at=NOW(),
                           last_inbound_message_id=$4,updated_at=NOW()
                       WHERE user_id=$1""",
                    int(user_id),
                    username,
                    display_name,
                    int(message_id),
                )
                return True
