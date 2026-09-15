"""Persistent operator kill switch for one private-chat user.

The kill switch is intentionally separate from Telegram's own block state. It
prevents this application from creating or executing more PV work for a user,
while preserving the Outbox/audit history and the single-Writer architecture.
"""

from __future__ import annotations

import json


PV_SUPPRESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS pv_suppressed_users (
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  reason TEXT NOT NULL DEFAULT 'admin',
  active BOOLEAN NOT NULL DEFAULT TRUE,
  suppressed_by BIGINT,
  suppressed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resumed_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS pv_suppressed_active_idx
ON pv_suppressed_users(active,updated_at DESC);
"""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def normalize_target(value: str) -> str:
    return (value or "").strip().lstrip("@").strip()


async def ensure_schema(pool) -> None:
    await pool.execute(PV_SUPPRESSION_SCHEMA)


async def is_suppressed(pool, user_id: int) -> bool:
    return bool(
        await pool.fetchval(
            """SELECT EXISTS(SELECT 1 FROM pv_suppressed_users
               WHERE user_id=$1 AND active IS TRUE)""",
            int(user_id),
        )
    )


async def resolve_pv_user(pool, target: str) -> int | None:
    token = normalize_target(target)
    if not token:
        return None
    if token.isdigit():
        return int(token)
    user_id = await pool.fetchval(
        """SELECT user_id FROM pv_reply_contacts
           WHERE LOWER(COALESCE(username,''))=LOWER($1)
           ORDER BY last_inbound_at DESC LIMIT 1""",
        token,
    )
    if user_id is None:
        user_id = await pool.fetchval(
            """SELECT user_id FROM contact_ledger
               WHERE LOWER(COALESCE(username,''))=LOWER($1)
               ORDER BY last_seen_at DESC LIMIT 1""",
            token,
        )
    return None if user_id is None else int(user_id)


async def suppress_pv_user(
    pool,
    user_id: int,
    *,
    suppressed_by: int | None,
    reason: str = "admin_panel",
) -> int:
    """Stop future PV work and neutralize pending Outbox actions for one user."""
    user_id = int(user_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            username = await conn.fetchval(
                """SELECT username FROM pv_reply_contacts WHERE user_id=$1
                   UNION ALL
                   SELECT username FROM contact_ledger WHERE user_id=$1
                   LIMIT 1""",
                user_id,
            )
            await conn.execute(
                """INSERT INTO pv_suppressed_users(
                     user_id,username,reason,active,suppressed_by,suppressed_at,resumed_at,updated_at)
                   VALUES($1,$2,$3,TRUE,$4,NOW(),NULL,NOW())
                   ON CONFLICT(user_id) DO UPDATE SET
                     username=COALESCE(EXCLUDED.username,pv_suppressed_users.username),
                     reason=EXCLUDED.reason,active=TRUE,
                     suppressed_by=EXCLUDED.suppressed_by,
                     suppressed_at=NOW(),resumed_at=NULL,updated_at=NOW()""",
                user_id,
                username,
                reason[:200],
                suppressed_by,
            )
            await conn.execute(
                """UPDATE pv_reply_contacts SET stage='stopped',stopped_at=NOW(),
                     next_followup_at=NULL,updated_at=NOW() WHERE user_id=$1""",
                user_id,
            )
            await conn.execute(
                """UPDATE pv_two_screens_sessions SET status='stopped',updated_at=NOW()
                   WHERE user_id=$1 AND status NOT IN ('completed','stopped')""",
                user_id,
            )
            await conn.execute(
                """INSERT INTO live_alert_subscriptions(user_id,status,responded_at)
                   VALUES($1,'unsubscribed',NOW())
                   ON CONFLICT(user_id) DO UPDATE SET status='unsubscribed',
                     responded_at=NOW(),updated_at=NOW()""",
                user_id,
            )
            await conn.execute(
                """UPDATE live_campaign_recipients SET stage='stopped',updated_at=NOW()
                   WHERE user_id=$1 AND stage IN (
                     'queued','awaiting','remarketing_sent','link_queued'
                   )""",
                user_id,
            )
            rows = await conn.fetch(
                """UPDATE outbox_actions SET status='succeeded',
                     result=$2::jsonb,lease_until=NULL,last_error=NULL,updated_at=NOW()
                   WHERE module_id='pv_reply' AND status='pending'
                     AND payload ? 'peer' AND payload->>'peer'=$1
                   RETURNING id""",
                str(user_id),
                _json({"sent": False, "reason": "admin_suppressed", "peer": user_id}),
            )
    return len(rows)


async def list_suppressed(pool, limit: int = 20):
    return await pool.fetch(
        """SELECT user_id,username,reason,suppressed_at FROM pv_suppressed_users
           WHERE active IS TRUE ORDER BY suppressed_at DESC LIMIT $1""",
        max(1, min(int(limit), 100)),
    )
