"""Read-only facts derived from journals already owned by the monolith.

No state is duplicated here. A successful preview-link delivery comes from the
Telegram effect journal; an optional chat destination comes from Radar's
existing link_targets read model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PreviewLinkDelivery:
    sent_at: datetime
    message_id: int | None
    destination_url: str | None
    target_chat_id: int | None


async def latest_preview_link_delivery(pool, user_id: int) -> PreviewLinkDelivery | None:
    row = await pool.fetchrow(
        """SELECT
             e.updated_at AS sent_at,
             NULLIF(e.result->>'message_id','')::BIGINT AS message_id,
             NULLIF(e.payload->>'text','') AS destination_url,
             lt.target_chat_id
           FROM telegram_effects e
           JOIN outbox_actions a ON a.id=e.outbox_action_id
           JOIN pv_message_steps s
             ON s.id=NULLIF(a.payload->>'step_id','')::BIGINT
           LEFT JOIN link_targets lt
             ON RTRIM(lt.url,'/')=RTRIM(COALESCE(e.payload->>'text',''),'/')
            AND lt.disposition='active'
           WHERE e.status='succeeded'
             AND a.module_id='pv_reply'
             AND a.action_type='send_linear_balloon'
             AND a.payload->>'peer'=$1
             AND s.step_key='link.preview'
           ORDER BY e.updated_at DESC
           LIMIT 1""",
        str(int(user_id)),
    )
    if row is None:
        return None
    message_id = row["message_id"]
    target_chat_id = row["target_chat_id"]
    return PreviewLinkDelivery(
        sent_at=row["sent_at"],
        message_id=None if message_id is None else int(message_id),
        destination_url=(str(row["destination_url"]) if row["destination_url"] else None),
        target_chat_id=(None if target_chat_id is None else int(target_chat_id)),
    )
