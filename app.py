"""Compatibility entry point for Railway and existing local commands."""

import asyncio
import logging
import os

from dotenv import load_dotenv

from gr_observer.application import Observer
from gr_observer.config import missing_panel_env
from gr_observer.durable_peers import DurablePeerObserverMixin
from gr_observer.flood_monitor import FloodAwareObserverMixin
from gr_observer.pv_production_guard import PvProductionGuardMixin

load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("gr-observer")


class RuntimeObserver(
    FloodAwareObserverMixin,
    PvProductionGuardMixin,
    DurablePeerObserverMixin,
    Observer,
):
    """Production composition root with PV routing guards and durable peers."""


if __name__ == "__main__":
    missing = missing_panel_env()
    if missing:
        log.error("Configuração pendente: %s. Painel não iniciado.", ", ".join(missing))
        raise SystemExit(0)
    asyncio.run(RuntimeObserver().run())
