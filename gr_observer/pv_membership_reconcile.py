"""Read-only Telegram reconciliation for preview-group membership.

This closes the event-order gap where a person may join the preview group before
PV knows them as a lead. It reuses the already-connected Telegram USER client,
performs only read RPCs, and persists the observed current state into the same
membership read model used by the linear PV conversation.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from telethon import types, utils
from telethon.errors import UserNotParticipantError
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.messages import GetFullChatRequest


def _clean_link(value: str | None) -> str:
    return (value or "").strip().rstrip("/")


def _event_key(*parts) -> str:
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _participant_present(participant) -> bool:
    if participant is None:
        return False
    kind = type(participant).__name__
    if kind == "ChannelParticipantLeft":
        return False
    if kind == "ChannelParticipantBanned":
        return not bool(getattr(participant, "left", False))
    return True


async def _preview_chat_ids(pool, preview_link: str) -> tuple[int, ...]:
    link = _clean_link(preview_link)
    if not link:
        return ()
    rows = await pool.fetch(
        """SELECT DISTINCT target_chat_id
           FROM link_targets
           WHERE disposition='active'
             AND target_chat_id IS NOT NULL
             AND RTRIM(url,'/')=RTRIM($1,'/')
           ORDER BY target_chat_id""",
        link,
    )
    return tuple(int(row["target_chat_id"]) for row in rows)


async def _probe_channel_membership(client, chat_id: int, user_id: int, user_entity=None) -> bool:
    real_id, peer_cls = utils.resolve_id(int(chat_id))
    peer = peer_cls(real_id)

    if peer_cls is types.PeerChannel:
        channel = await client.get_input_entity(peer)
        participant = await client.get_input_entity(
            user_entity if user_entity is not None else types.PeerUser(int(user_id))
        )
        try:
            result = await client(GetParticipantRequest(channel, participant))
        except UserNotParticipantError:
            return False
        return _participant_present(getattr(result, "participant", None))

    if peer_cls is types.PeerChat:
        result = await client(GetFullChatRequest(int(real_id)))
        full_chat = getattr(result, "full_chat", None)
        participants = getattr(full_chat, "participants", None)
        entries = getattr(participants, "participants", None)
        if entries is None:
            raise RuntimeError("basic_group_participants_unavailable")
        return any(int(getattr(item, "user_id", 0) or 0) == int(user_id) for item in entries)

    raise RuntimeError("unsupported_preview_peer")


async def _persist_snapshot(pool, *, chat_id: int, user_id: int, present: bool) -> str:
    now = datetime.now(timezone.utc)
    new_status = "joined" if present else "left"

    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                """SELECT status,join_count,leave_count
                   FROM pv_group_membership_state
                   WHERE chat_id=$1 AND user_id=$2 FOR UPDATE""",
                int(chat_id),
                int(user_id),
            )

            # Absence without any historical row means "not known to have joined",
            # so keep BEFORE_JOIN semantics instead of inventing a leave event.
            if current is None and not present:
                return "absent_unknown"

            if current is not None and str(current["status"]) == new_status:
                await conn.execute(
                    """UPDATE pv_group_membership_state
                       SET preview_group=TRUE,
                           last_reason='current_membership_reconcile',
                           updated_at=NOW()
                       WHERE chat_id=$1 AND user_id=$2""",
                    int(chat_id),
                    int(user_id),
                )
                return f"confirmed_{new_status}"

            event_key = _event_key(
                "reconcile",
                int(chat_id),
                int(user_id),
                new_status,
                int(now.timestamp()),
            )
            await conn.execute(
                """INSERT INTO pv_group_membership_events(
                     event_key,chat_id,user_id,transition,reason,
                     preview_link_match,telegram_order,event_at)
                   VALUES($1,$2,$3,$4,'current_membership_reconcile',TRUE,NULL,$5)
                   ON CONFLICT(event_key) DO NOTHING""",
                event_key,
                int(chat_id),
                int(user_id),
                new_status,
                now,
            )

            if current is None:
                await conn.execute(
                    """INSERT INTO pv_group_membership_state(
                         chat_id,user_id,status,first_joined_at,last_joined_at,
                         last_left_at,last_change_at,join_count,leave_count,
                         last_reason,preview_group,last_telegram_order,updated_at)
                       VALUES(
                         $1,$2,'joined',$3,$3,NULL,$3,1,0,
                         'current_membership_reconcile',TRUE,NULL,NOW())""",
                    int(chat_id),
                    int(user_id),
                    now,
                )
                return "joined"

            await conn.execute(
                """UPDATE pv_group_membership_state
                   SET status=$3,
                       last_joined_at=CASE WHEN $3='joined' THEN $4 ELSE last_joined_at END,
                       last_left_at=CASE WHEN $3='left' THEN $4 ELSE last_left_at END,
                       last_change_at=$4,
                       join_count=join_count + CASE WHEN $3='joined' THEN 1 ELSE 0 END,
                       leave_count=leave_count + CASE WHEN $3='left' THEN 1 ELSE 0 END,
                       last_reason='current_membership_reconcile',
                       preview_group=TRUE,
                       last_telegram_order=NULL,
                       updated_at=NOW()
                   WHERE chat_id=$1 AND user_id=$2""",
                int(chat_id),
                int(user_id),
                new_status,
                now,
            )
            return new_status


async def reconcile_preview_membership(
    pool,
    client,
    *,
    preview_link: str,
    user_id: int,
    user_entity=None,
) -> str:
    """Read current Telegram membership and refresh the existing shadow model.

    No Telegram mutation is performed. If the preview destination cannot be
    resolved from the already-known link target, the function leaves state
    untouched and reports a neutral outcome.
    """
    chat_ids = await _preview_chat_ids(pool, preview_link)
    if not chat_ids:
        return "preview_unresolved"

    outcomes: list[str] = []
    for chat_id in chat_ids:
        present = await _probe_channel_membership(
            client,
            int(chat_id),
            int(user_id),
            user_entity=user_entity,
        )
        outcomes.append(
            await _persist_snapshot(
                pool,
                chat_id=int(chat_id),
                user_id=int(user_id),
                present=present,
            )
        )

    if any(value in {"joined", "confirmed_joined"} for value in outcomes):
        return "joined"
    if any(value == "left" for value in outcomes):
        return "left"
    if outcomes and all(value == "absent_unknown" for value in outcomes):
        return "before_join"
    return outcomes[-1] if outcomes else "preview_unresolved"
