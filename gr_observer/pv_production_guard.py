"""Fail-safe guards for PV routing and delayed sequence continuations.

This layer is intentionally additive: it does not create another Telegram client,
Outbox or Writer. It serializes same-user inbound state transitions, attaches a
state token to delayed message-step continuations, and repairs only missing
required destination steps without overwriting operator-edited copy.
"""

from __future__ import annotations

import asyncio
import logging
import re
from types import MethodType

from . import pv_message_runtime
from .pv_message_steps import has_link as base_has_link

log = logging.getLogger("gr-observer.pv-production-guard")

_LINKISH_RE = re.compile(
    r"(?i)(?:https?://|www\.|(?:t|telegram)\.me/|telegram\.dog/)\S+"
)


def pv_has_link(value: str) -> bool:
    """Recognize ordinary and Telegram links so the send gets a webpage card."""
    text = value or ""
    return base_has_link(text) or bool(_LINKISH_RE.search(text))


def required_destination_steps(settings) -> tuple[tuple, ...]:
    """Built-in destination steps that must exist for an intact PV journey.

    Existing rows are never changed. These definitions are used only to restore
    a row that was physically deleted by an older editor behavior.
    """
    reply_delay = max(0, int(settings.pv_reply_delay_seconds))
    return (
        (
            "link.preview",
            "link",
            None,
            2_000_000,
            "Link da prévia",
            "{preview_link}",
            "link",
            7,
        ),
        (
            "followup.link",
            "followup",
            None,
            2_000_000,
            "Link do follow-up",
            "{preview_link}",
            "link",
            3,
        ),
        (
            "reminder.link",
            "reminder_link",
            None,
            1_000_000,
            "Reenvio do link",
            "{preview_link}",
            "link",
            reply_delay,
        ),
        (
            "weekly.link1",
            "weekly",
            None,
            2_000_000,
            "Link semanal 1",
            "{preview_link}",
            "link",
            7,
        ),
        (
            "weekly.link2",
            "weekly",
            None,
            4_000_000,
            "Link semanal 2",
            "{preview_link}",
            "link",
            3,
        ),
        (
            "live.link",
            "live_link",
            None,
            1_000_000,
            "Link da live",
            "{live_link}",
            "link",
            0,
        ),
    )


async def repair_required_destination_steps(pool, settings) -> tuple[str, ...]:
    """Restore only missing required link rows; preserve every existing edit."""
    restored: list[str] = []
    for (
        step_key,
        block_key,
        branch_key,
        position,
        label,
        content,
        kind,
        median,
    ) in required_destination_steps(settings):
        inserted = await pool.fetchval(
            """INSERT INTO pv_message_steps(
                   step_key,block_key,branch_key,position,label,content,kind,
                   median_delay_seconds,variant_root,built_in,updated_at)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,FALSE,TRUE,NOW())
               ON CONFLICT(step_key) DO NOTHING
               RETURNING step_key""",
            step_key,
            block_key,
            branch_key,
            position,
            label,
            content,
            kind,
            median,
        )
        if inserted:
            restored.append(str(inserted))
    return tuple(restored)


class PvProductionGuardMixin:
    """Install fail-safe PV guards before the single USER runtime starts."""

    _PV_LOCK_STRIPES = 256

    async def apply_startup_requests(self) -> None:
        """Repair historic editor damage before USER-session work can start."""
        await super().apply_startup_requests()
        pv = self.registry.get("pv_reply").implementation
        await pv.message_store.ensure_ready()
        restored = await repair_required_destination_steps(self.pool, self.settings)
        if restored:
            log.warning(
                "PV safety restored missing destination steps: %s",
                ",".join(restored),
            )

    def _install_pv_production_guards(self) -> None:
        if getattr(self, "_pv_production_guards_installed", False):
            return
        self._pv_production_guards_installed = True
        self._pv_accept_locks = [asyncio.Lock() for _ in range(self._PV_LOCK_STRIPES)]

        pv_message_runtime.has_link = pv_has_link
        original_accept = self.storage.accept_pv_message

        async def serialized_accept_pv_message(*args, **kwargs):
            user_id = int(kwargs.get("user_id") or 0)
            lock = self._pv_accept_locks[user_id % self._PV_LOCK_STRIPES]
            async with lock:
                return await original_accept(*args, **kwargs)

        self.storage.accept_pv_message = serialized_accept_pv_message

        pv = self.registry.get("pv_reply").implementation
        original_queue = pv._queue_sequence_continuation
        original_step = pv.action_send_message_step

        async def guarded_queue(this, **kwargs):
            peer = int(kwargs["peer"])
            row = await this.storage.pool.fetchrow(
                """SELECT stage,last_inbound_message_id
                   FROM pv_reply_contacts WHERE user_id=$1""",
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
            return await original_queue(**kwargs)

        async def guarded_step(this, action: dict, effects):
            payload = action.get("payload") or {}
            context = dict(payload.get("context") or {})
            guard = context.get("_pv_guard")
            if not isinstance(guard, dict):
                return {"sent": False, "reason": "legacy_step_quarantined"}

            peer = int(payload.get("peer") or 0)
            row = await this.storage.pool.fetchrow(
                """SELECT stage,last_inbound_message_id
                   FROM pv_reply_contacts WHERE user_id=$1""",
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
            if (
                current_stage != guard.get("stage")
                or current_message_id != expected_message_id
            ):
                return {"sent": False, "reason": "conversation_state_changed"}
            return await original_step(action, effects)

        pv._queue_sequence_continuation = MethodType(guarded_queue, pv)
        pv.action_send_message_step = MethodType(guarded_step, pv)

    async def user_runtime(self) -> None:
        self._install_pv_production_guards()
        await super().user_runtime()
