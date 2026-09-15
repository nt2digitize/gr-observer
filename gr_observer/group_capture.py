"""Durable capture state shared by group bait replies and PV arrival cleanup.

This is infrastructure, not a new rib, queue, worker, Writer or Telegram client.
The group rib owns Telegram group mutations; the PV rib only records that the
captured user arrived privately and queues cleanup through the same Outbox.
"""

from __future__ import annotations

import json
from typing import Any

CAPTURE_REPLY_TTL_SECONDS = 45 * 60

CAPTURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_capture_events (
  chat_id BIGINT NOT NULL,
  bait_message_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  source_message_id BIGINT NOT NULL,
  username TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  reply_message_id BIGINT,
  sent_at TIMESTAMPTZ,
  pv_arrived_at TIMESTAMPTZ,
  cleanup_at TIMESTAMPTZ,
  cleaned_at TIMESTAMPTZ,
  cleanup_reason TEXT,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(chat_id,bait_message_id,user_id),
  UNIQUE(chat_id,source_message_id)
);
CREATE INDEX IF NOT EXISTS group_capture_user_open_idx
ON group_capture_events(user_id,cleaned_at,created_at DESC);
CREATE INDEX IF NOT EXISTS group_capture_cleanup_idx
ON group_capture_events(cleaned_at,cleanup_at);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class GroupCaptureStore:
    def __init__(self, pool) -> None:
        self.pool = pool

    async def ensure_schema(self) -> None:
        await self.pool.execute(CAPTURE_SCHEMA)

    async def queue_capture(
        self,
        *,
        module_id: str,
        chat_id: int,
        bait_message_id: int,
        source_message_id: int,
        user_id: int,
        username: str | None,
        display_name: str,
        first_name: str | None,
        last_name: str | None,
    ) -> bool:
        """Persist one fish per user/bait and queue exactly one P00 intent."""
        action_key = f"group-capture:{chat_id}:{bait_message_id}:{user_id}"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """INSERT INTO group_capture_events(
                           chat_id,bait_message_id,user_id,source_message_id,username,status
                       ) VALUES($1,$2,$3,$4,$5,'queued')
                       ON CONFLICT DO NOTHING RETURNING user_id""",
                    int(chat_id),
                    int(bait_message_id),
                    int(user_id),
                    int(source_message_id),
                    username,
                )
                if inserted is None:
                    return False

                # Reserve this inbound message inside the existing group event
                # namespace before the legacy reactive matcher runs. This blocks
                # two different automatic replies to the same human message.
                await conn.execute(
                    """INSERT INTO group_reply_events(
                           chat_id,message_id,user_id,status,created_at
                       ) VALUES($1,$2,$3,'captured',NOW())
                       ON CONFLICT(chat_id,message_id) DO NOTHING""",
                    int(chat_id),
                    int(source_message_id),
                    int(user_id),
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(
                           action_key,module_id,action_type,payload,available_at
                       ) VALUES($1,$2,'group_capture_contact_reply',$3::jsonb,NOW())
                       ON CONFLICT(action_key) DO NOTHING""",
                    action_key,
                    module_id,
                    _json(
                        {
                            "peer": int(chat_id),
                            "bait_message_id": int(bait_message_id),
                            "source_message_id": int(source_message_id),
                            "source_user_id": int(user_id),
                            "username": username,
                            "display_name": display_name,
                            "first_name": first_name,
                            "last_name": last_name,
                        }
                    ),
                )
                return True

    async def mark_sent(
        self,
        *,
        module_id: str,
        chat_id: int,
        bait_message_id: int,
        user_id: int,
        reply_message_id: int,
        ttl_seconds: int = CAPTURE_REPLY_TTL_SECONDS,
    ) -> None:
        """Persist the exact message we own, then schedule bounded cleanup."""
        ttl_seconds = max(60, int(ttl_seconds))
        cleanup_key = f"group-capture-cleanup:{chat_id}:{bait_message_id}:{user_id}:ttl"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """UPDATE group_capture_events SET
                         status='replied',reply_message_id=$4,sent_at=NOW(),
                         cleanup_at=NOW()+($5 * INTERVAL '1 second'),
                         last_error=NULL,updated_at=NOW()
                       WHERE chat_id=$1 AND bait_message_id=$2 AND user_id=$3""",
                    int(chat_id),
                    int(bait_message_id),
                    int(user_id),
                    int(reply_message_id),
                    ttl_seconds,
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(
                           action_key,module_id,action_type,payload,available_at
                       ) VALUES($1,$2,'group_capture_cleanup',$3::jsonb,
                                NOW()+($4 * INTERVAL '1 second'))
                       ON CONFLICT(action_key) DO NOTHING""",
                    cleanup_key,
                    module_id,
                    _json(
                        {
                            "peer": int(chat_id),
                            "bait_message_id": int(bait_message_id),
                            "source_user_id": int(user_id),
                            "reply_message_id": int(reply_message_id),
                            "reason": "ttl",
                        }
                    ),
                    ttl_seconds,
                )

    async def mark_error(
        self,
        *,
        chat_id: int,
        bait_message_id: int,
        user_id: int,
        status: str,
        error: BaseException,
    ) -> None:
        await self.pool.execute(
            """UPDATE group_capture_events SET status=$4,last_error=$5,updated_at=NOW()
               WHERE chat_id=$1 AND bait_message_id=$2 AND user_id=$3""",
            int(chat_id),
            int(bait_message_id),
            int(user_id),
            str(status),
            f"{type(error).__name__}: {error}"[:500],
        )

    async def queue_private_arrival_cleanup(
        self,
        *,
        module_id: str,
        user_id: int,
    ) -> int:
        """PV arrival accelerates cleanup without deleting the human's message."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """UPDATE group_capture_events SET
                         pv_arrived_at=COALESCE(pv_arrived_at,NOW()),updated_at=NOW()
                       WHERE user_id=$1 AND reply_message_id IS NOT NULL
                         AND cleaned_at IS NULL
                       RETURNING chat_id,bait_message_id,user_id,reply_message_id""",
                    int(user_id),
                )
                for row in rows:
                    action_key = (
                        f"group-capture-cleanup:{int(row['chat_id'])}:"
                        f"{int(row['bait_message_id'])}:{int(row['user_id'])}:pv"
                    )
                    await conn.execute(
                        """INSERT INTO outbox_actions(
                               action_key,module_id,action_type,payload,available_at
                           ) VALUES($1,$2,'group_capture_cleanup',$3::jsonb,NOW())
                           ON CONFLICT(action_key) DO NOTHING""",
                        action_key,
                        module_id,
                        _json(
                            {
                                "peer": int(row["chat_id"]),
                                "bait_message_id": int(row["bait_message_id"]),
                                "source_user_id": int(row["user_id"]),
                                "reply_message_id": int(row["reply_message_id"]),
                                "reason": "pv_arrived",
                            }
                        ),
                    )
                return len(rows)

    async def cleanup_allowed(
        self,
        *,
        chat_id: int,
        bait_message_id: int,
        user_id: int,
        reply_message_id: int,
    ) -> bool:
        return bool(
            await self.pool.fetchval(
                """SELECT EXISTS(
                     SELECT 1 FROM group_capture_events
                     WHERE chat_id=$1 AND bait_message_id=$2 AND user_id=$3
                       AND reply_message_id=$4 AND cleaned_at IS NULL
                   )""",
                int(chat_id),
                int(bait_message_id),
                int(user_id),
                int(reply_message_id),
            )
        )

    async def mark_cleaned(
        self,
        *,
        chat_id: int,
        bait_message_id: int,
        user_id: int,
        reason: str,
    ) -> None:
        await self.pool.execute(
            """UPDATE group_capture_events SET
                 status='cleaned',cleaned_at=COALESCE(cleaned_at,NOW()),
                 cleanup_reason=COALESCE(cleanup_reason,$4),updated_at=NOW()
               WHERE chat_id=$1 AND bait_message_id=$2 AND user_id=$3""",
            int(chat_id),
            int(bait_message_id),
            int(user_id),
            str(reason)[:100],
        )
