"""Emergency, reversible production containment for PV automation.

When PV_REPLY_EMERGENCY_PAUSE is enabled, pv_reply is forced off during
startup before the Telegram user runtime is created. No queued action or state
row is deleted; clearing the flag restores ordinary operator control.
"""

from __future__ import annotations

import os


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
        "sim",
    }


class EmergencyPvPauseMixin:
    async def apply_startup_requests(self) -> None:
        await super().apply_startup_requests()
        if not _enabled("PV_REPLY_EMERGENCY_PAUSE"):
            return
        item = self.registry.get("pv_reply")
        item.enabled = False
        item.reason = "Pausado por contenção de segurança do PV"
        await self.storage.set_module_state(
            "pv_reply", False, "Pausado por contenção de segurança do PV"
        )
