"""Production composition extension for passive membership observation.

The inherited Observer remains the sole owner of the Telegram USER session, Outbox
and Writer. This layer only attaches one read-only raw-update handler to that same
client and persists membership facts through GroupMembershipTracker.
"""

from __future__ import annotations

import asyncio
import logging

from telethon import events, types

from .application import Observer as BaseObserver
from .group_membership_tracker import GroupMembershipTracker

log = logging.getLogger("gr-observer.membership")


_MEMBERSHIP_UPDATE_TYPES = (
    types.UpdateChannelParticipant,
    types.UpdateChatParticipantAdd,
    types.UpdateChatParticipantDelete,
)


class MembershipObserver(BaseObserver):
    """Canonical Observer plus a passive, non-sending membership listener."""

    def __init__(self, settings=None):
        super().__init__(settings)
        self.membership_tracker: GroupMembershipTracker | None = None
        self._membership_handler_registered = False

    async def _ensure_membership_handler(self) -> None:
        if self.pool is None:
            return
        if self.membership_tracker is None:
            self.membership_tracker = GroupMembershipTracker(
                self.pool,
                preview_link=self.settings.pv_preview_link,
            )
        await self.membership_tracker.ensure_schema()
        if self.user is None or self._membership_handler_registered:
            return
        self.user.add_event_handler(
            self._observe_membership_update,
            events.Raw(types=_MEMBERSHIP_UPDATE_TYPES),
        )
        self._membership_handler_registered = True

    async def _observe_membership_update(self, update) -> None:
        """Observe raw Telegram membership facts; never create Telegram work."""
        task = asyncio.current_task()
        if task is not None:
            self.active_events.add(task)
        try:
            tracker = self.membership_tracker
            if tracker is None:
                return
            outcome = await tracker.observe(update)
            if outcome in {"joined", "left"}:
                log.info("Membership shadow registrou mudança=%s", outcome)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Shadow observation must never pause modules or the USER runtime.
            log.warning(
                "Membership shadow ignorou falha local erro=%s",
                type(exc).__name__,
            )
        finally:
            if task is not None:
                self.active_events.discard(task)

    async def _connect_module(self, module_id: str) -> None:
        # user_runtime creates the single USER client before connecting modules.
        # Attach the observer to that existing client exactly once.
        await self._ensure_membership_handler()
        await super()._connect_module(module_id)

    async def user_runtime(self) -> None:
        self._membership_handler_registered = False
        try:
            await super().user_runtime()
        finally:
            self._membership_handler_registered = False
