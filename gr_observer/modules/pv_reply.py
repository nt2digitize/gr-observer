"""Delayed, stateful replies to inbound private messages from human accounts."""

from __future__ import annotations

import asyncio
import hashlib
import logging

from ..catalog import (
    CAMPAIGNS,
    DIALOGS,
    LIVE_INVITE_VARIANTS,
    LIVE_REMARKETING_VARIANTS,
    PV_GREETING_VARIANTS,
    PV_RESPONSE_RULES,
    normalize_text,
)
from ..pv_photo_flow import (
    PvPhotoFlowStore,
    caption_for_slot,
    classify_photo_preference,
)

log = logging.getLogger("gr-observer.pv-reply")
LINK_BALLOON_DELAY_SECONDS = 7
LIVE_OPTIN_DELAY_SECONDS = 20 * 60
LIVE_REMARKETING_DELAY_SECONDS = 10 * 60
TWO_SCREENS_PROMPT_DELAY_SECONDS = 20
TWO_SCREENS_BALLOON_DELAY_RANGE_SECONDS = (5, 8)
TWO_SCREENS_PHOTO_DELAY_RANGE_SECONDS = (4, 7)


def classify_response(text: str) -> str:
    normalized = normalize_text(text)
    rules = sorted(PV_RESPONSE_RULES.items(), key=lambda item: item[1]["order"])
    for response_kind, rule in rules:
        exact = {normalize_text(value) for value in rule["exact"]}
        contains = tuple(normalize_text(value) for value in rule["contains"])
        if normalized in exact or any(value in normalized for value in contains):
            return response_kind
    return "unknown"


def classify_live_response(text: str) -> str:
    normalized = normalize_text(text)
    if any(value in normalized for value in ("parar", "não envie", "nao envie")):
        return "opt_out"
    if normalized in {"não", "nao", "não quero", "nao quero"}:
        return "negative"
    if normalized in {"sim", "quero", "manda", "pode", "pode mandar"}:
        return "positive"
    if any(value in normalized for value in ("manda o link", "quero ver")):
        return "positive"
    return "unknown"


def classify_two_screens_response(text: str) -> str:
    """Classify the conversational acknowledgement without changing persistence."""
    normalized = normalize_text(text)
    if any(value in normalized for value in ("parar", "não envie", "nao envie")):
        return "opt_out"
    if normalized in {"não", "nao", "não quero", "nao quero"}:
        return "negative"
    if normalized in {"sim", "quero", "pode", "manda", "faz"}:
        return "positive"
    return "unknown"


def classify_two_screens_choice(text: str) -> str | None:
    """Compatibility wrapper around the simple three-slot preference parser."""
    return classify_photo_preference(text)


def stable_delay_seconds(key: str, minimum: int, maximum: int) -> int:
    """Choose an inclusive, retry-stable delay inside the approved window."""
    low, high = sorted((int(minimum), int(maximum)))
    width = (high - low) + 1
    return low + (int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") % width)


def is_human_sender(sender) -> bool:
    """Telegram can prove "not marked as bot", not real-world identity."""
    if sender is None:
        return False
    sender_id = getattr(sender, "id", None)
    return bool(
        sender_id
        and int(sender_id) > 0
        and int(sender_id) != 777000
        and not getattr(sender, "bot", False)
        and not getattr(sender, "deleted", False)
        and not getattr(sender, "support", False)
    )


def display_name(sender) -> str:
    return " ".join(
        value
        for value in (
            getattr(sender, "first_name", None),
            getattr(sender, "last_name", None),
        )
        if value
    )[:200]


def render_campaign(campaign_id: str, preview_link: str) -> str:
    return CAMPAIGNS[campaign_id]["text"].format(preview_link=preview_link)


def greeting_for(action_key: str) -> str:
    """Choose one approved greeting while keeping retries idempotent."""
    digest = hashlib.sha256(action_key.encode()).digest()
    index = int.from_bytes(digest[:8], "big") % len(PV_GREETING_VARIANTS)
    return PV_GREETING_VARIANTS[index]


def variant_for(action_key: str, variants: tuple[str, ...]) -> str:
    digest = hashlib.sha256(action_key.encode()).digest()
    return variants[int.from_bytes(digest[:8], "big") % len(variants)]


def followup_delay_seconds(
    user_id: int,
    cycle: int,
    minimum_hours: float,
    maximum_hours: float,
) -> int:
    """Stable jitter: 23–25h, then one extra day on each next cycle."""
    if cycle < 1:
        raise ValueError("ciclo precisa começar em 1")
    low, high = sorted((float(minimum_hours), float(maximum_hours)))
    digest = hashlib.sha256(f"pv:{user_id}:{cycle}".encode()).digest()
    fraction = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
    base_hours = low + ((high - low) * fraction)
    progressive_hours = base_hours + (24 * (cycle - 1))
    return round(progressive_hours * 3600)


class PvReplyModule:
    module_id = "pv_reply"

    def __init__(self, storage, settings):
        self.storage = storage
        self.settings = settings
        self.photo_flow = PvPhotoFlowStore(storage.pool)
        self.client = None
        self.me = None

    async def on_connect(self, client, me) -> None:
        self.client = client
        self.me = me

    async def on_disconnect(self) -> None:
        self.client = None
        self.me = None

    def register_actions(self, writer) -> None:
        writer.register(self.module_id, "send_greeting", self.action_send_greeting)
        writer.register(self.module_id, "send_link", self.action_send_link)
        writer.register(self.module_id, "send_followup", self.action_send_followup)
        writer.register(
            self.module_id, "send_reminder_link", self.action_send_reminder_link
        )
        writer.register(
            self.module_id, "send_weekly_question", self.action_send_weekly_question
        )
        writer.register(self.module_id, "send_live_optin", self.action_send_live_optin)
        writer.register(self.module_id, "send_live_invite", self.action_send_live_invite)
        writer.register(
            self.module_id, "send_live_remarketing", self.action_send_live_remarketing
        )
        writer.register(self.module_id, "send_live_link", self.action_send_live_link)
        writer.register(
            self.module_id,
            "send_two_screens_prompt",
            self.action_send_two_screens_prompt,
        )
        writer.register(
            self.module_id,
            "send_two_screens_question",
            self.action_send_two_screens_question,
        )
        writer.register(
            self.module_id,
            "send_two_screens_limit",
            self.action_send_two_screens_limit,
        )
        writer.register(
            self.module_id,
            "send_two_screens_followup",
            self.action_send_two_screens_followup,
        )
        writer.register(
            self.module_id,
            "send_two_screens_retry",
            self.action_send_two_screens_retry,
        )
        writer.register(
            self.module_id,
            "send_two_screens_photo",
            self.action_send_two_screens_photo,
        )
        writer.register(
            self.module_id, "close_live_recipient", self.action_close_live_recipient
        )

    @staticmethod
    def _event_key(event) -> str:
        chat_id = event.chat_id if event.chat_id is not None else event.sender_id
        return f"{chat_id}:{event.id}"

    async def handle_event(self, event) -> bool:
        if not event.is_private or event.out:
            return False
        sender = await event.get_sender()
        if not is_human_sender(sender):
            return False
        sender_id = int(sender.id)
        if self.me is not None and sender_id == int(self.me.id):
            return False

        raw_text = event.raw_text or ""
        response_kind = classify_response(raw_text)
        live_response_kind = classify_live_response(raw_text)
        choice = classify_two_screens_choice(raw_text)

        # Once the preference question is visible, this branch owns the next
        # private reply.  Ambiguous replies deliberately fall back to one of
        # the not-yet-sent slots.  Explicit opt-out still goes through the
        # primary PV state machine below and stops all automation.
        if (
            self.settings.pv_two_screens_enabled
            and response_kind != "opt_out"
            and live_response_kind != "opt_out"
            and await self.photo_flow.is_waiting_choice(sender_id)
        ):
            result = await self.photo_flow.accept_choice(
                event_key=self._event_key(event),
                user_id=sender_id,
                message_id=int(event.id),
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
                requested_slot=choice,
                first_photo_delay_seconds=stable_delay_seconds(
                    f"two-screens-photo:{sender_id}:{event.id}",
                    *TWO_SCREENS_PHOTO_DELAY_RANGE_SECONDS,
                ),
            )
            if result != "ignored":
                return False

        two_screens_response_kind = classify_two_screens_response(raw_text)
        # Compatibility for sessions created before the simplified prompt:
        # a plain "não" must not strand the old awaiting_optin state.
        if self.settings.pv_two_screens_enabled and two_screens_response_kind == "negative":
            two_screens_response_kind = "positive"
        await self.storage.accept_pv_message(
            event_key=self._event_key(event),
            user_id=sender_id,
            message_id=int(event.id),
            username=getattr(sender, "username", None),
            display_name=display_name(sender),
            response_kind=response_kind,
            live_response_kind=live_response_kind,
            two_screens_response_kind=two_screens_response_kind,
            two_screens_choice=choice,
            two_screens_photo_delay_seconds=stable_delay_seconds(
                f"two-screens-photo:{sender_id}:{event.id}",
                *TWO_SCREENS_PHOTO_DELAY_RANGE_SECONDS,
            ),
            delay_seconds=self.settings.pv_reply_delay_seconds,
        )
        # The Radar may still record a probable origin for this same PV. The
        # ribs do not consume one another's ordinary business events.
        return False

    def campaign_text(self, campaign_id: str) -> str:
        return render_campaign(campaign_id, self.settings.pv_preview_link)

    def delay_for_cycle(self, user_id: int, cycle: int) -> int:
        return followup_delay_seconds(
            user_id,
            cycle,
            self.settings.pv_followup_min_hours,
            self.settings.pv_followup_max_hours,
        )

    @property
    def weekly_delay_seconds(self) -> int:
        return round(self.settings.pv_weekly_interval_hours * 3600)

    def preview(self) -> str:
        windows = []
        for cycle in range(1, self.settings.pv_followup_max_cycles + 1):
            extra = 24 * (cycle - 1)
            low = self.settings.pv_followup_min_hours + extra
            high = self.settings.pv_followup_max_hours + extra
            windows.append(f"{cycle}: {low:g}–{high:g} h")
        schedule = ", ".join(windows) if windows else "sem lembretes"
        return "\n\n".join(
            [
                "ATENDIMENTO PV — PRÉVIA",
                f"1. Uma de {len(PV_GREETING_VARIANTS)} aberturas aprovadas",
                f"2. {self.campaign_text('pv.link_invite')}",
                f"3. Após {LINK_BALLOON_DELAY_SECONDS} s: {self.campaign_text('pv.preview_link')}",
                f"Lembrete: {self.campaign_text('pv.followup')} + link separado",
                f"Atraso das duas primeiras mensagens: {self.settings.pv_reply_delay_seconds} s",
                f"Intervalos progressivos até o limite semanal: {schedule}",
                f"Depois: {self.campaign_text('pv.weekly_question')} + sequência semanal",
                f"Periodicidade semanal: {self.settings.pv_weekly_interval_hours:g} h",
                "Resposta positiva encerra sem nova mensagem; resposta negativa recebe o link.",
                (
                    "Após o link: pede ‘duas telas’, pergunta peito/buceta/cuzinho e envia uma foto; "
                    "se não entender, escolhe uma categoria ainda não enviada."
                ),
                "Fotos extras pedidas depois entram uma por vez com 25–35 min entre elas, até três categorias.",
                "‘Parar’ ou ‘não quero’ encerra tudo.",
            ]
        )

    async def action_send_greeting(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self.storage.pv_action_allowed(peer, "greeting_queued"):
            return {"sent": False, "reason": "state_changed"}
        result = await effects.send_text(
            peer,
            greeting_for(action["action_key"]),
            f"{action['action_key']}:send",
        )
        advanced = await self.storage.mark_pv_greeting_sent(peer)
        return {"sent": True, "advanced": advanced, **result}

    async def action_send_link(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self.storage.pv_action_allowed(peer, "link_queued"):
            return {"sent": False, "reason": "state_changed"}
        invite = await effects.send_text(
            peer,
            self.campaign_text("pv.link_invite"),
            f"{action['action_key']}:invite",
        )
        await asyncio.sleep(LINK_BALLOON_DELAY_SECONDS)
        link = await effects.send_text(
            peer,
            self.campaign_text("pv.preview_link"),
            f"{action['action_key']}:link",
        )
        advanced = await self.storage.mark_pv_link_sent_and_schedule(
            user_id=peer,
            delay_seconds=self.delay_for_cycle(peer, 1),
            weekly_delay_seconds=self.weekly_delay_seconds,
            max_cycles=self.settings.pv_followup_max_cycles,
            two_screens_enabled=self.settings.pv_two_screens_enabled,
            two_screens_delay_seconds=TWO_SCREENS_PROMPT_DELAY_SECONDS,
        )
        if advanced:
            await self.storage.queue_live_optin(peer, LIVE_OPTIN_DELAY_SECONDS)
        return {"sent": True, "advanced": advanced, "invite": invite, "link": link}

    async def action_send_followup(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        cycle = int(action["payload"]["cycle"])
        if not await self.storage.pv_action_allowed(
            peer, "following_up", cycle
        ):
            return {"sent": False, "reason": "state_changed", "cycle": cycle}
        reminder = await effects.send_text(
            peer,
            self.campaign_text("pv.followup"),
            f"{action['action_key']}:reminder",
        )
        link = await effects.send_text(
            peer,
            self.campaign_text("pv.preview_link"),
            f"{action['action_key']}:link",
        )
        next_cycle = cycle + 1
        next_delay = (
            self.delay_for_cycle(peer, next_cycle)
            if next_cycle <= self.settings.pv_followup_max_cycles
            else 0
        )
        advanced = await self.storage.complete_pv_followup_and_schedule_next(
            user_id=peer,
            completed_cycle=cycle,
            next_delay_seconds=next_delay,
            weekly_delay_seconds=self.weekly_delay_seconds,
            max_cycles=self.settings.pv_followup_max_cycles,
        )
        return {
            "sent": True,
            "advanced": advanced,
            "cycle": cycle,
            "reminder": reminder,
            "link": link,
        }

    async def action_send_reminder_link(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self.storage.pv_reminder_link_allowed(peer):
            return {"sent": False, "reason": "state_changed"}
        result = await effects.send_text(
            peer,
            self.campaign_text("pv.preview_link"),
            f"{action['action_key']}:send",
        )
        return {"sent": True, **result}

    async def action_send_weekly_question(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        cycle = int(action["payload"]["cycle"])
        if not await self.storage.pv_action_allowed(peer, "weekly", cycle):
            return {"sent": False, "reason": "state_changed", "cycle": cycle}
        question = await effects.send_text(
            peer,
            self.campaign_text("pv.weekly_question"),
            f"{action['action_key']}:question",
        )
        await asyncio.sleep(LINK_BALLOON_DELAY_SECONDS)
        first_link = await effects.send_text(
            peer,
            self.campaign_text("pv.preview_link"),
            f"{action['action_key']}:first-link",
        )
        reentry = await effects.send_text(
            peer,
            self.campaign_text("pv.weekly_reentry"),
            f"{action['action_key']}:reentry",
        )
        second_link = await effects.send_text(
            peer,
            self.campaign_text("pv.preview_link"),
            f"{action['action_key']}:second-link",
        )
        advanced = await self.storage.complete_pv_weekly_and_schedule_next(
            user_id=peer,
            completed_cycle=cycle,
            delay_seconds=self.weekly_delay_seconds,
        )
        return {
            "sent": True,
            "advanced": advanced,
            "cycle": cycle,
            "question": question,
            "first_link": first_link,
            "reentry": reentry,
            "second_link": second_link,
        }

    async def action_send_live_optin(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self.storage.live_optin_allowed(peer):
            return {"sent": False, "reason": "already_asked"}
        result = await effects.send_text(
            peer,
            self.campaign_text("pv.live_optin"),
            f"{action['action_key']}:send",
        )
        await self.storage.mark_live_optin_asked(peer)
        return {"sent": True, **result}

    async def action_send_two_screens_prompt(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self.storage.two_screens_action_allowed(peer, "prompt_queued"):
            return {"sent": False, "reason": "state_changed"}
        request = await effects.send_text(
            peer,
            self.campaign_text("pv.two_screens.prompt"),
            f"{action['action_key']}:request",
        )
        question = await effects.send_text(
            peer,
            DIALOGS["pv.two_screens.preference"],
            f"{action['action_key']}:preference",
        )
        advanced = await self.storage.advance_two_screens(
            peer, "prompt_queued", "awaiting_choice"
        )
        return {
            "sent": True,
            "advanced": advanced,
            "request": request,
            "question": question,
        }

    async def action_send_two_screens_question(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self.storage.two_screens_action_allowed(peer, "question_queued"):
            return {"sent": False, "reason": "state_changed"}
        if self.settings.pv_two_screens_enabled:
            result = await effects.send_text(
                peer,
                DIALOGS["pv.two_screens.preference"],
                f"{action['action_key']}:send",
            )
            advanced = await self.storage.advance_two_screens(
                peer, "question_queued", "awaiting_choice"
            )
            return {"sent": True, "advanced": advanced, **result}

        result = await effects.send_text(
            peer,
            self.campaign_text("pv.two_screens.question"),
            f"{action['action_key']}:send",
        )
        advanced = await self.storage.queue_next_two_screens_action(
            peer,
            expected="question_queued",
            next_status="limit_queued",
            action_type="send_two_screens_limit",
            action_key=f"pv_reply:two-screens:limit:{peer}",
            delay_seconds=stable_delay_seconds(
                f"{action['action_key']}:limit-delay",
                *TWO_SCREENS_BALLOON_DELAY_RANGE_SECONDS,
            ),
        )
        return {"sent": True, "advanced": advanced, **result}

    async def action_send_two_screens_limit(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self.storage.two_screens_action_allowed(peer, "limit_queued"):
            return {"sent": False, "reason": "state_changed"}
        result = await effects.send_text(
            peer,
            self.campaign_text("pv.two_screens.limit"),
            f"{action['action_key']}:send",
        )
        advanced = await self.storage.queue_next_two_screens_action(
            peer,
            expected="limit_queued",
            next_status="followup_queued",
            action_type="send_two_screens_followup",
            action_key=f"pv_reply:two-screens:followup:{peer}",
            delay_seconds=stable_delay_seconds(
                f"{action['action_key']}:followup-delay",
                *TWO_SCREENS_BALLOON_DELAY_RANGE_SECONDS,
            ),
        )
        return {"sent": True, "advanced": advanced, **result}

    async def action_send_two_screens_followup(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self.storage.two_screens_action_allowed(peer, "followup_queued"):
            return {"sent": False, "reason": "state_changed"}
        result = await effects.send_text(
            peer,
            self.campaign_text("pv.two_screens.followup"),
            f"{action['action_key']}:send",
        )
        advanced = await self.storage.advance_two_screens(
            peer, "followup_queued", "awaiting_choice"
        )
        return {"sent": True, "advanced": advanced, **result}

    async def action_send_two_screens_retry(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self.storage.two_screens_action_allowed(peer, "awaiting_choice"):
            return {"sent": False, "reason": "state_changed"}
        text = (
            DIALOGS["pv.two_screens.preference"]
            if self.settings.pv_two_screens_enabled
            else self.campaign_text("pv.two_screens.retry")
        )
        result = await effects.send_text(
            peer,
            text,
            f"{action['action_key']}:send",
        )
        return {"sent": True, **result}

    async def action_send_two_screens_photo(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        slot = str(action["payload"]["slot"])
        final = bool(action["payload"].get("final", False))
        if not await self.storage.two_screens_action_allowed(peer, "photo_queued"):
            return {"sent": False, "reason": "state_changed"}
        media = await self.storage.two_screens_media_slot(slot)
        if not media:
            return {"sent": False, "reason": "media_slot_missing", "slot": slot}
        photo = await effects.forward_message(
            int(media["source_peer"]),
            int(media["source_message_id"]),
            peer,
            f"{action['action_key']}:photo",
        )
        caption = await effects.send_text(
            peer,
            caption_for_slot(slot),
            f"{action['action_key']}:caption",
        )
        advanced = await self.photo_flow.mark_photo_sent(peer, slot, final=final)
        return {
            "sent": True,
            "advanced": advanced,
            "slot": slot,
            "photo": photo,
            "caption": caption,
        }

    async def action_send_live_invite(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        campaign_id = int(action["payload"]["campaign_id"])
        if not await self.storage.live_recipient_allowed(campaign_id, peer, "queued"):
            return {"sent": False, "reason": "state_changed"}
        result = await effects.send_text(
            peer,
            variant_for(action["action_key"], LIVE_INVITE_VARIANTS),
            f"{action['action_key']}:send",
        )
        await self.storage.mark_live_invite_and_schedule_remarketing(
            campaign_id=campaign_id,
            user_id=peer,
            delay_seconds=LIVE_REMARKETING_DELAY_SECONDS,
        )
        return {"sent": True, **result}

    async def action_send_live_remarketing(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        campaign_id = int(action["payload"]["campaign_id"])
        if not await self.storage.live_recipient_allowed(campaign_id, peer, "awaiting"):
            return {"sent": False, "reason": "answered_or_stopped"}
        result = await effects.send_text(
            peer,
            variant_for(action["action_key"], LIVE_REMARKETING_VARIANTS),
            f"{action['action_key']}:send",
        )
        await self.storage.mark_live_remarketing_sent(
            campaign_id, peer, LIVE_REMARKETING_DELAY_SECONDS
        )
        return {"sent": True, **result}

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
        result = await effects.send_text(
            peer,
            link,
            f"{action['action_key']}:send",
        )
        await self.storage.mark_live_link_delivered(campaign_id, peer)
        return {"sent": True, **result}

    async def action_close_live_recipient(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        campaign_id = int(action["payload"]["campaign_id"])
        await self.storage.close_live_recipient(campaign_id, peer)
        return {"closed": True}