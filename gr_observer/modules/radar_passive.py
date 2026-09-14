"""Production Radar policy: event-driven observation with no periodic crawler."""

from __future__ import annotations

import logging

from .radar import RadarModule as LegacyRadarModule

log = logging.getLogger("gr-observer.radar-passive")


class PassiveRadarModule(LegacyRadarModule):
    """Use Radar's event handlers and targeted audits without auto-scanning dialogs."""

    connection_reason = "Ligado — passivo, sem varredura periódica"

    async def on_connect(self, client, me) -> None:
        self.client = client
        self.me = me
        self.scan_task = None
        log.info("Radar conectado em modo passivo; crawler periódico não iniciado")

    async def on_disconnect(self) -> None:
        self.client = None
        self.me = None
        self.scan_task = None
