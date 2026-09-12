"""Persistent FloodWait telemetry exposed only to the control-panel admin.

This component observes warning logs produced by the existing Telegram runtime.
It never changes module state and never calls the Telegram user session.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from telethon import events

FLOOD_WAIT_RE = re.compile(r"FloodWait(?:\s+por\s+|\s*\()(\d+)s", re.IGNORECASE)
FLOOD_REASON_RE = re.compile(r"FloodWait\s*\((\d+)s\)", re.IGNORECASE)
FLOOD_SCHEMA = """
CREATE TABLE IF NOT EXISTS flood_events (
  id BIGSERIAL PRIMARY KEY,
  module_id TEXT NOT NULL,
  source TEXT NOT NULL,
  wait_seconds INTEGER NOT NULL CHECK(wait_seconds > 0),
  observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS flood_events_latest_idx
ON flood_events(observed_at DESC, id DESC);
"""


def flood_wait_seconds(message: str) -> int | None:
    match = FLOOD_WAIT_RE.search(message or "")
    return int(match.group(1)) if match else None


def flood_reason_seconds(reason: str) -> int | None:
    match = FLOOD_REASON_RE.search(reason or "")
    return int(match.group(1)) if match else None


def flood_origin(logger_name: str, message: str) -> tuple[str, str]:
    logger = (logger_name or "").casefold()
    text = (message or "").casefold()
    if "radar" in logger or text.startswith("radar aguardando floodwait"):
        return "radar", "varredura"
    if "outbox" in logger or text.startswith("outbox aguardando floodwait"):
        return "outbox", "writer"
    return "core", "sessão"


def is_flood_command(text: str) -> bool:
    value = (text or "").strip().casefold()
    if value == "flood":
        return True
    token = value.split(maxsplit=1)[0] if value else ""
    return token.split("@", 1)[0] == "/flood"


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def relative_age(value: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = max(0, int((now - value).total_seconds()))
    if seconds < 60:
        return f"há {seconds}s"
    if seconds < 3600:
        return f"há {seconds // 60}min"
    if seconds < 86400:
        return f"há {seconds // 3600}h"
    return f"há {seconds // 86400}d"


class _FloodLogHandler(logging.Handler):
    def __init__(self, monitor: "FloodMonitor") -> None:
        super().__init__(level=logging.WARNING)
        self.monitor = monitor

    def emit(self, record: logging.LogRecord) -> None:
        # Only real warning/error events are captured. Startup INFO lines may
        # contain a stale persisted FloodWait reason and must not become events.
        if record.levelno < logging.WARNING:
            return
        try:
            message = record.getMessage()
            seconds = flood_wait_seconds(message)
            if seconds is None:
                return
            module_id, source = flood_origin(record.name, message)
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                self.monitor._record(module_id, source, seconds),
                name="floodwait-telemetry",
            )
            self.monitor.tasks.add(task)
            task.add_done_callback(self.monitor.tasks.discard)
        except Exception:
            # Observability must never break the user runtime or recurse through
            # logging while handling a logging failure.
            return


class FloodMonitor:
    def __init__(self, application) -> None:
        self.app = application
        self.handler = _FloodLogHandler(self)
        self.tasks: set[asyncio.Task] = set()
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        await self.app.pool.execute(FLOOD_SCHEMA)
        logging.getLogger().addHandler(self.handler)
        self.app.panel.add_event_handler(self.on_message, events.NewMessage())
        self.started = True

    async def stop(self) -> None:
        if not self.started:
            return
        logging.getLogger().removeHandler(self.handler)
        self.app.panel.remove_event_handler(self.on_message, events.NewMessage())
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        self.started = False

    async def _record(self, module_id: str, source: str, seconds: int) -> None:
        try:
            await self.app.pool.execute(
                """INSERT INTO flood_events(module_id,source,wait_seconds,observed_at)
                   VALUES($1,$2,$3,NOW())""",
                module_id,
                source,
                max(1, int(seconds)),
            )
        except Exception:
            # Never log here: that could recurse through this logging handler.
            return

    async def on_message(self, event) -> None:
        if not self.app.is_admin(event) or not is_flood_command(event.raw_text or ""):
            return
        await event.respond(await self.render(), parse_mode=None, link_preview=False)

    async def render(self) -> str:
        now = datetime.now(timezone.utc)
        current = await self.app.pool.fetch(
            """SELECT module_id,reason,updated_at FROM module_control
               WHERE reason ILIKE '%FloodWait%' ORDER BY updated_at DESC,module_id"""
        )
        history = await self.app.pool.fetch(
            """SELECT module_id,source,wait_seconds,observed_at
               FROM flood_events ORDER BY observed_at DESC,id DESC LIMIT 10"""
        )

        lines = ["🌊 FLOODWAIT", ""]
        if current:
            lines.append("Estado atual:")
            for row in current:
                seconds = flood_reason_seconds(str(row["reason"]))
                if seconds is None:
                    lines.append(f"• {row['module_id']}: {row['reason']}")
                    continue
                updated = row["updated_at"]
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                remaining = max(
                    0,
                    int((updated + timedelta(seconds=seconds) - now).total_seconds()),
                )
                suffix = (
                    f"restante estimado {format_duration(remaining)}"
                    if remaining
                    else "janela estimada já terminou; módulo segue pausado até ser religado"
                )
                lines.append(
                    f"• {row['module_id']}: {format_duration(seconds)} — {suffix}"
                )
        else:
            lines.append("Estado atual: nenhum módulo marcado por FloodWait.")

        lines.extend(("", "Últimos eventos registrados:"))
        if not history:
            lines.append("• Nenhum evento registrado desde a instalação do /flood.")
        else:
            for row in history:
                lines.append(
                    f"• {relative_age(row['observed_at'], now)} — "
                    f"{row['module_id']}/{row['source']}: "
                    f"{format_duration(row['wait_seconds'])}"
                )
        lines.extend(
            (
                "",
                "O tempo vem do próprio Telegram. O comando só consulta; não religa nada.",
            )
        )
        return "\n".join(lines)[:3900]


class FloodAwareObserverMixin:
    """Lifecycle mixin used explicitly by the Railway entry point."""

    async def setup(self) -> None:
        await super().setup()
        self.flood_monitor = FloodMonitor(self)
        await self.flood_monitor.start()

    async def run(self) -> None:
        try:
            await super().run()
        finally:
            monitor = getattr(self, "flood_monitor", None)
            if monitor is not None:
                await monitor.stop()
