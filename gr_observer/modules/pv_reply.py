"""Delayed, stateful replies to inbound private messages from human accounts."""

from __future__ import annotations

import hashlib
import logging

from ..catalog import CAMPAIGNS, PV_RESPONSE_RULES, normalize_text

log = logging.getLogger("gr-observer.pv-reply")


def classify_response(text: str) -> str:
    normalized = normalize_text(text)
    rules = sorted(PV_RESPONSE_RULES.items(), key=lambda item: item[1]["order"])
    for response_kind, rule in rules:
        exact = {normalize_text(value) for value in rule["exact"]}
        contains = tuple(normalize_text(value) for value in rule["contains"])
        if normalized in exact or any(value in normalized for value in contains):
            return response_kind
    return "unknown"


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
        await self.storage.accept_pv_message(
            event_key=self._event_key(event),
            user_id=sender_id,
            message_id=int(event.id),
            username=getattr(sender, "username", None),
            display_name=display_name(sender),
            response_kind=classify_response(event.raw_text or ""),
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
                f"1. {self.campaign_text('pv.greeting')}",
                f"2. {self.campaign_text('pv.preview_link')}",
                f"Lembrete: {self.campaign_text('pv.followup')}",
                f"Atraso das duas primeiras mensagens: {self.settings.pv_reply_delay_seconds} s",
                f"Intervalos progressivos até o limite semanal: {schedule}",
                f"Depois: {self.campaign_text('pv.weekly_question')}",
                f"Periodicidade semanal: {self.settings.pv_weekly_interval_hours:g} h",
                "Resposta positiva encerra sem nova mensagem; resposta negativa recebe o link.",
                "‘Parar’ ou ‘não quero’ encerra tudo.",
            ]
        )

    async def action_send_greeting(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self.storage.pv_action_allowed(peer, "greeting_queued"):
            return {"sent": False, "reason": "state_changed"}
        result = await effects.send_text(
            peer,
            self.campaign_text("pv.greeting"),
            f"{action['action_key']}:send",
        )
        advanced = await self.storage.mark_pv_greeting_sent(peer)
        return {"sent": True, "advanced": advanced, **result}

    async def action_send_link(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        if not await self.storage.pv_action_allowed(peer, "link_queued"):
            return {"sent": False, "reason": "state_changed"}
        result = await effects.send_text(
            peer,
            self.campaign_text("pv.preview_link"),
            f"{action['action_key']}:send",
        )
        advanced = await self.storage.mark_pv_link_sent_and_schedule(
            user_id=peer,
            delay_seconds=self.delay_for_cycle(peer, 1),
            weekly_delay_seconds=self.weekly_delay_seconds,
            max_cycles=self.settings.pv_followup_max_cycles,
        )
        return {"sent": True, "advanced": advanced, **result}

    async def action_send_followup(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["peer"])
        cycle = int(action["payload"]["cycle"])
        if not await self.storage.pv_action_allowed(
            peer, "following_up", cycle
        ):
            return {"sent": False, "reason": "state_changed", "cycle": cycle}
        result = await effects.send_text(
            peer,
            self.campaign_text("pv.followup"),
            f"{action['action_key']}:send",
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
            **result,
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
        result = await effects.send_text(
            peer,
            self.campaign_text("pv.weekly_question"),
            f"{action['action_key']}:send",
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
            **result,
        }
