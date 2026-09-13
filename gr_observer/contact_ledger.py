"""Shared contact ledger and contact-saving effect helpers.

This is infrastructure shared by existing ribs, not a new Telegram client,
worker or architectural rib. The source module remains owner of each Outbox
intent; all Telegram mutations still execute through TelegramEffects.
"""

from __future__ import annotations

import json
from typing import Any

from telethon import functions, utils

from .outbox import AmbiguousExternalEffect


CONTACT_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_ledger (
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  display_name TEXT,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  first_private_contact_at TIMESTAMPTZ,
  last_private_contact_at TIMESTAMPTZ,
  first_source_chat_id BIGINT,
  last_source_chat_id BIGINT,
  last_source_message_id BIGINT,
  last_source_type TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS contact_account_state (
  account_user_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL REFERENCES contact_ledger(user_id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'observed'
    CHECK(status IN (
      'observed','save_queued','saved','already_saved',
      'save_failed','review','historical_unreachable'
    )),
  saved_at TIMESTAMPTZ,
  last_attempt_at TIMESTAMPTZ,
  last_error TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(account_user_id,user_id)
);
CREATE INDEX IF NOT EXISTS contact_account_state_status_idx
ON contact_account_state(account_user_id,status,updated_at DESC);
"""


def _safe_first_name(first_name: str | None, display_name: str | None, username: str | None) -> str:
    for value in (first_name, display_name, username):
        text = (value or "").strip()
        if text:
            return text[:64]
    return "Contato"


def _safe_last_name(last_name: str | None) -> str:
    return (last_name or "").strip()[:64]


class ContactLedger:
    def __init__(self, pool) -> None:
        self.pool = pool
        self.account_user_id: int | None = None

    async def on_connect(self, me) -> None:
        self.account_user_id = int(me.id)
        await self.ensure_schema()
        # If a module was administratively stopped after enqueue, the Writer
        # can conclusively fail that Outbox action before this state is updated.
        # Reset only save_queued rows that no longer have live work.
        await self.pool.execute(
            """UPDATE contact_account_state cas
               SET status='observed',updated_at=NOW()
               WHERE cas.account_user_id=$1
                 AND cas.status='save_queued'
                 AND NOT EXISTS(
                   SELECT 1 FROM outbox_actions oa
                   WHERE oa.action_type='ensure_contact_saved'
                     AND oa.status IN ('pending','processing')
                     AND (oa.payload->>'account_user_id')::BIGINT=cas.account_user_id
                     AND (oa.payload->>'peer')::BIGINT=cas.user_id
                 )""",
            self.account_user_id,
        )

    async def ensure_schema(self) -> None:
        # Compatibility guard. The schema remains additive and contains no
        # private-message body or phone-number discovery.
        await self.pool.execute(CONTACT_SCHEMA)

    async def observe(
        self,
        *,
        user_id: int,
        username: str | None,
        display_name: str | None,
        source_type: str,
        source_chat_id: int | None,
        source_message_id: int | None,
        private_contact: bool,
    ) -> None:
        await self.pool.execute(
            """INSERT INTO contact_ledger(
                   user_id,username,display_name,first_seen_at,last_seen_at,
                   first_private_contact_at,last_private_contact_at,
                   first_source_chat_id,last_source_chat_id,last_source_message_id,
                   last_source_type,created_at,updated_at
               ) VALUES(
                   $1,$2,$3,NOW(),NOW(),
                   CASE WHEN $7 THEN NOW() ELSE NULL END,
                   CASE WHEN $7 THEN NOW() ELSE NULL END,
                   $4,$4,$5,$6,NOW(),NOW()
               )
               ON CONFLICT(user_id) DO UPDATE SET
                   username=COALESCE(EXCLUDED.username,contact_ledger.username),
                   display_name=COALESCE(EXCLUDED.display_name,contact_ledger.display_name),
                   last_seen_at=NOW(),
                   first_private_contact_at=CASE
                     WHEN $7 THEN COALESCE(contact_ledger.first_private_contact_at,NOW())
                     ELSE contact_ledger.first_private_contact_at END,
                   last_private_contact_at=CASE
                     WHEN $7 THEN NOW() ELSE contact_ledger.last_private_contact_at END,
                   first_source_chat_id=COALESCE(contact_ledger.first_source_chat_id,$4),
                   last_source_chat_id=COALESCE($4,contact_ledger.last_source_chat_id),
                   last_source_message_id=COALESCE($5,contact_ledger.last_source_message_id),
                   last_source_type=$6,
                   updated_at=NOW()""",
            int(user_id),
            username,
            display_name,
            source_chat_id,
            source_message_id,
            source_type,
            bool(private_contact),
        )

    async def mark_already_saved(self, user_id: int) -> None:
        if self.account_user_id is None:
            return
        await self.pool.execute(
            """INSERT INTO contact_account_state(
                   account_user_id,user_id,status,saved_at,updated_at
               ) VALUES($1,$2,'already_saved',NOW(),NOW())
               ON CONFLICT(account_user_id,user_id) DO UPDATE SET
                   status='already_saved',saved_at=COALESCE(contact_account_state.saved_at,NOW()),
                   last_error=NULL,updated_at=NOW()""",
            self.account_user_id,
            int(user_id),
        )

    async def queue_save(
        self,
        *,
        module_id: str,
        user_id: int,
        username: str | None,
        display_name: str | None,
        first_name: str | None,
        last_name: str | None,
        source_type: str,
        source_chat_id: int | None,
        source_message_id: int | None,
        event_key: str,
        delay_seconds: int = 0,
        already_contact: bool = False,
    ) -> bool:
        if self.account_user_id is None:
            return False
        await self.observe(
            user_id=user_id,
            username=username,
            display_name=display_name,
            source_type=source_type,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            private_contact=source_type == "pv",
        )
        if already_contact:
            await self.mark_already_saved(user_id)
            return False

        account_id = self.account_user_id
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                status = await conn.fetchval(
                    """SELECT status FROM contact_account_state
                       WHERE account_user_id=$1 AND user_id=$2 FOR UPDATE""",
                    account_id,
                    int(user_id),
                )
                if status in {"saved", "already_saved", "save_queued", "review"}:
                    return False
                await conn.execute(
                    """INSERT INTO contact_account_state(
                           account_user_id,user_id,status,updated_at
                       ) VALUES($1,$2,'save_queued',NOW())
                       ON CONFLICT(account_user_id,user_id) DO UPDATE SET
                           status='save_queued',last_error=NULL,updated_at=NOW()""",
                    account_id,
                    int(user_id),
                )
                action_key = f"contact-save:{account_id}:{user_id}:{event_key}"
                inserted = await conn.fetchval(
                    """INSERT INTO outbox_actions(
                           action_key,module_id,action_type,payload,available_at
                       ) VALUES($1,$2,'ensure_contact_saved',$3::jsonb,
                                NOW()+($4 * INTERVAL '1 second'))
                       ON CONFLICT(action_key) DO NOTHING RETURNING id""",
                    action_key,
                    module_id,
                    json.dumps(
                        {
                            "peer": int(user_id),
                            "account_user_id": account_id,
                            "username": username,
                            "display_name": display_name,
                            "first_name": _safe_first_name(first_name, display_name, username),
                            "last_name": _safe_last_name(last_name),
                        },
                        ensure_ascii=False,
                    ),
                    max(0, int(delay_seconds)),
                )
                return inserted is not None

    async def save_with_effects(self, action: dict[str, Any], effects) -> dict[str, Any]:
        payload = action["payload"]
        user_id = int(payload["peer"])
        account_id = int(payload.get("account_user_id") or self.account_user_id or 0)
        if not account_id:
            raise RuntimeError("conta operadora indisponível para salvar contato")

        await self.pool.execute(
            """INSERT INTO contact_account_state(
                   account_user_id,user_id,status,last_attempt_at,updated_at
               ) VALUES($1,$2,'save_queued',NOW(),NOW())
               ON CONFLICT(account_user_id,user_id) DO NOTHING""",
            account_id,
            user_id,
        )
        state = await self.pool.fetchval(
            """SELECT status FROM contact_account_state
               WHERE account_user_id=$1 AND user_id=$2""",
            account_id,
            user_id,
        )
        if state in {"saved", "already_saved"}:
            return {"saved": True, "reason": "already_saved", "user_id": user_id}

        first_name = _safe_first_name(
            payload.get("first_name"), payload.get("display_name"), payload.get("username")
        )
        last_name = _safe_last_name(payload.get("last_name"))
        effect_key = f"{action['action_key']}:add-contact"

        async def add_contact():
            input_peer = await effects.client.get_input_entity(user_id)
            input_user = utils.get_input_user(input_peer)
            if effects.before_user_write is not None:
                await effects.before_user_write()
            await effects.client(
                functions.contacts.AddContactRequest(
                    id=input_user,
                    first_name=first_name,
                    last_name=last_name,
                    phone="",
                    add_phone_privacy_exception=False,
                )
            )
            return {"saved": True, "user_id": user_id}

        try:
            result = await effects.perform(
                effect_key,
                "add_contact",
                {
                    "user_id": str(user_id),
                    "share_phone": False,
                    "first_name": first_name,
                    "last_name": last_name,
                },
                add_contact,
            )
        except AmbiguousExternalEffect as exc:
            await self.pool.execute(
                """UPDATE contact_account_state SET
                     status='review',last_attempt_at=NOW(),last_error=$3,updated_at=NOW()
                   WHERE account_user_id=$1 AND user_id=$2""",
                account_id,
                user_id,
                f"{type(exc).__name__}: {exc}"[:500],
            )
            raise
        except Exception as exc:
            await self.pool.execute(
                """UPDATE contact_account_state SET
                     status='save_failed',last_attempt_at=NOW(),last_error=$3,updated_at=NOW()
                   WHERE account_user_id=$1 AND user_id=$2""",
                account_id,
                user_id,
                f"{type(exc).__name__}: {exc}"[:500],
            )
            raise

        await self.pool.execute(
            """UPDATE contact_account_state SET
                 status='saved',saved_at=COALESCE(saved_at,NOW()),
                 last_attempt_at=NOW(),last_error=NULL,updated_at=NOW()
               WHERE account_user_id=$1 AND user_id=$2""",
            account_id,
            user_id,
        )
        return {"saved": True, "user_id": user_id, **(result or {})}
