"""Composition root and the single Telegram user-session runtime."""

from __future__ import annotations

import asyncio
import logging

import asyncpg
from telethon import TelegramClient, errors, events
from telethon.sessions import MemorySession, StringSession

from .config import Settings
from .modules.botson import BotsonModule
from .modules.pv_reply import PvReplyModule
from .modules.radar import RadarModule
from .outbox import OutboxWriter
from .panel import ControlPanel
from .registry import ModuleRegistry
from .storage import Storage

log = logging.getLogger("gr-observer")


class Observer:
    """Modular monolith kept under the historical public class name."""

    USER_SESSION_LOCK_KEY = 5139241886192644145
    USER_SESSION_HANDOFF_SECONDS = 3

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.api_id = self.settings.api_id
        self.api_hash = self.settings.api_hash
        self.admin_id = self.settings.admin_id
        self.db_url = self.settings.database_url
        self.panel = TelegramClient(
            MemorySession(), self.api_id, self.api_hash, flood_sleep_threshold=0
        )
        self.pool = None
        self.storage = None
        self.registry = ModuleRegistry()
        self.control_panel = None
        self.user = None
        self.me = None
        self.writer = None
        self.worker: asyncio.Task | None = None
        self.writer_task: asyncio.Task | None = None
        self.connection_task: asyncio.Task | None = None
        self.control_lock = asyncio.Lock()
        self.active_events: set[asyncio.Task] = set()
        self.pause_tasks: set[asyncio.Task] = set()
        self.connected_modules: set[str] = set()
        self.state_reason = "Pausado"
        self.enabled = False

    async def setup(self) -> None:
        self.pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=6)
        self.storage = Storage(self.pool)
        await self.storage.initialize()
        self.registry.register(
            "radar",
            RadarModule(self.pool, self.settings, self.pause_module),
        )
        self.registry.register(
            "pv_reply", PvReplyModule(self.storage, self.settings)
        )
        self.registry.register("botson", BotsonModule(self.storage, self.settings))
        self.registry.apply_states(await self.storage.module_states())
        await self.apply_startup_requests()
        for item in list(self.registry.enabled()):
            blocker = self.settings.module_blocker(item.module_id)
            if blocker:
                item.enabled = False
                item.reason = blocker
                await self.storage.set_module_state(item.module_id, False, blocker)
        radar = self.registry.get("radar")
        self.state_reason = radar.reason
        self.enabled = radar.enabled
        await self.panel.start(bot_token=self.settings.control_bot_token)
        self.control_panel = ControlPanel(self)
        self.control_panel.register_handlers()
        if self.registry.enabled():
            self.worker = asyncio.create_task(
                self.user_runtime(), name="telegram-user-runtime"
            )

    async def apply_startup_requests(self) -> None:
        """Honor an explicit first-deploy opt-in without defeating later pauses."""
        item = self.registry.get("pv_reply")
        if (
            not self.settings.pv_reply_auto_enable
            or item.enabled
            or item.reason != item.spec["initial_reason"]
            or self.settings.module_blocker("pv_reply")
        ):
            return
        item.enabled = True
        item.reason = "Conectando por ativação inicial autorizada"
        await self.storage.set_module_state(item.module_id, True, item.reason)

    def is_admin(self, event) -> bool:
        return bool(event.is_private and int(event.sender_id) == self.admin_id)

    async def action_notify_admin(self, action: dict, effects) -> dict:
        return await effects.send_panel_text(
            self.admin_id,
            str(action["payload"]["text"]),
            f"{action['action_key']}:send",
        )

    async def acquire_user_session_lock(self):
        conn = await self.pool.acquire()
        try:
            acquired = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1)", self.USER_SESSION_LOCK_KEY
            )
            if not acquired:
                log.info(
                    "Outro deploy ainda usa a sessão; aguardando encerramento seguro"
                )
                await conn.execute(
                    "SELECT pg_advisory_lock($1)", self.USER_SESSION_LOCK_KEY
                )
                await asyncio.sleep(self.USER_SESSION_HANDOFF_SECONDS)
            return conn
        except BaseException:
            await self.pool.release(conn)
            raise

    async def release_user_session_lock(self, conn) -> None:
        try:
            await conn.execute(
                "SELECT pg_advisory_unlock($1)", self.USER_SESSION_LOCK_KEY
            )
        finally:
            await self.pool.release(conn)

    @staticmethod
    def observer_failure_reason(exc: BaseException) -> str:
        if isinstance(exc, errors.FloodWaitError):
            return f"Pausado por FloodWait ({exc.seconds}s); revisar antes de ligar"
        if isinstance(exc, errors.AuthKeyDuplicatedError):
            return (
                "Sessão invalidada pelo Telegram (AuthKeyDuplicatedError). "
                "Gere uma nova USER_SESSION_STRING antes de ligar."
            )
        return f"Observação parada: {type(exc).__name__}. Confira a sessão e a conexão."

    async def _connect_module(self, module_id: str) -> None:
        if module_id in self.connected_modules or self.user is None:
            return
        item = self.registry.get(module_id)
        await item.implementation.on_connect(self.user, self.me)
        self.connected_modules.add(module_id)
        item.reason = "Ligado"
        await self.storage.set_module_reason(module_id, "Ligado")
        if module_id == "radar":
            self.enabled = True
            self.state_reason = "Ligado"

    async def _disconnect_module(self, module_id: str) -> None:
        if module_id not in self.connected_modules:
            return
        self.connected_modules.discard(module_id)
        await self.registry.get(module_id).implementation.on_disconnect()

    async def set_module_enabled(self, module_id: str, enabled: bool) -> str:
        async with self.control_lock:
            item = self.registry.get(module_id)
            if enabled:
                if item.enabled:
                    if module_id == "radar":
                        return "O Radar já está ligado ou conectando."
                    return f"{item.spec['label']} já está ligado ou conectando."
                blocker = self.settings.module_blocker(module_id)
                if blocker:
                    item.enabled = False
                    item.reason = blocker
                    await self.storage.set_module_state(module_id, False, blocker)
                    if module_id == "radar":
                        self.enabled = False
                        self.state_reason = blocker
                    return blocker
                item.enabled = True
                item.reason = "Conectando"
                await self.storage.set_module_state(module_id, True, "Conectando")
                if module_id == "radar":
                    self.enabled = False
                    self.state_reason = "Conectando"
                if self.user is not None:
                    await self._connect_module(module_id)
                elif not self.worker or self.worker.done():
                    self.worker = asyncio.create_task(
                        self.user_runtime(), name="telegram-user-runtime"
                    )
                if module_id == "radar":
                    return "Observação solicitada. Use /status para conferir."
                return f"{item.spec['label']} solicitado. Use /status para conferir."

            if not item.enabled:
                return f"{item.spec['label']} já está desligado."
            item.enabled = False
            item.reason = "Pausado pelo administrador"
            await self.storage.set_module_state(
                module_id, False, "Pausado pelo administrador"
            )
            if module_id == "radar":
                self.enabled = False
                self.state_reason = item.reason

            # Cancel an active-writing rib at the process boundary. Any
            # interrupted effect remains in review and is not blindly replayed.
            if item.spec["active_writes"] and self.user is not None:
                await self.stop_worker()
                if self.registry.enabled():
                    self.worker = asyncio.create_task(
                        self.user_runtime(), name="telegram-user-runtime"
                    )
            else:
                await self._disconnect_module(module_id)
                if not self.registry.enabled():
                    await self.stop_worker()
            if module_id == "radar":
                return "Observação desligada. O painel continua disponível."
            return f"{item.spec['label']} desligado. O painel continua disponível."

    async def set_enabled(
        self, enabled: bool, reason="Pausado pelo administrador"
    ) -> str:
        """Compatibility for the original /ligar and /desligar API."""
        if not enabled and reason != "Pausado pelo administrador":
            item = self.registry.get("radar")
            item.enabled = False
            item.reason = reason
            await self.storage.set_module_state("radar", False, reason)
            self.enabled = False
            self.state_reason = reason
            await self._disconnect_module("radar")
            if not self.registry.enabled():
                await self.stop_worker()
            return "Observação desligada. O painel continua disponível."
        return await self.set_module_enabled("radar", enabled)

    async def persist_state(self, enabled: bool, reason: str) -> None:
        await self.storage.set_module_state("radar", enabled, reason)
        item = self.registry.get("radar")
        item.enabled = enabled
        item.reason = reason
        self.enabled = enabled
        self.state_reason = reason

    async def pause_module(self, module_id: str, reason: str) -> None:
        item = self.registry.get(module_id)
        item.enabled = False
        item.reason = reason
        await self.storage.set_module_state(module_id, False, reason)
        if module_id == "radar":
            self.enabled = False
            self.state_reason = reason
        await self._disconnect_module(module_id)
        if not self.registry.enabled():
            task = asyncio.create_task(self.stop_worker())
            self.pause_tasks.add(task)
            task.add_done_callback(self.pause_tasks.discard)

    async def guarded_dispatch(self, event) -> None:
        task = asyncio.current_task()
        self.active_events.add(task)
        try:
            # Dispatch order is data. Operational commands get first refusal;
            # ordinary PV events can still reach both Atendimento and Radar.
            items = sorted(
                self.registry.enabled(),
                key=lambda item: item.spec["dispatch_order"],
            )
            for item in items:
                if item.module_id not in self.connected_modules:
                    continue
                try:
                    if await item.implementation.handle_event(event):
                        return
                except errors.FloodWaitError as exc:
                    await self.pause_module(
                        item.module_id,
                        f"Pausado por FloodWait ({exc.seconds}s); revisar antes de ligar",
                    )
        finally:
            self.active_events.discard(task)

    async def stop_worker(self) -> None:
        current = asyncio.current_task()
        tasks = list(self.active_events)
        for candidate in (self.writer_task, self.connection_task, self.worker):
            if candidate and candidate is not current:
                tasks.append(candidate)
        unique = set(tasks)
        for task in unique:
            task.cancel()
        if unique:
            await asyncio.gather(*unique, return_exceptions=True)
        if self.worker is not current:
            self.worker = None

    async def user_runtime(self) -> None:
        session_lock = None
        runtime_tasks = []
        try:
            session_lock = await self.acquire_user_session_lock()
            await self.storage.recover_interrupted()
            self.user = TelegramClient(
                StringSession(self.settings.user_session_string),
                self.api_id,
                self.api_hash,
                flood_sleep_threshold=0,
            )
            self.user.add_event_handler(self.guarded_dispatch, events.NewMessage())
            await self.user.connect()
            if not await self.user.is_user_authorized():
                raise RuntimeError("Sessão não autorizada")
            self.me = await self.user.get_me()
            self.writer = OutboxWriter(
                self.storage,
                self.user,
                panel_client=self.panel,
                module_enabled=lambda module_id: self.registry.get(module_id).enabled,
            )
            self.writer.register("core", "notify_admin", self.action_notify_admin)
            for item in self.registry.ordered():
                register = getattr(item.implementation, "register_actions", None)
                if register:
                    register(self.writer)
            for item in self.registry.enabled():
                await self._connect_module(item.module_id)
            self.writer_task = asyncio.create_task(
                self.writer.run(), name="outbox-writer"
            )
            self.connection_task = asyncio.create_task(
                self.user.run_until_disconnected(), name="telegram-connection"
            )
            runtime_tasks = [self.writer_task, self.connection_task]
            done, _ = await asyncio.wait(
                runtime_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
            raise RuntimeError("Conexão encerrada")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = self.observer_failure_reason(exc)
            log.warning("%s", reason)
            for item in self.registry.enabled():
                item.enabled = False
                item.reason = reason
                await self.storage.set_module_state(item.module_id, False, reason)
            self.enabled = False
            self.state_reason = reason
        finally:
            if self.writer:
                self.writer.stop()
            for task in runtime_tasks + list(self.active_events):
                if task is not asyncio.current_task():
                    task.cancel()
            if runtime_tasks:
                await asyncio.gather(*runtime_tasks, return_exceptions=True)
            for module_id in list(self.connected_modules):
                await self._disconnect_module(module_id)
            user, self.user = self.user, None
            self.me = None
            self.writer = None
            self.writer_task = None
            self.connection_task = None
            try:
                if user:
                    await user.disconnect()
            finally:
                if session_lock:
                    await self.release_user_session_lock(session_lock)

    def status_text(self) -> str:
        lines = []
        for item in self.registry.ordered():
            status = (
                "LIGADO"
                if item.enabled and item.module_id in self.connected_modules
                else "CONECTANDO"
                if item.enabled
                else "DESLIGADO"
            )
            icon = "🟢" if status == "LIGADO" else "🟡" if status == "CONECTANDO" else "🔴"
            lines.extend((f"{icon} {item.spec['label']}: {status}", item.reason))
        session_status = "online" if self.user is not None else "offline"
        lines.extend((f"Sessão única: {session_status}", "Painel: online"))
        return "\n".join(lines)

    async def show_dashboard(self, event) -> None:
        await self.control_panel.show_dashboard(event)

    async def show_list(self, event, section, offset) -> None:
        await self.control_panel.show_list(event, section, offset)

    async def show_chat(self, event, chat_id) -> None:
        await self.control_panel.show_chat(event, chat_id)

    async def run(self) -> None:
        try:
            await self.setup()
            log.info(
                "Painel GR Observer online; %s", self.status_text().replace("\n", " | ")
            )
            await self.panel.run_until_disconnected()
        finally:
            await self.stop_worker()
            if self.pause_tasks:
                await asyncio.gather(*list(self.pause_tasks), return_exceptions=True)
            await self.panel.disconnect()
            if self.pool:
                await self.pool.close()
