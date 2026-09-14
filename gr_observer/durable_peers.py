"""Durable Telegram input-peer references for delayed user-session work.

The Railway runtime uses a StringSession, so delayed Outbox actions must not rely
on Telethon's in-memory entity cache surviving a restart. This mixin persists
the technical InputPeerUser reference observed on inbound private messages and
uses it as a fallback when Telethon can no longer resolve a numeric user id.

Legacy delayed actions that predate durable peer storage are not discarded when
the entity cache cannot resolve them. They are parked in the existing Outbox
until Telegram exposes that user again in a real inbound private event. No
Telegram sweep, second client, session, queue, worker or Writer is introduced.
"""

from __future__ import annotations

import logging

from telethon import types

log = logging.getLogger("gr-observer.durable-peers")

WAIT_PREFIX = "PeerReferenceUnavailable:"
WAIT_INTERVAL_SQL = "INTERVAL '3650 days'"
PEER_ERROR_FRAGMENT = "Could not find the input entity for PeerUser"

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


def _is_missing_peer_error(exc: BaseException | str) -> bool:
    text = str(exc)
    return PEER_ERROR_FRAGMENT in text or (
        "Could not find input entity for PeerUser" in text
    )


class DurablePeerObserverMixin:
    """Persist and reuse InputPeerUser references without changing topology."""

    _durable_peer_client_marker: int | None = None
    _legacy_peer_wait_storage_marker: int | None = None

    async def _ensure_durable_peer_schema(self) -> None:
        pool = getattr(self, "pool", None)
        if pool is not None:
            await pool.execute(DURABLE_PEER_SCHEMA)

    async def _wake_waiting_peer(self, user_id: int) -> None:
        """Release only persisted PV work for a peer just observed by Telegram."""
        pool = getattr(self, "pool", None)
        if pool is None:
            return
        await pool.execute(
            """UPDATE outbox_actions
               SET available_at=NOW(),
                   last_error=$2,
                   updated_at=NOW()
               WHERE module_id='pv_reply'
                 AND status='pending'
                 AND payload->>'peer'=$1
                 AND last_error LIKE $3""",
            str(int(user_id)),
            f"{WAIT_PREFIX} reference restored; retry queued",
            f"{WAIT_PREFIX}%",
        )

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
        await self._wake_waiting_peer(user_id)

    async def _recover_legacy_peer_waits(self, account_user_id: int) -> None:
        """Park old PeerUser failures and wake only those already rehydratable.

        Earlier versions recorded a cache-resolution ValueError as a definitive
        rejection even though no Telegram mutation had been attempted. Reclassify
        only that exact local failure. The old effect result is retained as audit
        history; status/last_error make the effect safely executable again.
        """
        pool = getattr(self, "pool", None)
        if pool is None:
            return
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """UPDATE telegram_effects effects
                       SET status='processing',
                           last_error=$1,
                           updated_at=NOW()
                       FROM outbox_actions actions
                       WHERE effects.outbox_action_id=actions.id
                         AND actions.module_id='pv_reply'
                         AND actions.status='failed'
                         AND actions.last_error LIKE $2
                         AND effects.status='succeeded'
                         AND effects.result->>'_effect_outcome'='rejected'
                         AND effects.result->>'error_type'='ValueError'
                         AND effects.result->>'error' LIKE $3""",
                    f"{WAIT_PREFIX} legacy rejection parked for rehydration",
                    f"%{PEER_ERROR_FRAGMENT}%",
                    f"%{PEER_ERROR_FRAGMENT}%",
                )
                await conn.execute(
                    f"""UPDATE outbox_actions
                        SET status='pending',
                            available_at=NOW()+{WAIT_INTERVAL_SQL},
                            lease_until=NULL,
                            last_error=$1,
                            updated_at=NOW()
                        WHERE module_id='pv_reply'
                          AND status='failed'
                          AND last_error LIKE $2""",
                    f"{WAIT_PREFIX} waiting for observed Telegram entity",
                    f"%{PEER_ERROR_FRAGMENT}%",
                )
                await conn.execute(
                    """UPDATE outbox_actions actions
                       SET available_at=NOW(),
                           last_error=$2,
                           updated_at=NOW()
                       WHERE actions.module_id='pv_reply'
                         AND actions.status='pending'
                         AND actions.last_error LIKE $3
                         AND (actions.payload->>'peer') ~ '^[0-9]+$'
                         AND EXISTS(
                           SELECT 1 FROM telegram_peer_refs peers
                           WHERE peers.account_user_id=$1
                             AND peers.peer_id=(actions.payload->>'peer')::BIGINT
                             AND peers.peer_type='user'
                         )""",
                    int(account_user_id),
                    f"{WAIT_PREFIX} durable reference already available; retry queued",
                    f"{WAIT_PREFIX}%",
                )

    async def _install_legacy_peer_wait_storage(self) -> None:
        """Teach the existing Storage instance to park missing-peer actions."""
        storage = getattr(self, "storage", None)
        pool = getattr(self, "pool", None)
        me = getattr(self, "me", None)
        if storage is None or pool is None or me is None:
            return
        marker = id(storage)
        if self._legacy_peer_wait_storage_marker == marker:
            return

        original_begin_effect = storage.begin_effect
        original_fail_action = storage.fail_action

        async def begin_effect(*, effect_key, action_id, effect_type, payload):
            row = await pool.fetchrow(
                "SELECT status,last_error FROM telegram_effects WHERE effect_key=$1",
                effect_key,
            )
            if row and str(row["last_error"] or "").startswith(WAIT_PREFIX):
                await pool.execute(
                    """UPDATE telegram_effects
                       SET status='processing', attempts=attempts+1,
                           last_error=NULL, updated_at=NOW()
                       WHERE effect_key=$1""",
                    effect_key,
                )
                return "execute", None
            return await original_begin_effect(
                effect_key=effect_key,
                action_id=action_id,
                effect_type=effect_type,
                payload=payload,
            )

        async def fail_action(action_id: int, exc: BaseException) -> None:
            if not _is_missing_peer_error(exc):
                return await original_fail_action(action_id, exc)
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """UPDATE telegram_effects
                           SET status='processing', last_error=$2, updated_at=NOW()
                           WHERE outbox_action_id=$1""",
                        int(action_id),
                        f"{WAIT_PREFIX} entity absent; no Telegram mutation attempted",
                    )
                    await conn.execute(
                        f"""UPDATE outbox_actions
                            SET status='pending',
                                available_at=NOW()+{WAIT_INTERVAL_SQL},
                                lease_until=NULL,
                                last_error=$2,
                                updated_at=NOW()
                            WHERE id=$1""",
                        int(action_id),
                        f"{WAIT_PREFIX} waiting for observed Telegram entity",
                    )
            log.info(
                "Ação PV aguardando reidratação de entidade action_id=%s",
                action_id,
            )

        storage.begin_effect = begin_effect
        storage.fail_action = fail_action
        self._legacy_peer_wait_storage_marker = marker
        await self._recover_legacy_peer_waits(int(me.id))

    async def _install_durable_peer_resolver(self) -> None:
        client = getattr(self, "user", None)
        me = getattr(self, "me", None)
        pool = getattr(self, "pool", None)
        if client is None or me is None or pool is None:
            return
        marker = id(client)
        if self._durable_peer_client_marker == marker:
            await self._install_legacy_peer_wait_storage()
            return

        await self._ensure_durable_peer_schema()
        await self._install_legacy_peer_wait_storage()
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
