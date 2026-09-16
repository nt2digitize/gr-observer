"""Canonical production policy for the PV rib.

All safeguards live in normal class composition. Nothing in this module replaces
methods on existing objects at runtime.
"""

from __future__ import annotations

import asyncio
import logging

from telethon import events, types
from telethon.tl.functions.contacts import GetBlockedRequest

from ..human_timing import MIN_WRITING_DELAY_SECONDS
from ..pv_balloon_sender import has_media, send_media_balloon
from ..pv_homage import PvHomageStore
from ..pv_message_steps import POSITION_GAP
from ..pv_response_memory import PvResponseMemoryShadow
from ..pv_suppression import suppress_pv_user
from ..pv_temperature import PvTemperatureShadow, classify_demand_signal
from .pv_reply import display_name, is_human_sender
from .pv_reply_contacts import PvReplyWithContacts

log = logging.getLogger("gr-observer.pv-production")


class PvReplyProduction(PvReplyWithContacts):
    """Production PV rib with per-contact ordering and stale-step protection."""

    _LOCK_STRIPES = 256
    _NATIVE_BLOCK_RECONCILE_LIMIT = 100

    def __init__(self, storage, settings):
        super().__init__(storage, settings)
        self._accept_locks = [asyncio.Lock() for _ in range(self._LOCK_STRIPES)]
        self._native_block_handler = None
        self.homage_store = PvHomageStore(storage.pool)
        self.temperature_shadow = PvTemperatureShadow(storage.pool)
        self._temperature_shadow_ready = False
        self.response_memory_shadow = PvResponseMemoryShadow(
            storage.pool,
            enabled=bool(getattr(settings, "pv_minilearn_shadow_enabled", False)),
        )
        self._response_memory_shadow_ready = False

    async def on_connect(self, client, me) -> None:
        await super().on_connect(client, me)
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
        if self.client is not None and self._native_block_handler is not None:
            self.client.remove_event_handler(self._native_block_handler)
        self._native_block_handler = None
        self._temperature_shadow_ready = False
        self._response_memory_shadow_ready = False
        await super().on_disconnect()

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

    @staticmethod
    def _homage_media_kind(event) -> str | None:
        message = getattr(event, "message", None)
        if message is None:
            return None
        if bool(getattr(message, "gif", False)):
            return "gif"
        if bool(getattr(message, "video", False)):
            return "video"
        if getattr(message, "photo", None) is not None:
            return "photo"
        return None

    async def _capture_two_screens_homage(self, event) -> bool:
        """Capture a post-photo media reply without turning it into another choice."""
        settings = getattr(self, "settings", None)
        homage_store = getattr(self, "homage_store", None)
        if not bool(getattr(settings, "pv_two_screens_enabled", False)) or homage_store is None:
            return False
        media_kind = self._homage_media_kind(event)
        if media_kind is None:
            return False
        sender = await event.get_sender()
        if not is_human_sender(sender):
            return False
        sender_id = int(sender.id)
        me = getattr(self, "me", None)
        if me is not None and sender_id == int(me.id):
            return False
        if not await homage_store.can_accept(sender_id):
            return False
        recorded = await homage_store.record(
            event_key=self._event_key(event),
            user_id=sender_id,
            message_id=int(event.id),
            media_kind=media_kind,
            username=getattr(sender, "username", None),
            display_name=display_name(sender),
        )
        if not recorded:
            return False
        origin_key = f"pv_reply:two-screens:homage:{sender_id}:{int(event.id)}"
        prewait = await self._next_block_wait("two_screens_followup", origin_key)
        await self._queue_sequence_continuation(
            peer=sender_id,
            origin_key=origin_key,
            block_key="two_screens_followup",
            branch_key=None,
            after_position="0",
            continuation="none",
            context={"peer": sender_id},
            variables={},
            prewait=prewait,
        )
        return True

    async def handle_event(self, event) -> bool:
        if event.is_private and not event.out and event.sender_id:
            lock = self._accept_locks[int(event.sender_id) % self._LOCK_STRIPES]
            async with lock:
                if await self._capture_two_screens_homage(event):
                    await self._observe_temperature_shadow(event)
                    await self._observe_response_memory_shadow(event)
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
        """Keep legacy text delivery untouched; divert only catalogued media rows."""
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
        reply_delay = max(MIN_WRITING_DELAY_SECONDS, int(self.settings.pv_reply_delay_seconds))
        rows = (
            ("link.preview", "link", POSITION_GAP * 2, "Link da prévia", "{preview_link}", 7),
            ("followup.link", "followup", POSITION_GAP * 2, "Link do follow-up", "{preview_link}", 3),
            ("reminder.link", "reminder_link", POSITION_GAP, "Reenvio do link", "{preview_link}", reply_delay),
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
