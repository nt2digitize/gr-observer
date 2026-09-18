"""Canonical production policy for the PV rib.

All safeguards live in normal class composition. Nothing in this module replaces
methods on existing objects at runtime.
"""

from __future__ import annotations

import asyncio
import logging

from telethon import events, types
from telethon.tl.functions.contacts import GetBlockedRequest

from ..group_membership_tracker import GroupMembershipTracker
from ..human_timing import MIN_WRITING_DELAY_SECONDS
from ..pv_balloon_sender import has_media, send_media_balloon
from ..pv_linear_capability_runtime import PvLinearCapabilityRuntimeMixin
from ..pv_linear_flow import PvLinearRuntimeMixin, linear_flow_enabled
from ..pv_intent_variant_runtime import PvIntentVariantRuntimeMixin
from ..pv_membership_context import membership_conversation_context
from ..pv_message_steps import POSITION_GAP
from ..pv_response_memory import PvResponseMemoryShadow
from ..pv_suppression import suppress_pv_user
from ..pv_temperature import PvTemperatureShadow, classify_demand_signal
from .pv_reply import classify_response, is_human_sender
from .pv_reply_contacts import PvReplyWithContacts

log = logging.getLogger("gr-observer.pv-production")


class PvReplyProduction(
    PvLinearCapabilityRuntimeMixin,
    PvLinearRuntimeMixin,
    PvIntentVariantRuntimeMixin,
    PvReplyWithContacts,
):
    """Production PV rib with per-contact ordering and stale-step protection."""

    _LOCK_STRIPES = 256
    _NATIVE_BLOCK_RECONCILE_LIMIT = 100

    def __init__(self, storage, settings):
        super().__init__(storage, settings)
        self._accept_locks = [asyncio.Lock() for _ in range(self._LOCK_STRIPES)]
        self._native_block_handler = None
        self.membership_tracker = GroupMembershipTracker(
            storage.pool,
            preview_link=getattr(settings, "pv_preview_link", ""),
        )
        self._membership_tracker_ready = False
        self._membership_handler = None
        self.temperature_shadow = PvTemperatureShadow(storage.pool)
        self._temperature_shadow_ready = False
        self.response_memory_shadow = PvResponseMemoryShadow(
            storage.pool,
            enabled=bool(getattr(settings, "pv_minilearn_shadow_enabled", False)),
        )
        self._response_memory_shadow_ready = False

    async def _run_block(self, *args, **kwargs) -> dict:
        """Quarantine the legacy sequencer whenever the linear engine owns PV."""
        if linear_flow_enabled():
            return {"sent": False, "reason": "legacy_flow_disabled"}
        return await super()._run_block(*args, **kwargs)

    async def action_auto_queue_two_screens_photo(self, action: dict, effects) -> dict:
        return await super().action_auto_queue_two_screens_photo(action, effects)

    async def action_send_two_screens_photo(self, action: dict, effects) -> dict:
        return await super().action_send_two_screens_photo(action, effects)

    async def action_close_live_recipient(self, action: dict, effects) -> dict:
        return await super().action_close_live_recipient(action, effects)

    async def on_connect(self, client, me) -> None:
        await super().on_connect(client, me)
        try:
            await self.membership_tracker.ensure_schema()
        except Exception as exc:
            log.warning(
                "PV membership shadow unavailable erro=%s",
                type(exc).__name__,
            )
            self._membership_tracker_ready = False
        else:
            self._membership_tracker_ready = True
            self._membership_handler = self._on_membership_update
            client.add_event_handler(
                self._membership_handler,
                events.Raw(
                    types=(
                        types.UpdateChannelParticipant,
                        types.UpdateChatParticipantAdd,
                        types.UpdateChatParticipantDelete,
                    )
                ),
            )
        try:
            await self.temperature_shadow.ensure_schema()
        except Exception as exc:
            log.warning("PV temperature shadow unavailable erro=%s", type(exc).__name__)
            self._temperature_shadow_ready = False
        else:
            self._temperature_shadow_ready = True
        try:
            await self.response_memory_shadow.ensure_schema()
        except Exception as exc:
            log.warning("PV MiniLearn shadow unavailable erro=%s", type(exc).__name__)
            self._response_memory_shadow_ready = False
        else:
            self._response_memory_shadow_ready = bool(
                self.response_memory_shadow.enabled
            )
        self._native_block_handler = self._on_native_block_update
        client.add_event_handler(
            self._native_block_handler,
            events.Raw(types.UpdatePeerBlocked),
        )
        try:
            reconciled = await self._reconcile_native_blocklist(client, int(me.id))
        except Exception as exc:
            log.warning(
                "PV native block reconciliation skipped erro=%s",
                type(exc).__name__,
            )
        else:
            if reconciled:
                log.info(
                    "PV native block reconciliation applied count=%s",
                    reconciled,
                )
        restored = await self._restore_missing_required_destinations()
        if restored:
            log.warning("PV restored missing required steps: %s", ",".join(restored))

    async def on_disconnect(self) -> None:
        if self.client is not None and self._membership_handler is not None:
            self.client.remove_event_handler(self._membership_handler)
        self._membership_handler = None
        self._membership_tracker_ready = False
        if self.client is not None and self._native_block_handler is not None:
            self.client.remove_event_handler(self._native_block_handler)
        self._native_block_handler = None
        self._temperature_shadow_ready = False
        self._response_memory_shadow_ready = False
        await super().on_disconnect()

    async def _on_membership_update(self, update) -> None:
        """Persist a membership fact without creating Telegram work."""
        if not self._membership_tracker_ready:
            return
        try:
            outcome = await self.membership_tracker.observe(update)
        except Exception as exc:
            log.warning(
                "PV membership shadow skipped erro=%s",
                type(exc).__name__,
            )
            return
        if outcome in {"joined", "left"}:
            log.info("PV membership shadow registrou mudança=%s", outcome)

    async def _on_native_block_update(self, update) -> None:
        """Mirror a Telegram main-blocklist event into the internal PV kill switch."""
        if not bool(getattr(update, "blocked", False)):
            return
        if bool(getattr(update, "blocked_my_stories_from", False)):
            return
        peer = getattr(update, "peer_id", None)
        if not isinstance(peer, types.PeerUser):
            return
        user_id = int(peer.user_id)
        neutralized = await suppress_pv_user(
            self.storage.pool,
            user_id,
            suppressed_by=(int(self.me.id) if self.me is not None else None),
            reason="telegram_native_block",
        )
        log.info(
            "PV native Telegram block synchronized peer=%s neutralized=%s",
            user_id,
            neutralized,
        )

    async def _reconcile_native_blocklist(self, client, actor_id: int) -> int:
        """Import the current main blocklist once so pre-deploy blocks are honored."""
        result = await client(
            GetBlockedRequest(
                offset=0,
                limit=self._NATIVE_BLOCK_RECONCILE_LIMIT,
            )
        )
        reconciled = 0
        for item in getattr(result, "blocked", ()):
            peer = getattr(item, "peer_id", None)
            if isinstance(peer, types.PeerUser):
                user_id = int(peer.user_id)
            else:
                legacy_user_id = getattr(item, "user_id", None)
                if legacy_user_id is None:
                    continue
                user_id = int(legacy_user_id)
            await suppress_pv_user(
                self.storage.pool,
                user_id,
                suppressed_by=int(actor_id),
                reason="telegram_native_block_reconcile",
            )
            reconciled += 1
        return reconciled

    async def _observe_temperature_shadow(self, event) -> None:
        if not getattr(self, "_temperature_shadow_ready", False):
            return
        try:
            decision = await self.temperature_shadow.observe(
                event_key=self._event_key(event),
                user_id=int(event.sender_id),
                demand_signal=classify_demand_signal(event.raw_text or ""),
            )
        except Exception as exc:
            log.warning("PV temperature shadow skipped erro=%s", type(exc).__name__)
            return
        if decision is not None and (decision.vacuum_candidate or decision.band in {"hot", "warm"}):
            log.info(
                "PV temperature shadow peer=%s band=%s score=%s vacuum=%s reasons=%s",
                int(event.sender_id),
                decision.band,
                decision.score,
                decision.vacuum_candidate,
                ",".join(decision.reasons),
            )

    async def _observe_response_memory_shadow(self, event) -> None:
        """Observe MiniLearn facts without ever changing the PV business path."""
        if not getattr(self, "_response_memory_shadow_ready", False):
            return
        try:
            if not getattr(event, "is_private", False):
                return
            if bool(getattr(event, "out", False)):
                peer = int(getattr(event, "chat_id", 0) or 0)
                if peer <= 0:
                    return
                observation = await self.response_memory_shadow.observe_outbound(
                    user_id=peer,
                    message_id=int(event.id),
                    text=event.raw_text or "",
                    has_media=getattr(event, "media", None) is not None,
                )
                if observation.learned:
                    log.info(
                        "PV MiniLearn learned intent=%s peer=%s",
                        observation.intent,
                        peer,
                    )
                return

            sender_id = int(getattr(event, "sender_id", 0) or 0)
            if sender_id <= 0:
                return
            await self.response_memory_shadow.observe_inbound(
                user_id=sender_id,
                message_id=int(event.id),
                text=event.raw_text or "",
            )
        except Exception as exc:
            log.warning("PV MiniLearn shadow skipped erro=%s", type(exc).__name__)

    async def _linear_opt_out(self, event) -> bool:
        """Keep opt-out as a global guardrail, never as a conversation branch."""
        if not linear_flow_enabled() or classify_response(event.raw_text or "") != "opt_out":
            return False
        sender = await event.get_sender()
        if not is_human_sender(sender):
            return False
        peer = int(sender.id)
        if self.me is not None and peer == int(self.me.id):
            return False
        await suppress_pv_user(
            self.storage.pool,
            peer,
            suppressed_by=None,
            reason="lead_opt_out",
            username_hint=getattr(sender, "username", None),
        )
        await self.storage.pool.execute(
            """UPDATE pv_linear_sessions SET status='stopped',updated_at=NOW()
               WHERE user_id=$1 AND status<>'stopped'""",
            peer,
        )
        return True

    async def handle_event(self, event) -> bool:
        if event.is_private and not event.out and event.sender_id:
            lock = self._accept_locks[int(event.sender_id) % self._LOCK_STRIPES]
            async with lock:
                if await self._linear_opt_out(event):
                    return False
                result = await super().handle_event(event)
                await self._observe_temperature_shadow(event)
                await self._observe_response_memory_shadow(event)
                return result
        result = await super().handle_event(event)
        await self._observe_response_memory_shadow(event)
        return result

    async def _send_row(
        self,
        *,
        effects,
        peer: int,
        row,
        origin_key: str,
        variables: dict,
    ) -> dict:
        """Attach passive membership context without changing delivery contracts."""
        linear_send = linear_flow_enabled() and str(origin_key).startswith("pv_reply:linear:")
        if linear_send:
            try:
                membership_context = await membership_conversation_context(
                    self.storage.pool,
                    int(peer),
                )
            except Exception as exc:
                log.warning(
                    "PV membership conversation context skipped erro=%s",
                    type(exc).__name__,
                )
            else:
                variables = dict(variables)
                variables["_membership_state"] = membership_context.state
                variables["_membership_repertoires"] = list(
                    membership_context.allowed_repertoires
                )
                log.info(
                    "PV linear membership context peer=%s state=%s repertoires=%s",
                    int(peer),
                    membership_context.state,
                    ",".join(membership_context.allowed_repertoires),
                )

        if not has_media(row):
            return await super()._send_row(
                effects=effects,
                peer=peer,
                row=row,
                origin_key=origin_key,
                variables=variables,
            )

        text = self.message_store.render(
            str(row["content"]),
            preview_link=self.settings.pv_preview_link,
            live_link=str(variables.get("live_link") or ""),
        ).strip()
        if text:
            _, _, typing = self._human_plan(row, origin_key, text)
            await self._show_typing(effects, peer, typing)
        effect_key = f"{origin_key}:message:{row['id']}"
        result = await send_media_balloon(
            effects=effects,
            peer=peer,
            row=row,
            text=text,
            effect_key=effect_key,
        )
        return {"sent": True, "step_id": int(row["id"]), **result}

    async def _restore_missing_required_destinations(self) -> tuple[str, ...]:
        """Restore only required rows physically deleted by old editor behavior."""
        rows = (
            ("link.preview", "link", POSITION_GAP * 2, "Link da prévia", "{preview_link}", 7),
            ("followup.link", "followup", POSITION_GAP * 2, "Link do follow-up", "{preview_link}", 3),
            ("weekly.link1", "weekly", POSITION_GAP * 2, "Link semanal 1", "{preview_link}", 7),
            ("weekly.link2", "weekly", POSITION_GAP * 4, "Link semanal 2", "{preview_link}", 3),
            ("live.link", "live_link", POSITION_GAP * 2, "Destino", "{live_link}", MIN_WRITING_DELAY_SECONDS),
        )
        restored: list[str] = []
        for step_key, block_key, position, label, content, median in rows:
            inserted = await self.storage.pool.fetchval(
                """INSERT INTO pv_message_steps(
                       step_key,block_key,branch_key,position,label,content,kind,
                       median_delay_seconds,variant_root,built_in,updated_at)
                   VALUES($1,$2,NULL,$3,$4,$5,'link',$6,FALSE,TRUE,NOW())
                   ON CONFLICT(step_key) DO NOTHING RETURNING step_key""",
                step_key,
                block_key,
                position,
                label,
                content,
                max(MIN_WRITING_DELAY_SECONDS, int(median)),
            )
            if inserted:
                restored.append(str(inserted))
        return tuple(restored)

    async def _queue_sequence_continuation(self, **kwargs) -> bool:
        peer = int(kwargs["peer"])
        row = await self.storage.pool.fetchrow(
            "SELECT stage,last_inbound_message_id FROM pv_reply_contacts WHERE user_id=$1",
            peer,
        )
        context = dict(kwargs.get("context") or {})
        context["_pv_guard"] = {
            "stage": str(row["stage"]) if row is not None else None,
            "last_inbound_message_id": (
                int(row["last_inbound_message_id"])
                if row is not None and row["last_inbound_message_id"] is not None
                else None
            ),
        }
        kwargs["context"] = context
        return await super()._queue_sequence_continuation(**kwargs)

    async def action_send_message_step(self, action: dict, effects) -> dict:
        if linear_flow_enabled():
            return {"sent": False, "reason": "legacy_flow_disabled"}
        payload = action.get("payload") or {}
        context = dict(payload.get("context") or {})
        guard = context.get("_pv_guard")
        if not isinstance(guard, dict):
            return {"sent": False, "reason": "legacy_step_quarantined"}

        peer = int(payload.get("peer") or 0)
        row = await self.storage.pool.fetchrow(
            "SELECT stage,last_inbound_message_id FROM pv_reply_contacts WHERE user_id=$1",
            peer,
        )
        current_stage = str(row["stage"]) if row is not None else None
        current_message_id = (
            int(row["last_inbound_message_id"])
            if row is not None and row["last_inbound_message_id"] is not None
            else None
        )
        expected_message_id = guard.get("last_inbound_message_id")
        if expected_message_id is not None:
            expected_message_id = int(expected_message_id)
        if current_stage != guard.get("stage") or current_message_id != expected_message_id:
            return {"sent": False, "reason": "conversation_state_changed"}
        return await super().action_send_message_step(action, effects)
