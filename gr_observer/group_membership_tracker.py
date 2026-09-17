"""Passive group-membership tracker for known PV leads.

This component is deliberately read-only with respect to Telegram: it consumes raw
membership updates and persists local facts. It never creates Outbox actions, never
calls Telegram methods and never sends a message.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from telethon import types, utils


DDL = """
CREATE TABLE IF NOT EXISTS pv_group_membership_events (
  event_key TEXT PRIMARY KEY,
  chat_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  transition TEXT NOT NULL CHECK(transition IN ('joined','left')),
  reason TEXT NOT NULL,
  actor_id BIGINT,
  invite_link TEXT,
  preview_link_match BOOLEAN NOT NULL DEFAULT FALSE,
  telegram_order BIGINT,
  event_at TIMESTAMPTZ NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS pv_group_membership_events_user_idx
ON pv_group_membership_events(user_id,event_at DESC);
CREATE INDEX IF NOT EXISTS pv_group_membership_events_chat_idx
ON pv_group_membership_events(chat_id,event_at DESC);

CREATE TABLE IF NOT EXISTS pv_group_membership_state (
  chat_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('joined','left')),
  first_joined_at TIMESTAMPTZ,
  last_joined_at TIMESTAMPTZ,
  last_left_at TIMESTAMPTZ,
  last_change_at TIMESTAMPTZ NOT NULL,
  join_count INTEGER NOT NULL DEFAULT 0,
  leave_count INTEGER NOT NULL DEFAULT 0,
  last_reason TEXT NOT NULL,
  last_actor_id BIGINT,
  last_invite_link TEXT,
  preview_group BOOLEAN NOT NULL DEFAULT FALSE,
  last_telegram_order BIGINT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(chat_id,user_id)
);
CREATE INDEX IF NOT EXISTS pv_group_membership_state_user_idx
ON pv_group_membership_state(user_id,status,updated_at DESC);
CREATE INDEX IF NOT EXISTS pv_group_membership_preview_idx
ON pv_group_membership_state(preview_group,status,updated_at DESC);
"""


@dataclass(frozen=True)
class MembershipChange:
    event_key: str
    chat_id: int
    user_id: int
    transition: str
    reason: str
    actor_id: int | None
    invite_link: str | None
    preview_link_match: bool
    telegram_order: int | None
    event_at: datetime


def _clean_link(value: str | None) -> str:
    return (value or "").strip().rstrip("/")


def _utc(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _participant_present(participant) -> bool:
    """Return membership presence without treating a restriction as an exit."""
    if participant is None:
        return False
    kind = type(participant).__name__
    if kind == "ChannelParticipantLeft":
        return False
    if kind == "ChannelParticipantBanned":
        # Telegram uses this constructor both for kicked/left users and for
        # members with granular restrictions. Only left=True means absent.
        return not bool(getattr(participant, "left", False))
    return True


def _invite_link(update) -> str | None:
    invite = getattr(update, "invite", None)
    value = getattr(invite, "link", None) if invite is not None else None
    clean = (value or "").strip()
    return clean or None


def _event_key(*parts) -> str:
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def membership_change_from_update(
    update,
    *,
    preview_link: str = "",
) -> MembershipChange | None:
    """Reduce supported raw Telegram updates to a membership transition.

    Admin/restriction changes that keep the person in the group are ignored.
    """
    name = type(update).__name__
    clean_preview = _clean_link(preview_link)

    if name == "UpdateChannelParticipant":
        before = _participant_present(getattr(update, "prev_participant", None))
        after = _participant_present(getattr(update, "new_participant", None))
        if before == after:
            return None
        transition = "joined" if after else "left"
        new_participant = getattr(update, "new_participant", None)
        if transition == "left" and type(new_participant).__name__ == "ChannelParticipantBanned":
            reason = "kicked_or_banned"
        else:
            reason = "channel_participant_update"
        channel_id = int(getattr(update, "channel_id"))
        chat_id = int(utils.get_peer_id(types.PeerChannel(channel_id)))
        user_id = int(getattr(update, "user_id"))
        actor_id = getattr(update, "actor_id", None)
        actor_id = int(actor_id) if actor_id is not None else None
        order = getattr(update, "qts", None)
        order = int(order) if order is not None else None
        invite_link = _invite_link(update)
        event_at = _utc(getattr(update, "date", None))
        key = _event_key(
            "channel",
            channel_id,
            user_id,
            order,
            int(event_at.timestamp()),
            transition,
        )
        return MembershipChange(
            event_key=key,
            chat_id=chat_id,
            user_id=user_id,
            transition=transition,
            reason=reason,
            actor_id=actor_id,
            invite_link=invite_link,
            preview_link_match=bool(
                clean_preview
                and invite_link
                and _clean_link(invite_link) == clean_preview
            ),
            telegram_order=order,
            event_at=event_at,
        )

    if name == "UpdateChatParticipantAdd":
        raw_chat_id = int(getattr(update, "chat_id"))
        chat_id = int(utils.get_peer_id(types.PeerChat(raw_chat_id)))
        user_id = int(getattr(update, "user_id"))
        actor_id = getattr(update, "inviter_id", None)
        actor_id = int(actor_id) if actor_id is not None else None
        order = getattr(update, "version", None)
        order = int(order) if order is not None else None
        event_at = _utc(getattr(update, "date", None))
        return MembershipChange(
            event_key=_event_key(
                "chat", raw_chat_id, user_id, order, int(event_at.timestamp()), "joined"
            ),
            chat_id=chat_id,
            user_id=user_id,
            transition="joined",
            reason="basic_group_add",
            actor_id=actor_id,
            invite_link=None,
            preview_link_match=False,
            telegram_order=order,
            event_at=event_at,
        )

    if name == "UpdateChatParticipantDelete":
        raw_chat_id = int(getattr(update, "chat_id"))
        chat_id = int(utils.get_peer_id(types.PeerChat(raw_chat_id)))
        user_id = int(getattr(update, "user_id"))
        order = getattr(update, "version", None)
        order = int(order) if order is not None else None
        event_at = datetime.now(timezone.utc)
        return MembershipChange(
            event_key=_event_key("chat", raw_chat_id, user_id, order, "left"),
            chat_id=chat_id,
            user_id=user_id,
            transition="left",
            reason="basic_group_delete",
            actor_id=None,
            invite_link=None,
            preview_link_match=False,
            telegram_order=order,
            event_at=event_at,
        )

    return None


class GroupMembershipTracker:
    """Shadow-only persistence of join/leave facts for already-known leads."""

    def __init__(self, pool, *, preview_link: str = "") -> None:
        self.pool = pool
        self.preview_link = preview_link or ""
        self._ready = False

    async def ensure_schema(self) -> None:
        if self._ready:
            return
        await self.pool.execute(DDL)
        self._ready = True

    async def _known_lead(self, user_id: int) -> bool:
        return bool(
            await self.pool.fetchval(
                """SELECT
                     EXISTS(SELECT 1 FROM contact_ledger WHERE user_id=$1)
                     OR EXISTS(SELECT 1 FROM pv_reply_contacts WHERE user_id=$1)""",
                int(user_id),
            )
        )

    async def observe(self, update) -> str:
        """Persist one raw membership update; never enqueue or send anything."""
        change = membership_change_from_update(update, preview_link=self.preview_link)
        if change is None:
            return "ignored"
        await self.ensure_schema()
        if not await self._known_lead(change.user_id):
            return "ignored_unknown_lead"

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """INSERT INTO pv_group_membership_events(
                         event_key,chat_id,user_id,transition,reason,actor_id,
                         invite_link,preview_link_match,telegram_order,event_at)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                       ON CONFLICT(event_key) DO NOTHING
                       RETURNING event_key""",
                    change.event_key,
                    change.chat_id,
                    change.user_id,
                    change.transition,
                    change.reason,
                    change.actor_id,
                    change.invite_link,
                    change.preview_link_match,
                    change.telegram_order,
                    change.event_at,
                )
                if not inserted:
                    return "duplicate"

                joined = change.transition == "joined"
                await conn.execute(
                    """INSERT INTO pv_group_membership_state(
                         chat_id,user_id,status,first_joined_at,last_joined_at,
                         last_left_at,last_change_at,join_count,leave_count,
                         last_reason,last_actor_id,last_invite_link,preview_group,
                         last_telegram_order,updated_at)
                       VALUES(
                         $1,$2,$3,
                         CASE WHEN $3='joined' THEN $4 ELSE NULL END,
                         CASE WHEN $3='joined' THEN $4 ELSE NULL END,
                         CASE WHEN $3='left' THEN $4 ELSE NULL END,
                         $4,
                         CASE WHEN $3='joined' THEN 1 ELSE 0 END,
                         CASE WHEN $3='left' THEN 1 ELSE 0 END,
                         $5,$6,$7,$8,$9,NOW()
                       )
                       ON CONFLICT(chat_id,user_id) DO UPDATE SET
                         status=EXCLUDED.status,
                         first_joined_at=COALESCE(
                           pv_group_membership_state.first_joined_at,
                           EXCLUDED.first_joined_at
                         ),
                         last_joined_at=COALESCE(
                           EXCLUDED.last_joined_at,
                           pv_group_membership_state.last_joined_at
                         ),
                         last_left_at=COALESCE(
                           EXCLUDED.last_left_at,
                           pv_group_membership_state.last_left_at
                         ),
                         last_change_at=EXCLUDED.last_change_at,
                         join_count=pv_group_membership_state.join_count
                           + CASE WHEN EXCLUDED.status='joined' THEN 1 ELSE 0 END,
                         leave_count=pv_group_membership_state.leave_count
                           + CASE WHEN EXCLUDED.status='left' THEN 1 ELSE 0 END,
                         last_reason=EXCLUDED.last_reason,
                         last_actor_id=EXCLUDED.last_actor_id,
                         last_invite_link=COALESCE(
                           EXCLUDED.last_invite_link,
                           pv_group_membership_state.last_invite_link
                         ),
                         preview_group=(
                           pv_group_membership_state.preview_group
                           OR EXCLUDED.preview_group
                         ),
                         last_telegram_order=EXCLUDED.last_telegram_order,
                         updated_at=NOW()""",
                    change.chat_id,
                    change.user_id,
                    change.transition,
                    change.event_at,
                    change.reason,
                    change.actor_id,
                    change.invite_link,
                    change.preview_link_match,
                    change.telegram_order,
                )
        return "joined" if joined else "left"
