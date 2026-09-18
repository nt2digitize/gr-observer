"""Runtime adapter for Two Screens and Live capabilities inside linear PV.

The linear session remains the only conversation progression state. This adapter
reuses the existing photo/live persistence and existing Outbox action types.
"""

from __future__ import annotations

import logging

from .modules.pv_reply import (
    TWO_SCREENS_PHOTO_DELAY_RANGE_SECONDS,
    PvReplyModule,
    classify_live_response,
    display_name,
    is_human_sender,
    stable_delay_seconds,
)
from .pv_linear_capabilities import (
    LIVE_OPTIN_STEP_KEY,
    TWO_SCREENS_CHOICE_STEP_KEY,
    TWO_SCREENS_PROMPT_STEP_KEY,
    PvLinearCapabilityStore,
)
from .pv_linear_flow import linear_flow_enabled
from .pv_photo_flow import classify_photo_preference

log = logging.getLogger("gr-observer.pv-linear-capabilities")


class PvLinearCapabilityRuntimeMixin:
    """Attach old feature capabilities without giving them conversation authority."""

    def __init__(self, storage, settings):
        self.linear_capability_store = PvLinearCapabilityStore(storage.pool)
        self._linear_capability_ready = False
        super().__init__(storage, settings)

    @staticmethod
    def _fallback_action_key(peer: int, generation: int) -> str:
        return f"pv_reply:linear-two-screens:auto:{int(peer)}:{int(generation)}"

    def _fallback_delay(self, peer: int, generation: int) -> int:
        return stable_delay_seconds(
            f"linear-two-screens-fallback:{int(peer)}:{int(generation)}",
            *TWO_SCREENS_PHOTO_DELAY_RANGE_SECONDS,
        )

    async def on_connect(self, client, me) -> None:
        await super().on_connect(client, me)
        try:
            await self.linear_capability_store.ensure_ready()
            self._linear_capability_ready = True
            if linear_flow_enabled():
                await self._reconcile_linear_capabilities()
        except Exception as exc:
            self._linear_capability_ready = False
            log.warning("PV linear capabilities unavailable erro=%s", type(exc).__name__)

    async def on_disconnect(self) -> None:
        self._linear_capability_ready = False
        await super().on_disconnect()

    async def _resume_linear_capability(self, peer: int, step_key: str) -> bool:
        """Advance one persisted waiting lane exactly once."""
        current = await self.linear_capability_store.waiting_step(peer)
        if (
            current is None
            or str(current["status"]) != "waiting_reply"
            or str(current["step_key"] or "") != str(step_key)
        ):
            return False
        nxt = await self.linear_store.next_step(current["current_position"])
        if nxt is None:
            changed = await self.storage.pool.fetchval(
                """UPDATE pv_linear_sessions SET status='completed',updated_at=NOW()
                   WHERE user_id=$1 AND status='waiting_reply'
                     AND current_step_id=$2 RETURNING generation""",
                int(peer),
                int(current["current_step_id"]),
            )
            return changed is not None
        generation = await self.storage.pool.fetchval(
            """UPDATE pv_linear_sessions
               SET status='active',current_step_id=$2,current_position=$3,updated_at=NOW()
               WHERE user_id=$1 AND status='waiting_reply' AND current_step_id=$4
               RETURNING generation""",
            int(peer),
            int(nxt["id"]),
            nxt["linear_position"],
            int(current["current_step_id"]),
        )
        if generation is None:
            return False
        await self._queue_linear_step(int(peer), nxt, int(generation))
        return True

    async def _open_or_repair_two_screens_wait(self, peer: int, generation: int) -> None:
        if self.photo_flow is None:
            await self.linear_capability_store.complete_two_screens(peer)
            await self._resume_linear_capability(peer, TWO_SCREENS_CHOICE_STEP_KEY)
            return
        status = await self.linear_capability_store.two_screens_status(peer)
        action_key = self._fallback_action_key(peer, generation)
        delay = self._fallback_delay(peer, generation)
        if status is None:
            prepared = await self.linear_capability_store.prepare_two_screens(peer)
            status = "prompt_queued" if prepared == "ready" else await self.linear_capability_store.two_screens_status(peer)
        if status == "prompt_queued":
            opened = await self.photo_flow.open_choice_window_and_schedule_auto_photo(
                user_id=int(peer),
                expected_status="prompt_queued",
                action_key=action_key,
                delay_seconds=delay,
            )
            if opened:
                return
            status = await self.linear_capability_store.two_screens_status(peer)
        if status == "awaiting_choice":
            await self.linear_capability_store.ensure_two_screens_fallback(
                peer,
                action_key=action_key,
                delay_seconds=delay,
            )
            return
        if status == "photo_queued":
            return
        await self.linear_capability_store.complete_two_screens(peer)
        await self._resume_linear_capability(peer, TWO_SCREENS_CHOICE_STEP_KEY)

    async def _reconcile_linear_capabilities(self) -> None:
        """Repair waiting capability state after restart without resending copy/media."""
        for row in await self.linear_capability_store.waiting_capabilities():
            peer = int(row["user_id"])
            step_key = str(row["step_key"])
            generation = int(row["generation"])
            if step_key == TWO_SCREENS_CHOICE_STEP_KEY:
                await self._open_or_repair_two_screens_wait(peer, generation)
                continue
            if step_key == LIVE_OPTIN_STEP_KEY:
                status = await self.linear_capability_store.live_subscription_status(peer)
                if status is None:
                    await self.storage.mark_live_optin_asked(peer)
                elif status != "pending":
                    await self._resume_linear_capability(peer, LIVE_OPTIN_STEP_KEY)

    async def _active_linear_session(self, peer: int, step_id: int, generation: int):
        session = await self.storage.pool.fetchrow(
            "SELECT * FROM pv_linear_sessions WHERE user_id=$1",
            int(peer),
        )
        if (
            session is None
            or str(session["status"]) != "active"
            or int(session["generation"]) != int(generation)
            or int(session["current_step_id"] or 0) != int(step_id)
        ):
            return None
        return session

    async def action_send_linear_balloon(self, action: dict, effects) -> dict:
        if not linear_flow_enabled() or not self._linear_capability_ready:
            return await super().action_send_linear_balloon(action, effects)
        payload = action.get("payload") or {}
        peer = int(payload.get("peer") or 0)
        step_id = int(payload.get("step_id") or 0)
        generation = int(payload.get("generation") or 0)
        if peer <= 0 or step_id <= 0:
            return await super().action_send_linear_balloon(action, effects)
        session = await self._active_linear_session(peer, step_id, generation)
        if session is None:
            return await super().action_send_linear_balloon(action, effects)
        row = await self.linear_store.get_step(step_id)
        if row is None:
            return await super().action_send_linear_balloon(action, effects)
        step_key = str(row["step_key"] or "")

        if step_key in {TWO_SCREENS_PROMPT_STEP_KEY, TWO_SCREENS_CHOICE_STEP_KEY}:
            status = await self.linear_capability_store.two_screens_status(peer)
            if status is None:
                prepared = await self.linear_capability_store.prepare_two_screens(peer)
                status = "prompt_queued" if prepared == "ready" else await self.linear_capability_store.two_screens_status(peer)
            if status in {"completed", "stopped"} or (
                status not in {"prompt_queued"} and step_key == TWO_SCREENS_PROMPT_STEP_KEY
            ):
                return await self._linear_advance_without_send(peer, session, row)
            if step_key == TWO_SCREENS_CHOICE_STEP_KEY and status not in {"prompt_queued"}:
                return await self._linear_advance_without_send(peer, session, row)

        if step_key == LIVE_OPTIN_STEP_KEY:
            subscription = await self.linear_capability_store.live_subscription_status(peer)
            if subscription is not None:
                return await self._linear_advance_without_send(peer, session, row)

        result = await super().action_send_linear_balloon(action, effects)
        if not result.get("waiting_reply"):
            return result
        if step_key == TWO_SCREENS_CHOICE_STEP_KEY:
            await self._open_or_repair_two_screens_wait(peer, generation)
        elif step_key == LIVE_OPTIN_STEP_KEY:
            await self.storage.mark_live_optin_asked(peer)
        return result

    async def handle_event(self, event) -> bool:
        if not linear_flow_enabled() or not self._linear_capability_ready:
            return await super().handle_event(event)
        if not event.is_private or event.out or not event.sender_id:
            return await super().handle_event(event)
        sender = await event.get_sender()
        if not is_human_sender(sender):
            return False
        peer = int(sender.id)
        if self.me is not None and peer == int(self.me.id):
            return False

        waiting = await self.linear_capability_store.waiting_step(peer)
        waiting_key = (
            str(waiting["step_key"] or "")
            if waiting is not None and str(waiting["status"]) == "waiting_reply"
            else ""
        )
        raw_text = event.raw_text or ""

        if waiting_key == TWO_SCREENS_CHOICE_STEP_KEY:
            if self.photo_flow is None:
                await self.linear_capability_store.complete_two_screens(peer)
                await self._resume_linear_capability(peer, TWO_SCREENS_CHOICE_STEP_KEY)
                return False
            outcome = await self.photo_flow.accept_choice(
                event_key=self._event_key(event),
                user_id=peer,
                message_id=int(event.id),
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
                requested_slot=classify_photo_preference(raw_text),
                first_photo_delay_seconds=self._fallback_delay(
                    peer,
                    int(waiting["generation"]),
                ),
            )
            if outcome in {"media_unavailable", "completed"}:
                await self.linear_capability_store.complete_two_screens(peer)
                await self._resume_linear_capability(peer, TWO_SCREENS_CHOICE_STEP_KEY)
            elif outcome == "ignored":
                status = await self.linear_capability_store.two_screens_status(peer)
                if status in {"completed", "stopped"}:
                    await self._resume_linear_capability(peer, TWO_SCREENS_CHOICE_STEP_KEY)
            return False

        live_kind = classify_live_response(raw_text)
        if waiting_key == LIVE_OPTIN_STEP_KEY:
            outcome = await self.linear_capability_store.handle_live_optin_response(
                peer,
                live_kind,
            )
            if outcome in {"subscribed", "declined", "not_pending"}:
                await self._resume_linear_capability(peer, LIVE_OPTIN_STEP_KEY)
            return False

        active_live = await self.linear_capability_store.handle_active_live_response(
            peer,
            live_kind,
        )
        if active_live is not None:
            return False
        return await super().handle_event(event)

    async def action_auto_queue_two_screens_photo(self, action: dict, effects) -> dict:
        if not linear_flow_enabled() or not self._linear_capability_ready:
            return await super().action_auto_queue_two_screens_photo(action, effects)
        peer = int((action.get("payload") or {}).get("peer") or 0)
        if not await self.linear_capability_store.waiting_on(peer, TWO_SCREENS_CHOICE_STEP_KEY):
            return {"queued": False, "reason": "linear_capability_not_waiting"}
        if self.photo_flow is None:
            await self.linear_capability_store.complete_two_screens(peer)
            await self._resume_linear_capability(peer, TWO_SCREENS_CHOICE_STEP_KEY)
            return {"queued": False, "reason": "durable_store_unavailable"}
        outcome = await self.photo_flow.auto_queue_first_photo(peer)
        if outcome in {"media_unavailable", "completed"}:
            await self.linear_capability_store.complete_two_screens(peer)
            await self._resume_linear_capability(peer, TWO_SCREENS_CHOICE_STEP_KEY)
        return {"queued": outcome == "photo_queued", "outcome": outcome}

    async def action_send_two_screens_photo(self, action: dict, effects) -> dict:
        if not linear_flow_enabled() or not self._linear_capability_ready:
            return await super().action_send_two_screens_photo(action, effects)
        peer = int((action.get("payload") or {}).get("peer") or 0)
        if not await self.linear_capability_store.waiting_on(peer, TWO_SCREENS_CHOICE_STEP_KEY):
            return {"sent": False, "reason": "linear_capability_not_waiting"}
        payload = dict(action.get("payload") or {})
        payload["final"] = True
        linear_action = {**action, "payload": payload}
        result = await PvReplyModule.action_send_two_screens_photo(self, linear_action, effects)
        if result.get("sent"):
            await self.linear_capability_store.complete_two_screens(peer)
            await self._resume_linear_capability(peer, TWO_SCREENS_CHOICE_STEP_KEY)
        elif result.get("reason") == "media_slot_missing":
            await self.linear_capability_store.complete_two_screens(peer)
            await self._resume_linear_capability(peer, TWO_SCREENS_CHOICE_STEP_KEY)
        elif result.get("reason") == "state_changed":
            status = await self.linear_capability_store.two_screens_status(peer)
            if status in {"completed", "stopped"}:
                await self._resume_linear_capability(peer, TWO_SCREENS_CHOICE_STEP_KEY)
        return result

    async def action_send_live_optin(self, action: dict, effects) -> dict:
        if linear_flow_enabled():
            return {"sent": False, "reason": "linear_capability_owned"}
        return await super().action_send_live_optin(action, effects)

    async def action_send_live_invite(self, action: dict, effects) -> dict:
        if linear_flow_enabled():
            return await PvReplyModule.action_send_live_invite(self, action, effects)
        return await super().action_send_live_invite(action, effects)

    async def action_send_live_remarketing(self, action: dict, effects) -> dict:
        if linear_flow_enabled():
            return await PvReplyModule.action_send_live_remarketing(self, action, effects)
        return await super().action_send_live_remarketing(action, effects)

    async def action_send_live_link(self, action: dict, effects) -> dict:
        if linear_flow_enabled():
            return await PvReplyModule.action_send_live_link(self, action, effects)
        return await super().action_send_live_link(action, effects)

    async def action_close_live_recipient(self, action: dict, effects) -> dict:
        if linear_flow_enabled():
            return await PvReplyModule.action_close_live_recipient(self, action, effects)
        return await super().action_close_live_recipient(action, effects)
