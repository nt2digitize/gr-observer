"""Durable Telegram input-peer references for delayed user-session work.

The Railway runtime uses a StringSession, so delayed Outbox actions must not rely
on Telethon's in-memory entity cache surviving a restart.  This mixin persists
only the technical InputPeerUser reference observed on inbound private messages
and uses it as a fallback when Telethon can no longer resolve a numeric user id.

It does not create another Telegram client, session, worker, queue or Writer.
"""

from __future__ import annotations

import logging

from telethon import types

log = logging.getLogger("gr-observer.durable-peers")

DURABLE_PEER_SCHEMA = """
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


class DurablePeerObserverMixin:
    """Persist and reuse InputPeerUser references without changing topology."""

    _durable_peer_client_marker: int | None = None

    async def _ensure_durable_peer_schema(self) -> None:
        pool = getattr(self, "pool", None)
        if pool is not None:
            await pool.execute(DURABLE_PEER_SCHEMA)

    async def _remember_event_peer(self, event) -> None:
        """Remember only an inbound private sender already exposed by Telegram."""
        if getattr(event, "out", False) or not getattr(event, "is_private", False):
            return
        me = getattr(self, "me", None)
        pool = getattr(self, "pool", None)
        if me is None or pool is None:
            return

        input_sender = getattr(event, "input_sender", None)
        if input_sender is None:
            try:
                input_sender = await event.get_input_sender()
            except Exception:
                return
        if not isinstance(input_sender, types.InputPeerUser):
            return
        access_hash = int(getattr(input_sender, "access_hash", 0) or 0)
        user_id = int(getattr(input_sender, "user_id", 0) or 0)
        if user_id <= 0 or access_hash == 0:
            return

        await pool.execute(
            """INSERT INTO telegram_peer_refs(
                   account_user_id,peer_id,peer_type,access_hash,first_seen_at,last_seen_at
               ) VALUES($1,$2,'user',$3,NOW(),NOW())
               ON CONFLICT(account_user_id,peer_id) DO UPDATE SET
                   access_hash=EXCLUDED.access_hash,peer_type='user',last_seen_at=NOW()""",
            int(me.id),
            user_id,
            access_hash,
        )

    async def _install_durable_peer_resolver(self) -> None:
        client = getattr(self, "user", None)
        me = getattr(self, "me", None)
        pool = getattr(self, "pool", None)
        if client is None or me is None or pool is None:
            return
        marker = id(client)
        if self._durable_peer_client_marker == marker:
            return

        await self._ensure_durable_peer_schema()
        original_get_input_entity = client.get_input_entity
        account_user_id = int(me.id)

        async def durable_get_input_entity(peer):
            try:
                return await original_get_input_entity(peer)
            except ValueError as original_error:
                if isinstance(peer, types.PeerUser):
                    peer_id = int(peer.user_id)
                elif isinstance(peer, int) and peer > 0:
                    peer_id = int(peer)
                else:
                    raise
                try:
                    row = await pool.fetchrow(
                        """SELECT access_hash FROM telegram_peer_refs
                           WHERE account_user_id=$1 AND peer_id=$2 AND peer_type='user'""",
                        account_user_id,
                        peer_id,
                    )
                except Exception as exc:
                    log.warning(
                        "Falha ao consultar referência Telegram durável peer=%s erro=%s",
                        peer_id,
                        type(exc).__name__,
                    )
                    raise original_error
                if not row:
                    raise original_error
                access_hash = int(row["access_hash"] or 0)
                if access_hash == 0:
                    raise original_error
                return types.InputPeerUser(user_id=peer_id, access_hash=access_hash)

        client.get_input_entity = durable_get_input_entity
        self._durable_peer_client_marker = marker

    async def _connect_module(self, module_id: str) -> None:
        # user_runtime creates the single client and Writer before modules are
        # connected. Install the resolver here, before writer.run() is started.
        await self._install_durable_peer_resolver()
        return await super()._connect_module(module_id)

    async def guarded_dispatch(self, event) -> None:
        # Persistence is best-effort observability/infrastructure. A database
        # hiccup here must never suppress the business event itself.
        try:
            await self._install_durable_peer_resolver()
            await self._remember_event_peer(event)
        except Exception as exc:
            log.warning(
                "Não foi possível persistir referência Telegram do evento erro=%s",
                type(exc).__name__,
            )
        return await super().guarded_dispatch(event)
