"""Second production safety layer: pace every explicit user-session RPC.

This module is intentionally additive.  It keeps the already deployed passive
Radar, Signal Ledger and durable Traffic Governor, while tightening two edges:

* all explicit Telethon RPC calls made by the USER client share one small global
  pacer, including reads that do not go through Outbox;
* a user-session FloodWait cooldown no longer prevents control-bot/core Outbox
  actions from being delivered.

Topology is unchanged: one USER session, one Outbox, one Writer.
"""

from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

from .outbox import (
    AmbiguousExternalEffect,
    DefinitiveExternalEffectError,
    action_lane,
)
from .runtime_safety import (
    DeferredTraffic,
    ProductionSafetyMixin,
    SafeTelegramEffects,
    SafeOutboxWriter,
    TrafficGovernor,
)

log = logging.getLogger("gr-observer.runtime-safety-v2")


class RpcPacer:
    """Serialize explicit USER-session RPC starts at a conservative cadence."""

    def __init__(self, minimum_interval_seconds: float = 0.75) -> None:
        self.minimum_interval = max(0.0, float(minimum_interval_seconds))
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait(self) -> None:
        if self.minimum_interval <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            delay = self._next_allowed - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_allowed = loop.time() + self.minimum_interval


class PacedTelegramClient(TelegramClient):
    """Telethon client whose explicit RPC entrypoint is account-wide paced."""

    def __init__(self, *args, rpc_min_interval_seconds: float = 0.75, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.rpc_pacer = RpcPacer(rpc_min_interval_seconds)

    async def __call__(self, request, *args, **kwargs):
        await self.rpc_pacer.wait()
        return await super().__call__(request, *args, **kwargs)


class SafeOutboxWriterV2(SafeOutboxWriter):
    """Single Writer that lets control-bot work pass during USER cooldowns."""

    async def run(self) -> None:
        await self.governor.ensure_schema()
        if await self._wait_or_stop(self.startup_grace_seconds):
            return

        while not self._stopped.is_set():
            action = await self.storage.claim_next_action()
            if not action:
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue

            lane = action_lane(action)
            handler = self.handlers.get((action["module_id"], action["action_type"]))
            if action["module_id"] != "core" and not self.module_enabled(action["module_id"]):
                await self.storage.fail_action(
                    action["id"], RuntimeError(f"módulo desligado: {action['module_id']}")
                )
                continue
            if handler is None:
                await self.storage.fail_action(
                    action["id"],
                    RuntimeError(f"sem handler: {action['module_id']}.{action['action_type']}"),
                )
                continue

            effects = SafeTelegramEffects(
                self.storage,
                self.client,
                action["id"],
                self.panel_client,
                before_user_write=self.user_write_pacer.wait,
                flood_wait_buffer_seconds=self.flood_wait_buffer_seconds,
                governor=self.governor,
            )
            deferred = False
            try:
                result = await handler(action, effects)
            except DeferredTraffic as exc:
                deferred = True
                log.warning(
                    "Ação deferida pelo TrafficGovernor lane=%s key=%s motivo=%s",
                    lane,
                    action["action_key"],
                    exc.reason,
                )
            except asyncio.CancelledError:
                raise
            except DefinitiveExternalEffectError as exc:
                await self.storage.fail_action(action["id"], exc)
            except AmbiguousExternalEffect as exc:
                await self.storage.review_action(action["id"], exc)
            except Exception as exc:
                log.exception(
                    "Ação da outbox falhou lane=%s key=%s",
                    lane,
                    action["action_key"],
                )
                await self.storage.fail_action(action["id"], exc)
            else:
                await self.storage.finish_action(action["id"], result)

            # Only actual USER-session work consumes the conservative action gap.
            # A deferred action made no Telegram call, and core actions use the
            # separate control-bot client.
            if action["module_id"] != "core" and not deferred:
                if await self._wait_or_stop(self.user_action_min_interval_seconds):
                    return


class ProductionSafetyV2Mixin(ProductionSafetyMixin):
    """Production topology with a paced USER client and the same one Writer."""

    async def user_runtime(self) -> None:
        session_lock = None
        runtime_tasks: list[asyncio.Task] = []
        self.session_authorized = False
        try:
            session_lock = await self.acquire_user_session_lock()
            await self.storage.recover_interrupted()
            self.user = PacedTelegramClient(
                StringSession(self.settings.user_session_string),
                self.api_id,
                self.api_hash,
                flood_sleep_threshold=0,
                rpc_min_interval_seconds=0.75,
            )
            from telethon import events

            self.user.add_event_handler(self.guarded_dispatch, events.NewMessage())
            await self.user.connect()
            if not await self.user.is_user_authorized():
                raise RuntimeError("Sessão não autorizada")
            self.me = await self.user.get_me()
            self.session_authorized = True

            self.writer = SafeOutboxWriterV2(
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

            self.writer_task = asyncio.create_task(self.writer.run(), name="outbox-writer")
            self.connection_task = asyncio.create_task(
                self.user.run_until_disconnected(), name="telegram-connection"
            )
            runtime_tasks = [self.writer_task, self.connection_task]
            done, _ = await asyncio.wait(runtime_tasks, return_when=asyncio.FIRST_COMPLETED)
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
            self.session_authorized = False
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
