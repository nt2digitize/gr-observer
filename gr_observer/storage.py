"""Persistence boundary, including transactional inbox/outbox operations."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .schema import SCHEMA


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decoded(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def run_id_for(trigger_key: str) -> str:
    return hashlib.sha256(trigger_key.encode("utf-8")).hexdigest()[:24]


class Storage:
    def __init__(self, pool):
        self.pool = pool

    async def initialize(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)

    async def recover_interrupted(self) -> None:
        """Quarantine work left between database and Telegram by a dead process."""
        await self.pool.execute(
            """UPDATE outbox_actions SET status='review', lease_until=NULL,
               last_error=COALESCE(last_error,'Processo interrompido; revisão necessária'),
               updated_at=NOW() WHERE status='processing'"""
        )
        await self.pool.execute(
            """UPDATE telegram_effects SET status='review',
               last_error=COALESCE(last_error,'Efeito interrompido; reconciliar antes de repetir'),
               updated_at=NOW() WHERE status='processing'"""
        )
        await self.pool.execute(
            """UPDATE module_runs SET status='review', finished_at=NOW(),
               error=COALESCE(error,'Execução interrompida; revisão necessária')
               WHERE status='running'"""
        )

    async def module_states(self) -> dict[str, dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT module_id, enabled, reason, config, updated_at FROM module_control ORDER BY module_id"
        )
        return {
            row["module_id"]: {
                "enabled": row["enabled"],
                "reason": row["reason"],
                "config": _decoded(row["config"]) or {},
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    async def set_module_state(
        self, module_id: str, enabled: bool, reason: str
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO module_control(module_id,enabled,reason,updated_at)
                       VALUES($1,$2,$3,NOW()) ON CONFLICT(module_id) DO UPDATE SET
                       enabled=EXCLUDED.enabled, reason=EXCLUDED.reason, updated_at=NOW()""",
                    module_id,
                    enabled,
                    reason,
                )
                if module_id == "radar":
                    await conn.execute(
                        """UPDATE observer_control SET enabled=$1, reason=$2, updated_at=NOW()
                           WHERE singleton=TRUE""",
                        enabled,
                        reason,
                    )

    async def set_module_reason(self, module_id: str, reason: str) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE module_control SET reason=$2, updated_at=NOW() WHERE module_id=$1",
                    module_id,
                    reason,
                )
                if module_id == "radar":
                    await conn.execute(
                        """UPDATE observer_control SET reason=$1, updated_at=NOW()
                           WHERE singleton=TRUE""",
                        reason,
                    )

    async def stored_controller_id(self) -> int | None:
        raw = await self.pool.fetchval(
            "SELECT config->>'controller_id' FROM module_control WHERE module_id='botson'"
        )
        return int(raw) if raw and str(raw).lstrip("-").isdigit() else None

    async def save_controller_id(self, controller_id: int) -> None:
        await self.pool.execute(
            """UPDATE module_control SET
               config=jsonb_set(config,'{controller_id}',to_jsonb($1::bigint),TRUE),
               updated_at=NOW() WHERE module_id='botson'""",
            controller_id,
        )

    async def pair_and_enqueue(
        self,
        *,
        event_key: str,
        controller_id: int,
        reply_peer: int,
        reply_text: str,
    ) -> str:
        """Persist pairing and its Telegram acknowledgement in one transaction."""
        action_key = f"botson:reply:{event_key}:paired"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """INSERT INTO inbox_events(source,event_key,module_id,payload)
                       VALUES('telegram-user',$1,'botson',$2::jsonb)
                       ON CONFLICT DO NOTHING RETURNING event_key""",
                    event_key,
                    _json({"kind": "pair", "sender_id": controller_id}),
                )
                if not inserted:
                    return "duplicate"
                await conn.execute(
                    """UPDATE module_control SET
                       config=jsonb_set(config,'{controller_id}',to_jsonb($1::bigint),TRUE),
                       updated_at=NOW() WHERE module_id='botson'""",
                    controller_id,
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(action_key,module_id,action_type,payload)
                       VALUES($1,'botson','reply',$2::jsonb)
                       ON CONFLICT(action_key) DO NOTHING""",
                    action_key,
                    _json({"peer": reply_peer, "text": reply_text}),
                )
        return "accepted"

    async def accept_and_enqueue(
        self,
        *,
        source: str,
        event_key: str,
        module_id: str,
        event_payload: dict[str, Any],
        action_type: str,
        action_payload: dict[str, Any],
        exclusive: bool = False,
    ) -> str:
        """Persist input and action intent atomically.

        Returns ``accepted``, ``duplicate`` or ``busy``.  This transaction is
        the Dual Write boundary: no Telegram call occurs here.
        """
        action_key = f"{module_id}:{action_type}:{source}:{event_key}"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """INSERT INTO inbox_events(source,event_key,module_id,payload)
                       VALUES($1,$2,$3,$4::jsonb) ON CONFLICT DO NOTHING
                       RETURNING event_key""",
                    source,
                    event_key,
                    module_id,
                    _json(event_payload),
                )
                if not inserted:
                    return "duplicate"
                if exclusive:
                    # Lock the module row so two incoming triggers cannot both
                    # pass the open-work test.
                    await conn.fetchval(
                        "SELECT module_id FROM module_control WHERE module_id=$1 FOR UPDATE",
                        module_id,
                    )
                    if module_id == "botson" and action_type in {
                        "run_fleet",
                        "retest_one",
                    }:
                        open_action = await conn.fetchval(
                            """SELECT id FROM outbox_actions WHERE module_id=$1
                               AND action_type IN ('run_fleet','retest_one')
                               AND status IN ('pending','processing') LIMIT 1""",
                            module_id,
                        )
                    else:
                        open_action = await conn.fetchval(
                            """SELECT id FROM outbox_actions WHERE module_id=$1
                               AND action_type=$2 AND status IN ('pending','processing') LIMIT 1""",
                            module_id,
                            action_type,
                        )
                    if open_action:
                        return "busy"
                await conn.execute(
                    """INSERT INTO outbox_actions(action_key,module_id,action_type,payload)
                       VALUES($1,$2,$3,$4::jsonb) ON CONFLICT(action_key) DO NOTHING""",
                    action_key,
                    module_id,
                    action_type,
                    _json(action_payload),
                )
                if action_type in {"run_fleet", "retest_one"}:
                    run_id = run_id_for(action_key)
                    await conn.execute(
                        """INSERT INTO module_runs(run_id,module_id,trigger_key,status,request)
                           VALUES($1,$2,$3,'queued',$4::jsonb)
                           ON CONFLICT(trigger_key) DO NOTHING""",
                        run_id,
                        module_id,
                        action_key,
                        _json(action_payload),
                    )
        return "accepted"

    async def enqueue_action(
        self,
        *,
        action_key: str,
        module_id: str,
        action_type: str,
        payload: dict[str, Any],
    ) -> bool:
        inserted = await self.pool.fetchval(
            """INSERT INTO outbox_actions(action_key,module_id,action_type,payload)
               VALUES($1,$2,$3,$4::jsonb) ON CONFLICT(action_key) DO NOTHING
               RETURNING id""",
            action_key,
            module_id,
            action_type,
            _json(payload),
        )
        return inserted is not None

    async def accept_pv_message(
        self,
        *,
        event_key: str,
        user_id: int,
        message_id: int,
        username: str | None,
        display_name: str,
        response_kind: str,
        live_response_kind: str,
        two_screens_response_kind: str,
        two_screens_choice: str | None,
        two_screens_photo_delay_seconds: int,
        delay_seconds: int,
    ) -> str:
        """Advance the PV conversation and enqueue at most one delayed action.

        The inbound text is deliberately not accepted by this persistence
        boundary. Only the event identity and a small response classification
        are stored.
        """
        if response_kind not in {"unknown", "positive", "negative", "opt_out"}:
            raise ValueError("classificação de resposta inválida")
        if live_response_kind not in {"unknown", "positive", "negative", "opt_out"}:
            raise ValueError("classificação de resposta de live inválida")
        if two_screens_response_kind not in {"unknown", "positive", "negative", "opt_out"}:
            raise ValueError("classificação de duas telas inválida")
        if two_screens_choice not in {None, "peitos", "buceta", "cu"}:
            raise ValueError("escolha de foto inválida")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """INSERT INTO inbox_events(source,event_key,module_id,payload)
                       VALUES('telegram-user',$1,'pv_reply',$2::jsonb)
                       ON CONFLICT DO NOTHING RETURNING event_key""",
                    event_key,
                    _json(
                        {
                            "kind": "private_message",
                            "sender_id": user_id,
                            "message_id": message_id,
                            "response_kind": response_kind,
                            "live_response_kind": live_response_kind,
                            "two_screens_response_kind": two_screens_response_kind,
                            "two_screens_choice": two_screens_choice,
                        }
                    ),
                )
                if not inserted:
                    return "duplicate"

                contact = await conn.fetchrow(
                    """SELECT stage,followup_cycle,weekly_cycle
                       FROM pv_reply_contacts WHERE user_id=$1 FOR UPDATE""",
                    user_id,
                )
                if contact is None:
                    stage = (
                        "stopped" if response_kind == "opt_out" else "greeting_queued"
                    )
                    await conn.execute(
                        """INSERT INTO pv_reply_contacts(
                           user_id,username,display_name,stage,last_inbound_at,
                           last_inbound_message_id,greeting_queued_at,stopped_at,updated_at)
                           VALUES($1,$2,$3,$4,NOW(),$5,
                           CASE WHEN $4='greeting_queued' THEN NOW() END,
                           CASE WHEN $4='stopped' THEN NOW() END,NOW())""",
                        user_id,
                        username,
                        display_name,
                        stage,
                        message_id,
                    )
                    if response_kind == "opt_out":
                        return "opted_out"
                    await conn.execute(
                        """INSERT INTO outbox_actions(
                           action_key,module_id,action_type,payload,available_at)
                           VALUES($1,'pv_reply','send_greeting',$2::jsonb,
                           NOW()+($3::double precision*INTERVAL '1 second'))
                           ON CONFLICT(action_key) DO NOTHING""",
                        f"pv_reply:greeting:telegram-user:{event_key}",
                        _json({"peer": user_id, "campaign_id": "pv.greeting"}),
                        delay_seconds,
                    )
                    return "greeting_queued"

                await conn.execute(
                    """UPDATE pv_reply_contacts SET username=$2,display_name=$3,
                       last_inbound_at=NOW(),last_inbound_message_id=$4,updated_at=NOW()
                       WHERE user_id=$1""",
                    user_id,
                    username,
                    display_name,
                    message_id,
                )
                active_live = await conn.fetchrow(
                    """SELECT campaign_id,stage FROM live_campaign_recipients
                       WHERE user_id=$1 AND stage IN ('awaiting','remarketing_sent')
                       ORDER BY campaign_id DESC LIMIT 1 FOR UPDATE""",
                    user_id,
                )
                if active_live and live_response_kind in {"positive", "negative"}:
                    campaign_id = int(active_live["campaign_id"])
                    if live_response_kind == "negative":
                        await conn.execute(
                            """UPDATE live_campaign_recipients SET stage='stopped',updated_at=NOW()
                               WHERE campaign_id=$1 AND user_id=$2""",
                            campaign_id,
                            user_id,
                        )
                        return "live_declined"
                    await conn.execute(
                        """UPDATE live_campaign_recipients SET stage='link_queued',updated_at=NOW()
                           WHERE campaign_id=$1 AND user_id=$2""",
                        campaign_id,
                        user_id,
                    )
                    await conn.execute(
                        """INSERT INTO outbox_actions(
                           action_key,module_id,action_type,payload)
                           VALUES($1,'pv_reply','send_live_link',$2::jsonb)
                           ON CONFLICT(action_key) DO NOTHING""",
                        f"pv_reply:live-link:{campaign_id}:{user_id}",
                        _json({"peer": user_id, "campaign_id": campaign_id}),
                    )
                    return "live_link_queued"

                subscription = await conn.fetchval(
                    """SELECT status FROM live_alert_subscriptions
                       WHERE user_id=$1 FOR UPDATE""",
                    user_id,
                )
                if subscription == "pending" and live_response_kind in {
                    "positive",
                    "negative",
                }:
                    status = (
                        "subscribed" if live_response_kind == "positive" else "declined"
                    )
                    await conn.execute(
                        """UPDATE live_alert_subscriptions SET status=$2,
                           responded_at=NOW(),updated_at=NOW() WHERE user_id=$1""",
                        user_id,
                        status,
                    )
                    return f"live_{status}"

                if response_kind == "opt_out" or live_response_kind == "opt_out":
                    await conn.execute(
                        """INSERT INTO live_alert_subscriptions(user_id,status,responded_at)
                           VALUES($1,'unsubscribed',NOW())
                           ON CONFLICT(user_id) DO UPDATE SET status='unsubscribed',
                           responded_at=NOW(),updated_at=NOW()""",
                        user_id,
                    )
                    await conn.execute(
                        """UPDATE live_campaign_recipients SET stage='stopped',updated_at=NOW()
                           WHERE user_id=$1 AND stage IN ('queued','awaiting','remarketing_sent')""",
                        user_id,
                    )
                    await conn.execute(
                        """UPDATE pv_reply_contacts SET stage='stopped',
                           stopped_at=NOW(),next_followup_at=NULL,updated_at=NOW()
                           WHERE user_id=$1""",
                        user_id,
                    )
                    return "opted_out"

                # This is an isolated post-link branch.  It consumes only the
                # reply to its own prompt, so a simple "sim" cannot complete
                # the established weekly/follow-up flow by accident.
                two_screens = await conn.fetchrow(
                    "SELECT status,choice_retry_sent FROM pv_two_screens_sessions WHERE user_id=$1 FOR UPDATE",
                    user_id,
                )
                if two_screens:
                    status = two_screens["status"]
                    if status == "awaiting_optin":
                        if two_screens_response_kind == "positive":
                            await conn.execute(
                                "UPDATE pv_two_screens_sessions SET status='question_queued',updated_at=NOW() WHERE user_id=$1",
                                user_id,
                            )
                            await conn.execute(
                                """INSERT INTO outbox_actions(action_key,module_id,action_type,payload)
                                   VALUES($1,'pv_reply','send_two_screens_question',$2::jsonb)
                                   ON CONFLICT(action_key) DO NOTHING""",
                                f"pv_reply:two-screens:question:{user_id}",
                                _json({"peer": user_id}),
                            )
                            return "two_screens_question_queued"
                        if two_screens_response_kind == "negative":
                            await conn.execute(
                                "UPDATE pv_two_screens_sessions SET status='completed',completed_at=NOW(),updated_at=NOW() WHERE user_id=$1",
                                user_id,
                            )
                            return "two_screens_declined"
                        return "two_screens_waiting"
                    if status == "awaiting_choice":
                        if two_screens_choice:
                            await conn.execute(
                                "UPDATE pv_two_screens_sessions SET status='photo_queued',selected_slot=$2,updated_at=NOW() WHERE user_id=$1",
                                user_id,
                                two_screens_choice,
                            )
                            await conn.execute(
                                """INSERT INTO outbox_actions(action_key,module_id,action_type,payload,available_at)
                                   VALUES($1,'pv_reply','send_two_screens_photo',$2::jsonb,
                                     NOW()+($3::double precision*INTERVAL '1 second'))
                                   ON CONFLICT(action_key) DO NOTHING""",
                                f"pv_reply:two-screens:photo:{user_id}",
                                _json({"peer": user_id, "slot": two_screens_choice}),
                                two_screens_photo_delay_seconds,
                            )
                            return "two_screens_photo_queued"
                        if not bool(two_screens["choice_retry_sent"]):
                            await conn.execute(
                                """UPDATE pv_two_screens_sessions
                                   SET choice_retry_sent=TRUE,updated_at=NOW()
                                   WHERE user_id=$1""",
                                user_id,
                            )
                            await conn.execute(
                                """INSERT INTO outbox_actions(action_key,module_id,action_type,payload)
                                   VALUES($1,'pv_reply','send_two_screens_retry',$2::jsonb)
                                   ON CONFLICT(action_key) DO NOTHING""",
                                f"pv_reply:two-screens:retry:{user_id}",
                                _json({"peer": user_id}),
                            )
                            return "two_screens_retry_queued"
                        return "two_screens_invalid_choice"

                if contact["stage"] == "awaiting_reply":
                    await conn.execute(
                        """UPDATE pv_reply_contacts SET stage='link_queued',
                           link_queued_at=NOW(),updated_at=NOW() WHERE user_id=$1""",
                        user_id,
                    )
                    await conn.execute(
                        """INSERT INTO outbox_actions(
                           action_key,module_id,action_type,payload,available_at)
                           VALUES($1,'pv_reply','send_link',$2::jsonb,
                           NOW()+($3::double precision*INTERVAL '1 second'))
                           ON CONFLICT(action_key) DO NOTHING""",
                        f"pv_reply:link:telegram-user:{event_key}",
                        _json(
                            {"peer": user_id, "campaign_id": "pv.preview_link"}
                        ),
                        delay_seconds,
                    )
                    return "link_queued"

                if contact["stage"] not in {"following_up", "weekly"}:
                    return "ignored"
                if response_kind == "positive":
                    await conn.execute(
                        """UPDATE pv_reply_contacts SET stage='completed',
                           next_followup_at=NULL,updated_at=NOW() WHERE user_id=$1""",
                        user_id,
                    )
                    return "completed"
                if contact["stage"] != "weekly" or response_kind != "negative":
                    return "ignored"

                cycle = int(contact["weekly_cycle"])
                await conn.execute(
                    """INSERT INTO outbox_actions(
                       action_key,module_id,action_type,payload,available_at)
                       VALUES($1,'pv_reply','send_reminder_link',$2::jsonb,
                       NOW()+($3::double precision*INTERVAL '1 second'))
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"pv_reply:conditional-link:{user_id}:weekly:{cycle}",
                    _json({"peer": user_id, "campaign_id": "pv.preview_link"}),
                    delay_seconds,
                )
                return "conditional_link_queued"

    async def two_screens_action_allowed(self, user_id: int, status: str) -> bool:
        return bool(await self.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pv_two_screens_sessions WHERE user_id=$1 AND status=$2)", user_id, status
        ))

    async def advance_two_screens(self, user_id: int, expected: str, next_status: str) -> bool:
        updated = await self.pool.fetchval(
            """UPDATE pv_two_screens_sessions SET status=$3,updated_at=NOW()
               WHERE user_id=$1 AND status=$2 RETURNING user_id""",
            user_id, expected, next_status,
        )
        return updated is not None

    async def queue_next_two_screens_action(
        self, user_id: int, *, expected: str, next_status: str, action_type: str,
        action_key: str, delay_seconds: int,
    ) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                updated = await conn.fetchval(
                    """UPDATE pv_two_screens_sessions SET status=$3,updated_at=NOW()
                       WHERE user_id=$1 AND status=$2 RETURNING user_id""",
                    user_id, expected, next_status,
                )
                if updated is None:
                    return False
                await conn.execute(
                    """INSERT INTO outbox_actions(action_key,module_id,action_type,payload,available_at)
                       VALUES($1,'pv_reply',$2,$3::jsonb,NOW()+($4::double precision*INTERVAL '1 second'))
                       ON CONFLICT(action_key) DO NOTHING""",
                    action_key, action_type, _json({"peer": user_id}), delay_seconds,
                )
                return True

    async def two_screens_media_slot(self, slot: str):
        return await self.pool.fetchrow(
            "SELECT source_peer,source_message_id FROM pv_two_screens_media_slots WHERE slot=$1", slot
        )

    async def mark_two_screens_photo_sent(self, user_id: int, slot: str) -> bool:
        updated = await self.pool.fetchval(
            """UPDATE pv_two_screens_sessions SET status='completed',completed_at=NOW(),updated_at=NOW()
               WHERE user_id=$1 AND status='photo_queued' AND selected_slot=$2 RETURNING user_id""",
            user_id, slot,
        )
        return updated is not None

    async def set_two_screens_media_slot(self, *, slot: str, source_peer: int, source_message_id: int, updated_by: int) -> None:
        await self.pool.execute(
            """INSERT INTO pv_two_screens_media_slots(slot,source_peer,source_message_id,updated_by)
               VALUES($1,$2,$3,$4) ON CONFLICT(slot) DO UPDATE SET
               source_peer=EXCLUDED.source_peer,source_message_id=EXCLUDED.source_message_id,
               updated_by=EXCLUDED.updated_by,updated_at=NOW()""",
            slot, source_peer, source_message_id, updated_by,
        )

    async def queue_live_optin(self, user_id: int, delay_seconds: int = 1200) -> None:
        await self.pool.execute(
            """INSERT INTO outbox_actions(
               action_key,module_id,action_type,payload,available_at)
               VALUES($1,'pv_reply','send_live_optin',$2::jsonb,
               NOW()+($3::double precision*INTERVAL '1 second'))
               ON CONFLICT(action_key) DO NOTHING""",
            f"pv_reply:live-optin:{user_id}",
            _json({"peer": user_id, "campaign_id": "pv.live_optin"}),
            delay_seconds,
        )

    async def live_optin_allowed(self, user_id: int) -> bool:
        return not bool(
            await self.pool.fetchval(
                """SELECT EXISTS(SELECT 1 FROM live_alert_subscriptions
                   WHERE user_id=$1 AND status IN ('pending','subscribed','declined','unsubscribed'))""",
                user_id,
            )
        )

    async def mark_live_optin_asked(self, user_id: int) -> None:
        await self.pool.execute(
            """INSERT INTO live_alert_subscriptions(user_id,status,asked_at)
               VALUES($1,'pending',NOW()) ON CONFLICT(user_id) DO NOTHING""",
            user_id,
        )

    async def create_live_campaign(self, *, created_by: int, link: str) -> tuple[int, int]:
        """Create one campaign and queue one invite per consenting contact."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                campaign_id = int(
                    await conn.fetchval(
                        """INSERT INTO live_campaigns(link,created_by)
                           VALUES($1,$2) RETURNING id""",
                        link,
                        created_by,
                    )
                )
                rows = await conn.fetch(
                    """SELECT user_id FROM live_alert_subscriptions
                       WHERE status='subscribed' ORDER BY user_id"""
                )
                for index, row in enumerate(rows):
                    user_id = int(row["user_id"])
                    await conn.execute(
                        """INSERT INTO live_campaign_recipients(campaign_id,user_id)
                           VALUES($1,$2)""",
                        campaign_id,
                        user_id,
                    )
                    await conn.execute(
                        """INSERT INTO outbox_actions(
                           action_key,module_id,action_type,payload,available_at)
                           VALUES($1,'pv_reply','send_live_invite',$2::jsonb,
                           NOW()+($3::double precision*INTERVAL '1 second'))""",
                        f"pv_reply:live-invite:{campaign_id}:{user_id}",
                        _json({"peer": user_id, "campaign_id": campaign_id}),
                        index * 5,
                    )
                await conn.execute(
                    "UPDATE live_campaigns SET status='sent' WHERE id=$1",
                    campaign_id,
                )
                return campaign_id, len(rows)

    async def live_recipient_allowed(
        self, campaign_id: int, user_id: int, stage: str
    ) -> bool:
        return bool(
            await self.pool.fetchval(
                """SELECT EXISTS(SELECT 1 FROM live_campaign_recipients
                   WHERE campaign_id=$1 AND user_id=$2 AND stage=$3)""",
                campaign_id,
                user_id,
                stage,
            )
        )

    async def mark_live_invite_and_schedule_remarketing(
        self, *, campaign_id: int, user_id: int, delay_seconds: int = 600
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                updated = await conn.fetchval(
                    """UPDATE live_campaign_recipients SET stage='awaiting',
                       invited_at=NOW(),updated_at=NOW()
                       WHERE campaign_id=$1 AND user_id=$2 AND stage='queued'
                       RETURNING user_id""",
                    campaign_id,
                    user_id,
                )
                if updated is None:
                    return
                await conn.execute(
                    """INSERT INTO outbox_actions(
                       action_key,module_id,action_type,payload,available_at)
                       VALUES($1,'pv_reply','send_live_remarketing',$2::jsonb,
                       NOW()+($3::double precision*INTERVAL '1 second'))
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"pv_reply:live-remarketing:{campaign_id}:{user_id}",
                    _json({"peer": user_id, "campaign_id": campaign_id}),
                    delay_seconds,
                )

    async def mark_live_remarketing_sent(
        self, campaign_id: int, user_id: int, close_delay_seconds: int = 600
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                updated = await conn.fetchval(
                    """UPDATE live_campaign_recipients SET stage='remarketing_sent',
                       remarketing_at=NOW(),updated_at=NOW()
                       WHERE campaign_id=$1 AND user_id=$2 AND stage='awaiting'
                       RETURNING user_id""",
                    campaign_id,
                    user_id,
                )
                if updated is None:
                    return
                await conn.execute(
                    """INSERT INTO outbox_actions(
                       action_key,module_id,action_type,payload,available_at)
                       VALUES($1,'pv_reply','close_live_recipient',$2::jsonb,
                       NOW()+($3::double precision*INTERVAL '1 second'))
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"pv_reply:live-close:{campaign_id}:{user_id}",
                    _json({"peer": user_id, "campaign_id": campaign_id}),
                    close_delay_seconds,
                )

    async def close_live_recipient(self, campaign_id: int, user_id: int) -> None:
        await self.pool.execute(
            """UPDATE live_campaign_recipients SET stage='stopped',updated_at=NOW()
               WHERE campaign_id=$1 AND user_id=$2 AND stage='remarketing_sent'""",
            campaign_id,
            user_id,
        )

    async def live_campaign_link(self, campaign_id: int) -> str | None:
        return await self.pool.fetchval(
            "SELECT link FROM live_campaigns WHERE id=$1", campaign_id
        )

    async def mark_live_link_delivered(self, campaign_id: int, user_id: int) -> None:
        await self.pool.execute(
            """UPDATE live_campaign_recipients SET stage='delivered',
               delivered_at=NOW(),updated_at=NOW()
               WHERE campaign_id=$1 AND user_id=$2 AND stage='link_queued'""",
            campaign_id,
            user_id,
        )

    async def live_subscription_counts(self) -> tuple[int, int]:
        row = await self.pool.fetchrow(
            """SELECT COUNT(*) FILTER(WHERE status='subscribed') subscribed,
               COUNT(*) FILTER(WHERE status='pending') pending
               FROM live_alert_subscriptions"""
        )
        return int(row["subscribed"] or 0), int(row["pending"] or 0)

    async def pv_action_allowed(
        self, user_id: int, stage: str, cycle: int | None = None
    ) -> bool:
        return bool(
            await self.pool.fetchval(
                """SELECT EXISTS(
                   SELECT 1 FROM pv_reply_contacts
                   WHERE user_id=$1 AND stage=$2
                   AND ($3::integer IS NULL
                     OR (stage='weekly' AND weekly_cycle=($3-1))
                     OR (stage<>'weekly' AND followup_cycle=$3)))""",
                user_id,
                stage,
                cycle,
            )
        )

    async def pv_reminder_link_allowed(self, user_id: int) -> bool:
        return bool(
            await self.pool.fetchval(
                """SELECT EXISTS(SELECT 1 FROM pv_reply_contacts
                   WHERE user_id=$1 AND stage IN ('following_up','weekly'))""",
                user_id,
            )
        )

    async def mark_pv_greeting_sent(self, user_id: int) -> bool:
        updated = await self.pool.fetchval(
            """UPDATE pv_reply_contacts SET stage='awaiting_reply',
               greeting_sent_at=NOW(),updated_at=NOW()
               WHERE user_id=$1 AND stage='greeting_queued' RETURNING user_id""",
            user_id,
        )
        return updated is not None

    async def mark_pv_link_sent_and_schedule(
        self,
        *,
        user_id: int,
        delay_seconds: int,
        weekly_delay_seconds: int,
        max_cycles: int,
        two_screens_enabled: bool = False,
        two_screens_delay_seconds: int = 20,
    ) -> bool:
        """Record link delivery and create the first follow-up atomically."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                stage = await conn.fetchval(
                    "SELECT stage FROM pv_reply_contacts WHERE user_id=$1 FOR UPDATE",
                    user_id,
                )
                if stage != "link_queued":
                    return False
                if max_cycles <= 0:
                    due = await conn.fetchval(
                        """UPDATE pv_reply_contacts SET stage='weekly',
                           link_sent_at=NOW(),weekly_cycle=0,
                           next_followup_at=NOW()+($2::double precision*INTERVAL '1 second'),
                           updated_at=NOW() WHERE user_id=$1
                           RETURNING next_followup_at""",
                        user_id,
                        weekly_delay_seconds,
                    )
                    await conn.execute(
                        """INSERT INTO outbox_actions(
                           action_key,module_id,action_type,payload,available_at)
                           VALUES($1,'pv_reply','send_weekly_question',$2::jsonb,$3)
                           ON CONFLICT(action_key) DO NOTHING""",
                        f"pv_reply:weekly-question:{user_id}:1",
                        _json(
                            {
                                "peer": user_id,
                                "campaign_id": "pv.weekly_question",
                                "cycle": 1,
                            }
                        ),
                        due,
                    )
                    await self._queue_two_screens_after_link(
                        conn,
                        user_id=user_id,
                        enabled=two_screens_enabled,
                        delay_seconds=two_screens_delay_seconds,
                    )
                    return True
                await conn.execute(
                    """UPDATE pv_reply_contacts SET stage='following_up',
                       link_sent_at=NOW(),followup_cycle=1,
                       next_followup_at=NOW()+($2::double precision*INTERVAL '1 second'),
                       updated_at=NOW() WHERE user_id=$1""",
                    user_id,
                    delay_seconds,
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(
                       action_key,module_id,action_type,payload,available_at)
                       VALUES($1,'pv_reply','send_followup',$2::jsonb,
                       NOW()+($3::double precision*INTERVAL '1 second'))
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"pv_reply:followup:{user_id}:1",
                    _json(
                        {
                            "peer": user_id,
                            "campaign_id": "pv.followup",
                            "cycle": 1,
                        }
                    ),
                    delay_seconds,
                )
                await self._queue_two_screens_after_link(
                    conn,
                    user_id=user_id,
                    enabled=two_screens_enabled,
                    delay_seconds=two_screens_delay_seconds,
                )
                return True

    @staticmethod
    async def _queue_two_screens_after_link(
        conn, *, user_id: int, enabled: bool, delay_seconds: int
    ) -> bool:
        """Queue the branch in the link transaction, only with all media ready."""
        if not enabled:
            return False
        media_ready = await conn.fetchval(
            """SELECT COUNT(*) = 3 FROM pv_two_screens_media_slots
               WHERE slot IN ('peitos','buceta','cu')"""
        )
        if not media_ready:
            return False
        inserted = await conn.fetchval(
            """INSERT INTO pv_two_screens_sessions(user_id,status)
               VALUES($1,'prompt_queued') ON CONFLICT(user_id) DO NOTHING
               RETURNING user_id""",
            user_id,
        )
        if inserted is None:
            return False
        await conn.execute(
            """INSERT INTO outbox_actions(action_key,module_id,action_type,payload,available_at)
               VALUES($1,'pv_reply','send_two_screens_prompt',$2::jsonb,
                 NOW()+($3::double precision*INTERVAL '1 second'))
               ON CONFLICT(action_key) DO NOTHING""",
            f"pv_reply:two-screens:prompt:{user_id}",
            _json({"peer": user_id}),
            delay_seconds,
        )
        return True

    async def complete_pv_followup_and_schedule_next(
        self,
        *,
        user_id: int,
        completed_cycle: int,
        next_delay_seconds: int,
        weekly_delay_seconds: int,
        max_cycles: int,
    ) -> bool:
        """Close one reminder and, if bounded policy allows, queue the next."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """SELECT stage,followup_cycle FROM pv_reply_contacts
                       WHERE user_id=$1 FOR UPDATE""",
                    user_id,
                )
                if (
                    not row
                    or row["stage"] != "following_up"
                    or int(row["followup_cycle"]) != completed_cycle
                ):
                    return False
                next_cycle = completed_cycle + 1
                if next_cycle > max_cycles:
                    due = await conn.fetchval(
                        """UPDATE pv_reply_contacts SET stage='weekly',weekly_cycle=0,
                           next_followup_at=NOW()+(
                             $2::double precision*INTERVAL '1 second'
                           ),updated_at=NOW() WHERE user_id=$1
                           RETURNING next_followup_at""",
                        user_id,
                        weekly_delay_seconds,
                    )
                    await conn.execute(
                        """INSERT INTO outbox_actions(
                           action_key,module_id,action_type,payload,available_at)
                           VALUES($1,'pv_reply','send_weekly_question',$2::jsonb,$3)
                           ON CONFLICT(action_key) DO NOTHING""",
                        f"pv_reply:weekly-question:{user_id}:1",
                        _json(
                            {
                                "peer": user_id,
                                "campaign_id": "pv.weekly_question",
                                "cycle": 1,
                            }
                        ),
                        due,
                    )
                    return True
                await conn.execute(
                    """UPDATE pv_reply_contacts SET followup_cycle=$2,
                       next_followup_at=NOW()+($3::double precision*INTERVAL '1 second'),
                       updated_at=NOW() WHERE user_id=$1""",
                    user_id,
                    next_cycle,
                    next_delay_seconds,
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(
                       action_key,module_id,action_type,payload,available_at)
                       VALUES($1,'pv_reply','send_followup',$2::jsonb,
                       NOW()+($3::double precision*INTERVAL '1 second'))
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"pv_reply:followup:{user_id}:{next_cycle}",
                    _json(
                        {
                            "peer": user_id,
                            "campaign_id": "pv.followup",
                            "cycle": next_cycle,
                        }
                    ),
                    next_delay_seconds,
                )
                return True

    async def complete_pv_weekly_and_schedule_next(
        self,
        *,
        user_id: int,
        completed_cycle: int,
        delay_seconds: int,
    ) -> bool:
        """Keep one friendly question per week until a reply closes the flow."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """SELECT stage,weekly_cycle FROM pv_reply_contacts
                       WHERE user_id=$1 FOR UPDATE""",
                    user_id,
                )
                if (
                    not row
                    or row["stage"] != "weekly"
                    or int(row["weekly_cycle"]) != completed_cycle - 1
                ):
                    return False
                next_cycle = completed_cycle + 1
                await conn.execute(
                    """UPDATE pv_reply_contacts SET weekly_cycle=$2,
                       weekly_last_question_at=NOW(),
                       next_followup_at=NOW()+($3::double precision*INTERVAL '1 second'),
                       updated_at=NOW() WHERE user_id=$1""",
                    user_id,
                    completed_cycle,
                    delay_seconds,
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(
                       action_key,module_id,action_type,payload,available_at)
                       VALUES($1,'pv_reply','send_weekly_question',$2::jsonb,
                       NOW()+($3::double precision*INTERVAL '1 second'))
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"pv_reply:weekly-question:{user_id}:{next_cycle}",
                    _json(
                        {
                            "peer": user_id,
                            "campaign_id": "pv.weekly_question",
                            "cycle": next_cycle,
                        }
                    ),
                    delay_seconds,
                )
                return True

    async def claim_next_action(self) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """UPDATE outbox_actions SET status='processing', attempts=attempts+1,
               lease_until=NOW()+INTERVAL '30 minutes', updated_at=NOW()
               WHERE id=(SELECT actions.id FROM outbox_actions actions
                         JOIN module_control modules
                           ON modules.module_id=actions.module_id
                          AND modules.enabled IS TRUE
                         WHERE actions.status='pending'
                           AND actions.available_at<=NOW()
                         ORDER BY actions.available_at,actions.id
                         FOR UPDATE OF actions SKIP LOCKED LIMIT 1)
               RETURNING *"""
        )
        if not row:
            return None
        result = dict(row)
        result["payload"] = _decoded(result["payload"]) or {}
        return result

    async def finish_action(self, action_id: int, result: Any = None) -> None:
        await self.pool.execute(
            """UPDATE outbox_actions SET status='succeeded', result=$2::jsonb,
               lease_until=NULL, updated_at=NOW() WHERE id=$1""",
            action_id,
            _json(result or {}),
        )

    async def fail_action(self, action_id: int, exc: BaseException) -> None:
        await self.pool.execute(
            """UPDATE outbox_actions SET status='failed', last_error=$2,
               lease_until=NULL, updated_at=NOW() WHERE id=$1""",
            action_id,
            f"{type(exc).__name__}: {exc}"[:1000],
        )

    async def review_action(self, action_id: int, exc: BaseException) -> None:
        await self.pool.execute(
            """UPDATE outbox_actions SET status='review', last_error=$2,
               lease_until=NULL, updated_at=NOW() WHERE id=$1""",
            action_id,
            f"{type(exc).__name__}: {exc}"[:1000],
        )

    async def start_run(self, run_id: str) -> None:
        await self.pool.execute(
            "UPDATE module_runs SET status='running', started_at=NOW() WHERE run_id=$1",
            run_id,
        )

    async def finish_run(self, run_id: str, result: dict[str, Any]) -> None:
        await self.pool.execute(
            """UPDATE module_runs SET status='succeeded', result=$2::jsonb,
               finished_at=NOW() WHERE run_id=$1""",
            run_id,
            _json(result),
        )

    async def finish_run_and_enqueue_reply(
        self,
        *,
        run_id: str,
        result: dict[str, Any],
        reply_peer: int,
        reply_text: str,
    ) -> None:
        """Commit the test result and final delivery intent atomically."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """UPDATE module_runs SET status='succeeded', result=$2::jsonb,
                       finished_at=NOW() WHERE run_id=$1""",
                    run_id,
                    _json(result),
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(action_key,module_id,action_type,payload)
                       VALUES($1,'botson','reply',$2::jsonb)
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"botson:reply:{run_id}:result",
                    _json({"peer": reply_peer, "text": reply_text}),
                )

    async def fail_run_and_enqueue_reply(
        self,
        *,
        run_id: str,
        exc: BaseException,
        reply_peer: int,
        reply_text: str,
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """UPDATE module_runs SET status='failed', error=$2,
                       finished_at=NOW() WHERE run_id=$1""",
                    run_id,
                    f"{type(exc).__name__}: {exc}"[:1000],
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(action_key,module_id,action_type,payload)
                       VALUES($1,'botson','reply',$2::jsonb)
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"botson:reply:{run_id}:failure",
                    _json({"peer": reply_peer, "text": reply_text}),
                )

    async def fail_run(self, run_id: str, exc: BaseException) -> None:
        await self.pool.execute(
            """UPDATE module_runs SET status='failed', error=$2,
               finished_at=NOW() WHERE run_id=$1""",
            run_id,
            f"{type(exc).__name__}: {exc}"[:1000],
        )

    async def latest_successful_run(self, module_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """SELECT run_id, result, finished_at FROM module_runs
               WHERE module_id=$1 AND status='succeeded'
               ORDER BY finished_at DESC LIMIT 1""",
            module_id,
        )
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "result": _decoded(row["result"]),
            "finished_at": row["finished_at"],
        }

    async def begin_effect(
        self,
        *,
        effect_key: str,
        action_id: int,
        effect_type: str,
        payload: dict[str, Any],
    ) -> tuple[str, Any]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status,result FROM telegram_effects WHERE effect_key=$1 FOR UPDATE",
                    effect_key,
                )
                if row:
                    if row["status"] == "succeeded":
                        return "succeeded", _decoded(row["result"])
                    return "ambiguous", None
                await conn.execute(
                    """INSERT INTO telegram_effects(
                       effect_key,outbox_action_id,effect_type,payload,status)
                       VALUES($1,$2,$3,$4::jsonb,'processing')""",
                    effect_key,
                    action_id,
                    effect_type,
                    _json(payload),
                )
                return "execute", None

    async def finish_effect(self, effect_key: str, result: Any = None) -> None:
        await self.pool.execute(
            """UPDATE telegram_effects SET status='succeeded', result=$2::jsonb,
               last_error=NULL, updated_at=NOW() WHERE effect_key=$1""",
            effect_key,
            _json(result or {}),
        )

    async def review_effect(self, effect_key: str, exc: BaseException) -> None:
        await self.pool.execute(
            """UPDATE telegram_effects SET status='review', last_error=$2,
               updated_at=NOW() WHERE effect_key=$1""",
            effect_key,
            f"{type(exc).__name__}: {exc}"[:1000],
        )

    async def counts_for_dashboard(self):
        return await self.pool.fetchrow(
            """SELECT COUNT(*) FILTER(WHERE kind='channel') channels,
               COUNT(*) FILTER(WHERE kind='group') groups,
               COUNT(*) FILTER(WHERE can_text IS TRUE) postable,
               COUNT(*) FILTER(WHERE can_text IS NULL OR risk IN ('unknown','medium')) uncertain
               FROM chats"""
        )
