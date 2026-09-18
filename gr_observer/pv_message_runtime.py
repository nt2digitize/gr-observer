"""Runtime adapter that applies configurable PV copy without changing PV business states."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from typing import Any

from telethon import functions, types

from .modules.pv_reply import (
    LIVE_REMARKETING_DELAY_SECONDS,
    TWO_SCREENS_PHOTO_DELAY_RANGE_SECONDS,
    stable_delay_seconds as legacy_stable_delay,
)
from .pv_message_steps import has_link, pre_send_wait_seconds, stable_delay_seconds

log = logging.getLogger("gr-observer.pv-messages")
ZERO_POSITION = "0"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class PvMessageRuntimeMixin:
    """Message copy/timing adapter for the existing ``pv_reply`` rib."""

    def register_actions(self, writer) -> None:
        handlers = {
            "send_greeting": self.action_send_greeting,
            "send_link": self.action_send_link,
            "send_followup": self.action_send_followup,
            "send_weekly_question": self.action_send_weekly_question,
            "send_live_optin": self.action_send_live_optin,
            "send_live_invite": self.action_send_live_invite,
            "send_live_remarketing": self.action_send_live_remarketing,
            "send_live_link": self.action_send_live_link,
            "send_two_screens_prompt": self.action_send_two_screens_prompt,
            "send_two_screens_question": self.action_send_two_screens_question,
            "send_two_screens_limit": self.action_send_two_screens_limit,
            "send_two_screens_followup": self.action_send_two_screens_followup,
            "send_two_screens_retry": self.action_send_two_screens_retry,
            "send_two_screens_photo": self.action_send_two_screens_photo,
            "send_message_step": self.action_send_message_step,
            "auto_queue_two_screens_photo": self.action_auto_queue_two_screens_photo,
            "close_live_recipient": self.action_close_live_recipient,
        }
        for action_type, handler in handlers.items():
            writer.register(self.module_id, action_type, handler)

    async def handle_event(self, event) -> bool:
        result = await super().handle_event(event)
        if event.is_private and not event.out and event.sender_id:
            await self._retime_event_actions(event)
        return result

    async def _retime_event_actions(self, event) -> None:
        """Retiming is limited to intents created by this same inbound event."""
        event_key = self._event_key(event)
        peer = int(event.sender_id)
        await self._retime_pending_action(
            f"pv_reply:greeting:telegram-user:{event_key}", "greeting"
        )
        await self._retime_pending_action(
            f"pv_reply:link:telegram-user:{event_key}", "link"
        )
        question_block = (
            "two_screens_preference"
            if self.settings.pv_two_screens_enabled
            else "two_screens_question"
        )
        retry_block = (
            "two_screens_retry_preference"
            if self.settings.pv_two_screens_enabled
            else "two_screens_retry"
        )
        for action_type, block in (
            ("send_live_link", "live_link"),
            ("send_two_screens_question", question_block),
            ("send_two_screens_retry", retry_block),
        ):
            row = await self.storage.pool.fetchrow(
                """SELECT action_key FROM outbox_actions
                   WHERE module_id='pv_reply' AND action_type=$1
                     AND payload->>'peer'=$2 AND status='pending'
                     AND created_at >= NOW()-INTERVAL '10 seconds'
                   ORDER BY id DESC LIMIT 1""",
                action_type,
                str(peer),
            )
            if row:
                await self._retime_pending_action(str(row["action_key"]), block)

    async def _first_step_plan(
        self,
        block_key: str,
        stable_key: str,
        *,
        extra_seconds: int = 0,
    ):
        await self.message_store.ensure_ready()
        branch = await self.message_store.choose_branch(block_key, stable_key)
        row = await self.message_store.first_step(block_key, branch)
        if row is None:
            return branch, None, 0.0, 0.0
        rendered = self.message_store.render(
            str(row["content"]), preview_link=self.settings.pv_preview_link
        )
        median = max(0, int(row["median_delay_seconds"]) + int(extra_seconds))
        total = stable_delay_seconds(f"{stable_key}:first:{row['id']}", median)
        prewait, typing = pre_send_wait_seconds(
            total, rendered, f"{stable_key}:first:{row['id']}"
        )
        return branch, row, prewait, typing

    async def _retime_pending_action(
        self,
        action_key: str,
        block_key: str,
        *,
        extra_seconds: int = 0,
    ) -> None:
        _, row, prewait, _ = await self._first_step_plan(
            block_key, action_key, extra_seconds=extra_seconds
        )
        if row is None:
            prewait = 0.0
        await self.storage.pool.execute(
            """UPDATE outbox_actions
               SET available_at=created_at+($2::double precision*INTERVAL '1 second')
               WHERE action_key=$1 AND status='pending'""",
            action_key,
            float(max(0.0, prewait)),
        )

    async def _next_block_wait(
        self,
        block_key: str,
        stable_key: str,
        *,
        extra_seconds: int = 0,
    ) -> int:
        _, _, prewait, _ = await self._first_step_plan(
            block_key, stable_key, extra_seconds=extra_seconds
        )
        return max(0, int(math.ceil(prewait)))

    async def _show_typing(self, effects, peer: int, seconds: float) -> None:
        """Short, best-effort typing phase inside the one active Writer."""
        if seconds <= 0:
            return
        try:
            if effects.before_user_write is not None:
                await effects.before_user_write()
            input_peer = await effects.client.get_input_entity(peer)
            await effects.client(
                functions.messages.SetTypingRequest(
                    peer=input_peer,
                    action=types.SendMessageTypingAction(),
                )
            )
        except Exception as exc:
            log.debug("Indicador digitando indisponível peer=%s: %s", peer, exc)
        await asyncio.sleep(min(8.0, max(0.0, float(seconds))))

    async def _send_row(
        self,
        *,
        effects,
        peer: int,
        row,
        origin_key: str,
        variables: dict,
    ) -> dict:
        text = self.message_store.render(
            str(row["content"]),
            preview_link=self.settings.pv_preview_link,
            live_link=str(variables.get("live_link") or ""),
        ).strip()
        if not text:
            return {
                "sent": False,
                "reason": "empty_after_render",
                "step_id": int(row["id"]),
            }
        total = stable_delay_seconds(
            f"{origin_key}:step:{row['id']}", int(row["median_delay_seconds"])
        )
        _, typing = pre_send_wait_seconds(
            total, text, f"{origin_key}:step:{row['id']}"
        )
        await self._show_typing(effects, peer, typing)
        effect_key = f"{origin_key}:message:{row['id']}"
        sender = (
            effects.send_text_preview
            if has_link(text) or str(row["kind"]) == "link"
            else effects.send_text
        )
        result = await sender(peer, text, effect_key)
        return {"sent": True, "step_id": int(row["id"]), **result}

    async def _queue_sequence_continuation(
        self,
        *,
        peer: int,
        origin_key: str,
        block_key: str,
        branch_key: str | None,
        after_position,
        continuation: str,
        context: dict,
        variables: dict,
        prewait: float,
    ) -> bool:
        digest = hashlib.sha256(
            f"{origin_key}:{block_key}:{branch_key}:{after_position}".encode("utf-8")
        ).hexdigest()[:24]
        action_key = f"pv_reply:message-step:{digest}"
        inserted = await self.storage.pool.fetchval(
            """INSERT INTO outbox_actions(
               action_key,module_id,action_type,payload,available_at)
               VALUES($1,'pv_reply','send_message_step',$2::jsonb,
                 NOW()+($3::double precision*INTERVAL '1 second'))
               ON CONFLICT(action_key) DO NOTHING RETURNING id""",
            action_key,
            _json(
                {
                    "peer": int(peer),
                    "origin_key": origin_key,
                    "block_key": block_key,
                    "branch_key": branch_key,
                    "after_position": str(after_position),
                    "continuation": continuation,
                    "context": context,
                    "variables": variables,
                }
            ),
            float(max(0.0, prewait)),
        )
        return inserted is not None

    async def _run_block(
        self,
        *,
        action: dict,
        effects,
        block_key: str,
        continuation: str,
        context: dict | None = None,
        variables: dict | None = None,
        first_delay_applied: bool = True,
    ) -> dict:
        await self.message_store.ensure_ready()
        peer = int(action["payload"]["peer"])
        origin_key = str(action["action_key"])
        context = dict(context or {})
        variables = dict(variables or {})
        branch = await self.message_store.choose_branch(block_key, origin_key)
        first = await self.message_store.first_step(block_key, branch)
        if first is None:
            return await self._complete_sequence(continuation, context, effects)
        if not first_delay_applied:
            rendered = self.message_store.render(
                str(first["content"]),
                preview_link=self.settings.pv_preview_link,
                live_link=str(variables.get("live_link") or ""),
            )
            total = stable_delay_seconds(
                f"{origin_key}:step:{first['id']}",
                int(first["median_delay_seconds"]),
            )
            prewait, _ = pre_send_wait_seconds(
                total, rendered, f"{origin_key}:step:{first['id']}"
            )
            if prewait > 0:
                await self._queue_sequence_continuation(
                    peer=peer,
                    origin_key=origin_key,
                    block_key=block_key,
                    branch_key=branch,
                    after_position=ZERO_POSITION,
                    continuation=continuation,
                    context=context,
                    variables=variables,
                    prewait=prewait,
                )
                return {
                    "sent": False,
                    "queued": True,
                    "reason": "first_step_delayed",
                }
        sent = await self._send_row(
            effects=effects,
            peer=peer,
            row=first,
            origin_key=origin_key,
            variables=variables,
        )
        return await self._continue_after_row(
            effects=effects,
            peer=peer,
            origin_key=origin_key,
            block_key=block_key,
            branch_key=branch,
            row=first,
            continuation=continuation,
            context=context,
            variables=variables,
            sent=sent,
        )

    async def _continue_after_row(
        self,
        *,
        effects,
        peer: int,
        origin_key: str,
        block_key: str,
        branch_key: str | None,
        row,
        continuation: str,
        context: dict,
        variables: dict,
        sent: dict,
    ) -> dict:
        current = row
        results = [sent]
        while True:
            nxt = await self.message_store.next_step(
                block_key, branch_key, current["position"]
            )
            if nxt is None:
                completed = await self._complete_sequence(
                    continuation, context, effects
                )
                return {
                    "sent": any(item.get("sent") for item in results),
                    "steps": results,
                    **completed,
                }
            rendered = self.message_store.render(
                str(nxt["content"]),
                preview_link=self.settings.pv_preview_link,
                live_link=str(variables.get("live_link") or ""),
            )
            total = stable_delay_seconds(
                f"{origin_key}:step:{nxt['id']}",
                int(nxt["median_delay_seconds"]),
            )
            prewait, _ = pre_send_wait_seconds(
                total, rendered, f"{origin_key}:step:{nxt['id']}"
            )
            if prewait > 0:
                queued = await self._queue_sequence_continuation(
                    peer=peer,
                    origin_key=origin_key,
                    block_key=block_key,
                    branch_key=branch_key,
                    after_position=current["position"],
                    continuation=continuation,
                    context=context,
                    variables=variables,
                    prewait=prewait,
                )
                return {
                    "sent": any(item.get("sent") for item in results),
                    "steps": results,
                    "queued": queued,
                }
            item = await self._send_row(
                effects=effects,
                peer=peer,
                row=nxt,
                origin_key=origin_key,
                variables=variables,
            )
            results.append(item)
            current = nxt

    async def action_send_message_step(self, action: dict, effects) -> dict:
        """Resolve current next row; a deleted pending row is simply skipped."""
        payload = action["payload"]
        peer = int(payload["peer"])
        block_key = str(payload["block_key"])
        branch_key = payload.get("branch_key")
        nxt = await self.message_store.next_step(
            block_key, branch_key, payload["after_position"]
        )
        if nxt is None:
            return await self._complete_sequence(
                str(payload["continuation"]),
                dict(payload.get("context") or {}),
                effects,
            )
        sent = await self._send_row(
            effects=effects,
            peer=peer,
            row=nxt,
            origin_key=str(payload["origin_key"]),
            variables=dict(payload.get("variables") or {}),
        )
        return await self._continue_after_row(
            effects=effects,
            peer=peer,
            origin_key=str(payload["origin_key"]),
            block_key=block_key,
            branch_key=branch_key,
            row=nxt,
            continuation=str(payload["continuation"]),
            context=dict(payload.get("context") or {}),
            variables=dict(payload.get("variables") or {}),
            sent=sent,
        )

    async def _complete_sequence(
        self, continuation: str, context: dict, effects
    ) -> dict:
        peer = int(context.get("peer", 0))
        if continuation == "none":
            return {"advanced": False}
        if continuation == "greeting":
            delay = await self._next_block_wait(
                "link", f"pv_reply:auto-link:{peer}"
            )
            if self.journey is not None:
                advanced = await self.journey.greeting_sent_and_schedule_link(
                    user_id=peer, delay_seconds=delay
                )
            else:
                advanced = await self.storage.mark_pv_greeting_sent(peer)
            return {"advanced": advanced}
        if continuation == "link":
            followup_delay = await self._next_block_wait(
                "followup", f"pv_reply:followup:{peer}:1"
            )
            weekly_delay = await self._next_block_wait(
                "weekly", f"pv_reply:weekly-question:{peer}:1"
            )
            two_delay = await self._next_block_wait(
                "two_screens_prompt", f"pv_reply:two-screens:prompt:{peer}"
            )
            advanced = await self.storage.mark_pv_link_sent_and_schedule(
                user_id=peer,
                delay_seconds=followup_delay,
                weekly_delay_seconds=weekly_delay,
                max_cycles=self.settings.pv_followup_max_cycles,
                two_screens_enabled=self.settings.pv_two_screens_enabled,
                two_screens_delay_seconds=two_delay,
            )
            if advanced:
                if self.journey is not None and self.settings.pv_two_screens_enabled:
                    await self.journey.ensure_two_screens_after_link(
                        user_id=peer, delay_seconds=two_delay
                    )
                live_delay = await self._next_block_wait(
                    "live_optin", f"pv_reply:live-optin:{peer}"
                )
                await self.storage.queue_live_optin(peer, live_delay)
            return {"advanced": advanced}
        if continuation == "followup":
            cycle = int(context["cycle"])
            next_cycle = cycle + 1
            if next_cycle <= self.settings.pv_followup_max_cycles:
                next_delay = await self._next_block_wait(
                    "followup",
                    f"pv_reply:followup:{peer}:{next_cycle}",
                    extra_seconds=24 * 3600 * (next_cycle - 1),
                )
            else:
                next_delay = 0
            weekly_delay = await self._next_block_wait(
                "weekly", f"pv_reply:weekly-question:{peer}:1"
            )
            advanced = await self.storage.complete_pv_followup_and_schedule_next(
                user_id=peer,
                completed_cycle=cycle,
                next_delay_seconds=next_delay,
                weekly_delay_seconds=weekly_delay,
                max_cycles=self.settings.pv_followup_max_cycles,
            )
            return {"advanced": advanced, "cycle": cycle}
        if continuation == "weekly":
            cycle = int(context["cycle"])
            delay = await self._next_block_wait(
                "weekly", f"pv_reply:weekly-question:{peer}:{cycle + 1}"
            )
            advanced = await self.storage.complete_pv_weekly_and_schedule_next(
                user_id=peer, completed_cycle=cycle, delay_seconds=delay
            )
            return {"advanced": advanced, "cycle": cycle}
        if continuation == "live_optin":
            await self.storage.mark_live_optin_asked(peer)
            return {"advanced": True}
        if continuation == "live_invite":
            campaign_id = int(context["campaign_id"])
            delay = await self._next_block_wait(
                "live_remarketing",
                f"pv_reply:live-remarketing:{campaign_id}:{peer}",
            )
            await self.storage.mark_live_invite_and_schedule_remarketing(
                campaign_id=campaign_id,
                user_id=peer,
                delay_seconds=delay,
            )
            return {"advanced": True}
        if continuation == "live_remarketing":
            campaign_id = int(context["campaign_id"])
            await self.storage.mark_live_remarketing_sent(
                campaign_id, peer, LIVE_REMARKETING_DELAY_SECONDS
            )
            return {"advanced": True}
        if continuation == "live_link":
            await self.storage.mark_live_link_delivered(
                int(context["campaign_id"]), peer
            )
            return {"advanced": True}
        if continuation == "two_prompt":
            if self.photo_flow is not None:
                advanced = await self.photo_flow.open_choice_window_and_schedule_auto_photo(
                    user_id=peer,
                    expected_status="prompt_queued",
                    action_key=f"pv_reply:two-screens:auto-photo:{peer}",
                    delay_seconds=legacy_stable_delay(
                        f"two-screens-auto-photo:{peer}",
                        *TWO_SCREENS_PHOTO_DELAY_RANGE_SECONDS,
                    ),
                )
            else:
                advanced = await self.storage.advance_two_screens(
                    peer, "prompt_queued", "awaiting_choice"
                )
            return {"advanced": advanced}
        if continuation == "two_question":
            if self.photo_flow is not None:
                advanced = await self.photo_flow.open_choice_window_and_schedule_auto_photo(
                    user_id=peer,
                    expected_status="question_queued",
                    action_key=f"pv_reply:two-screens:auto-photo:{peer}",
                    delay_seconds=legacy_stable_delay(
                        f"two-screens-auto-photo:{peer}",
                        *TWO_SCREENS_PHOTO_DELAY_RANGE_SECONDS,
                    ),
                )
            else:
                advanced = await self.storage.advance_two_screens(
                    peer, "question_queued", "awaiting_choice"
                )
            return {"advanced": advanced}
        if continuation == "two_question_legacy":
            delay = await self._next_block_wait(
                "two_screens_limit", f"pv_reply:two-screens:limit:{peer}"
            )
            advanced = await self.storage.queue_next_two_screens_action(
                peer,
                expected="question_queued",
                next_status="limit_queued",
                action_type="send_two_screens_limit",
                action_key=f"pv_reply:two-screens:limit:{peer}",
                delay_seconds=delay,
            )
            return {"advanced": advanced}
        if continuation == "two_limit":
            delay = await self._next_block_wait(
                "two_screens_followup",
                f"pv_reply:two-screens:followup:{peer}",
            )
            advanced = await self.storage.queue_next_two_screens_action(
                peer,
                expected="limit_queued",
                next_status="followup_queued",
                action_type="send_two_screens_followup",
                action_key=f"pv_reply:two-screens:followup:{peer}",
                delay_seconds=delay,
            )
            return {"advanced": advanced}
        if continuation == "two_followup":
            advanced = await self.storage.advance_two_screens(
                peer, "followup_queued", "awaiting_choice"
            )
            return {"advanced": advanced}
        if continuation == "photo_caption":
            advanced = await self.photo_flow.mark_photo_sent(
                peer,
                str(context["slot"]),
                final=bool(context.get("final", False)),
            )
            return {"advanced": advanced}
        raise RuntimeError(f"continuação PV desconhecida: {continuation}")

    async def action_send_greeting(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self.storage.pv_action_allowed(peer, "greeting_queued"):
            return {"sent": False, "reason": "state_changed"}
        return await self._run_block(
            action=action,
            effects=effects,
            block_key="greeting",
            continuation="greeting",
            context={"peer": peer},
        )

    async def action_send_link(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self.storage.pv_action_allowed(peer, "link_queued"):
            return {"sent": False, "reason": "state_changed"}
        return await self._run_block(
            action=action,
            effects=effects,
            block_key="link",
            continuation="link",
            context={"peer": peer},
        )

    async def action_send_followup(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        cycle = int(action["payload"]["cycle"])
        if not await self.storage.pv_action_allowed(peer, "following_up", cycle):
            return {"sent": False, "reason": "state_changed", "cycle": cycle}
        return await self._run_block(
            action=action,
            effects=effects,
            block_key="followup",
            continuation="followup",
            context={"peer": peer, "cycle": cycle},
        )

    async def action_send_weekly_question(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        cycle = int(action["payload"]["cycle"])
        if not await self.storage.pv_action_allowed(peer, "weekly", cycle):
            return {"sent": False, "reason": "state_changed", "cycle": cycle}
        return await self._run_block(
            action=action,
            effects=effects,
            block_key="weekly",
            continuation="weekly",
            context={"peer": peer, "cycle": cycle},
        )

    async def action_send_live_optin(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self.storage.live_optin_allowed(peer):
            return {"sent": False, "reason": "already_asked"}
        return await self._run_block(
            action=action,
            effects=effects,
            block_key="live_optin",
            continuation="live_optin",
            context={"peer": peer},
        )

    async def action_send_live_invite(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        campaign_id = int(action["payload"]["campaign_id"])
        if not await self.storage.live_recipient_allowed(campaign_id, peer, "queued"):
            return {"sent": False, "reason": "state_changed"}
        return await self._run_block(
            action=action,
            effects=effects,
            block_key="live_invite",
            continuation="live_invite",
            context={"peer": peer, "campaign_id": campaign_id},
            first_delay_applied=False,
        )

    async def action_send_live_remarketing(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        campaign_id = int(action["payload"]["campaign_id"])
        if not await self.storage.live_recipient_allowed(
            campaign_id, peer, "awaiting"
        ):
            return {"sent": False, "reason": "answered_or_stopped"}
        return await self._run_block(
            action=action,
            effects=effects,
            block_key="live_remarketing",
            continuation="live_remarketing",
            context={"peer": peer, "campaign_id": campaign_id},
        )

    async def action_send_live_link(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        campaign_id = int(action["payload"]["campaign_id"])
        if not await self.storage.live_recipient_allowed(
            campaign_id, peer, "link_queued"
        ):
            return {"sent": False, "reason": "state_changed"}
        link = await self.storage.live_campaign_link(campaign_id)
        if not link:
            return {"sent": False, "reason": "campaign_missing"}
        return await self._run_block(
            action=action,
            effects=effects,
            block_key="live_link",
            continuation="live_link",
            context={"peer": peer, "campaign_id": campaign_id},
            variables={"live_link": link},
        )

    async def action_send_two_screens_prompt(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self._contact_allows_automation(peer):
            return {"sent": False, "reason": "contact_stopped"}
        if not await self.storage.two_screens_action_allowed(peer, "prompt_queued"):
            return {"sent": False, "reason": "state_changed"}
        return await self._run_block(
            action=action,
            effects=effects,
            block_key="two_screens_prompt",
            continuation="two_prompt",
            context={"peer": peer},
        )

    async def action_send_two_screens_question(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self._contact_allows_automation(peer):
            return {"sent": False, "reason": "contact_stopped"}
        if not await self.storage.two_screens_action_allowed(peer, "question_queued"):
            return {"sent": False, "reason": "state_changed"}
        if self.settings.pv_two_screens_enabled:
            return await self._run_block(
                action=action,
                effects=effects,
                block_key="two_screens_preference",
                continuation="two_question",
                context={"peer": peer},
            )
        return await self._run_block(
            action=action,
            effects=effects,
            block_key="two_screens_question",
            continuation="two_question_legacy",
            context={"peer": peer},
        )

    async def action_send_two_screens_limit(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self._contact_allows_automation(peer):
            return {"sent": False, "reason": "contact_stopped"}
        if not await self.storage.two_screens_action_allowed(peer, "limit_queued"):
            return {"sent": False, "reason": "state_changed"}
        return await self._run_block(
            action=action,
            effects=effects,
            block_key="two_screens_limit",
            continuation="two_limit",
            context={"peer": peer},
        )

    async def action_send_two_screens_followup(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self._contact_allows_automation(peer):
            return {"sent": False, "reason": "contact_stopped"}
        if not await self.storage.two_screens_action_allowed(peer, "followup_queued"):
            return {"sent": False, "reason": "state_changed"}
        return await self._run_block(
            action=action,
            effects=effects,
            block_key="two_screens_followup",
            continuation="two_followup",
            context={"peer": peer},
        )

    async def action_send_two_screens_retry(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self._contact_allows_automation(peer):
            return {"sent": False, "reason": "contact_stopped"}
        if not await self.storage.two_screens_action_allowed(peer, "awaiting_choice"):
            return {"sent": False, "reason": "state_changed"}
        block = (
            "two_screens_retry_preference"
            if self.settings.pv_two_screens_enabled
            else "two_screens_retry"
        )
        return await self._run_block(
            action=action,
            effects=effects,
            block_key=block,
            continuation="none",
            context={"peer": peer},
        )

    async def action_send_two_screens_photo(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        slot = str(action["payload"]["slot"])
        final = bool(action["payload"].get("final", False))
        if not await self._contact_allows_automation(peer):
            return {"sent": False, "reason": "contact_stopped", "slot": slot}
        if not await self.storage.two_screens_action_allowed(peer, "photo_queued"):
            return {"sent": False, "reason": "state_changed"}
        media = await self.storage.two_screens_media_slot(slot)
        if not media:
            return {"sent": False, "reason": "media_slot_missing", "slot": slot}
        if self.photo_flow is None:
            return await super().action_send_two_screens_photo(action, effects)
        photo = await effects.send_catalogued_media(
            int(media["source_peer"]),
            int(media["source_message_id"]),
            peer,
            f"{action['action_key']}:photo",
            spoiler=True,
            ttl_seconds=30,
        )
        caption = await self._run_block(
            action=action,
            effects=effects,
            block_key=f"photo_caption_{slot}",
            continuation="photo_caption",
            context={"peer": peer, "slot": slot, "final": final},
            first_delay_applied=False,
        )
        return {"sent": True, "slot": slot, "photo": photo, "caption": caption}
