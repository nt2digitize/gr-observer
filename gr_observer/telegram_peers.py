"""Durable Telegram peer resolution for the single USER session.

The client type and persistence component are explicit dependencies. No method on
TelegramClient or Storage is replaced after construction.
"""

from __future__ import annotations

import logging

from telethon import TelegramClient, types

log = logging.getLogger("gr-observer.telegram-peers")

WAIT_PREFIX = "PeerReferenceUnavailable:"
WAIT_INTERVAL_SQL = "INTERVAL '3650 days'"
PEER_ERROR_FRAGMENTS = (
    "Could not find the input entity for PeerUser",
    "Could not find input entity for PeerUser",
)

PEER_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_peer_refs (
  account_user_id BIGINT NOT NULL,
  peer_id BIGINT NOT NULL,
  peer_type TEXT NOT NULL DEFAULT 'user' CHECK(peer_type='user'),
  access_hash BIGINT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(account_user_id,peer_id)
);
CREATE INDEX IF NOT EXISTS telegram_peer_refs_seen_idx
ON telegram_peer_refs(account_user_id,last_seen_at DESC);
"""


class PeerReferenceUnavailable(BaseException):
    """Control-flow signal: no Telegram mutation was attempted for this peer."""

    def __init__(self, peer_id: int):
        self.peer_id = int(peer_id)
        super().__init__(f"{WAIT_PREFIX} {self.peer_id}")


def is_missing_peer_error(value: BaseException | str) -> bool:
    text = str(value)
    return any(fragment in text for fragment in PEER_ERROR_FRAGMENTS)


class DurablePeerStore:
    def __init__(self, pool):
        self.pool = pool
        self.account_user_id: int | None = None

    async def ensure_schema(self) -> None:
        await self.pool.execute(PEER_SCHEMA)

    async def bind_account(self, account_user_id: int) -> None:
        await self.ensure_schema()
        self.account_user_id = int(account_user_id)
        await self.recover_legacy_waits()

    async def resolve_user(self, peer_id: int):
        if self.account_user_id is None:
            return None
        row = await self.pool.fetchrow(
            """SELECT access_hash FROM telegram_peer_refs
               WHERE account_user_id=$1 AND peer_id=$2 AND peer_type='user'""",
            self.account_user_id,
            int(peer_id),
        )
        if not row:
            return None
        access_hash = int(row["access_hash"] or 0)
        if access_hash == 0:
            return None
        return types.InputPeerUser(user_id=int(peer_id), access_hash=access_hash)

    async def remember_event(self, event) -> None:
        if self.account_user_id is None:
            return
        if getattr(event, "out", False) or not getattr(event, "is_private", False):
            return
        input_sender = getattr(event, "input_sender", None)
        if input_sender is None:
            try:
                input_sender = await event.get_input_sender()
            except Exception:
                return
        if not isinstance(input_sender, types.InputPeerUser):
            return
        peer_id = int(getattr(input_sender, "user_id", 0) or 0)
        access_hash = int(getattr(input_sender, "access_hash", 0) or 0)
        if peer_id <= 0 or access_hash == 0:
            return
        await self.pool.execute(
            """INSERT INTO telegram_peer_refs(
                   account_user_id,peer_id,peer_type,access_hash,first_seen_at,last_seen_at
               ) VALUES($1,$2,'user',$3,NOW(),NOW())
               ON CONFLICT(account_user_id,peer_id) DO UPDATE SET
                 access_hash=EXCLUDED.access_hash,last_seen_at=NOW()""",
            self.account_user_id,
            peer_id,
            access_hash,
        )
        await self.wake_peer(peer_id)

    async def wake_peer(self, peer_id: int) -> None:
        await self.pool.execute(
            """UPDATE outbox_actions SET available_at=NOW(),last_error=$2,updated_at=NOW()
               WHERE module_id='pv_reply' AND status='pending'
                 AND payload->>'peer'=$1 AND last_error LIKE $3""",
            str(int(peer_id)),
            f"{WAIT_PREFIX} reference restored; retry queued",
            f"{WAIT_PREFIX}%",
        )

    async def park_action(self, action_id: int, peer_id: int) -> None:
        await self.pool.execute(
            f"""UPDATE outbox_actions SET status='pending',
                 available_at=NOW()+{WAIT_INTERVAL_SQL},lease_until=NULL,
                 last_error=$2,updated_at=NOW() WHERE id=$1""",
            int(action_id),
            f"{WAIT_PREFIX} {int(peer_id)} waiting for observed Telegram entity",
        )

    async def park_effect(self, effect_key: str, action_id: int, peer_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """UPDATE telegram_effects SET status='processing',last_error=$2,updated_at=NOW()
                       WHERE effect_key=$1""",
                    effect_key,
                    f"{WAIT_PREFIX} {int(peer_id)}; no Telegram mutation attempted",
                )
                await conn.execute(
                    f"""UPDATE outbox_actions SET status='pending',
                         available_at=NOW()+{WAIT_INTERVAL_SQL},lease_until=NULL,
                         last_error=$2,updated_at=NOW() WHERE id=$1""",
                    int(action_id),
                    f"{WAIT_PREFIX} {int(peer_id)} waiting for observed Telegram entity",
                )

    async def reopen_waiting_effect(self, effect_key: str) -> bool:
        updated = await self.pool.fetchval(
            """UPDATE telegram_effects SET status='processing',attempts=attempts+1,
                 last_error=NULL,updated_at=NOW()
               WHERE effect_key=$1 AND last_error LIKE $2 RETURNING effect_key""",
            effect_key,
            f"{WAIT_PREFIX}%",
        )
        return updated is not None

    async def recover_legacy_waits(self) -> None:
        """Reclassify only the historical local PeerUser-resolution failure."""
        if self.account_user_id is None:
            return
        fragment = PEER_ERROR_FRAGMENTS[0]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """UPDATE telegram_effects effects SET
                         status='processing',last_error=$1,updated_at=NOW()
                       FROM outbox_actions actions
                       WHERE effects.outbox_action_id=actions.id
                         AND actions.module_id='pv_reply' AND actions.status='failed'
                         AND actions.last_error LIKE $2
                         AND effects.status='succeeded'
                         AND effects.result->>'_effect_outcome'='rejected'
                         AND effects.result->>'error_type'='ValueError'
                         AND effects.result->>'error' LIKE $2""",
                    f"{WAIT_PREFIX} legacy rejection parked for rehydration",
                    f"%{fragment}%",
                )
                await conn.execute(
                    f"""UPDATE outbox_actions SET status='pending',
                         available_at=NOW()+{WAIT_INTERVAL_SQL},lease_until=NULL,
                         last_error=$1,updated_at=NOW()
                       WHERE module_id='pv_reply' AND status='failed'
                         AND last_error LIKE $2""",
                    f"{WAIT_PREFIX} waiting for observed Telegram entity",
                    f"%{fragment}%",
                )
                await conn.execute(
                    """UPDATE outbox_actions actions SET available_at=NOW(),
                         last_error=$2,updated_at=NOW()
                       WHERE actions.module_id='pv_reply' AND actions.status='pending'
                         AND actions.last_error LIKE $3
                         AND (actions.payload->>'peer') ~ '^[0-9]+$'
                         AND EXISTS(
                           SELECT 1 FROM telegram_peer_refs peers
                           WHERE peers.account_user_id=$1
                             AND peers.peer_id=(actions.payload->>'peer')::BIGINT
                         )""",
                    self.account_user_id,
                    f"{WAIT_PREFIX} durable reference already available; retry queued",
                    f"{WAIT_PREFIX}%",
                )


class DurableTelegramClient(TelegramClient):
    """Telegram client with durable positive user-id fallback resolution."""

    def __init__(self, *args, peer_store: DurablePeerStore | None = None, **kwargs):
        self.peer_store = peer_store
        super().__init__(*args, **kwargs)

    async def get_input_entity(self, peer):
        try:
            return await super().get_input_entity(peer)
        except ValueError as original_error:
            if isinstance(peer, types.PeerUser):
                peer_id = int(peer.user_id)
            elif isinstance(peer, int) and peer > 0:
                peer_id = int(peer)
            else:
                raise
            if self.peer_store is not None:
                durable = await self.peer_store.resolve_user(peer_id)
                if durable is not None:
                    return durable
            log.info("Telegram peer reference unavailable peer=%s", peer_id)
            raise PeerReferenceUnavailable(peer_id) from original_error
