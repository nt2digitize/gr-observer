"""Single active writer and effect journal for Telegram side effects."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telethon import errors, functions, utils

log = logging.getLogger("gr-observer.outbox")

# Conservative defaults for a user-session runtime. Telegram does not publish
# one fixed safe send rate for user accounts; FLOOD_WAIT is authoritative.
OUTBOX_STARTUP_GRACE_SECONDS = 45.0
USER_ACTION_MIN_INTERVAL_SECONDS = 20.0
USER_WRITE_MIN_INTERVAL_SECONDS = 3.0
FLOOD_WAIT_BUFFER_SECONDS = 5


class AmbiguousExternalEffect(RuntimeError):
    pass


def stable_random_id(effect_key: str) -> int:
    """Return a stable non-zero signed int64 for Telegram message deduplication."""
    value = int.from_bytes(
        hashlib.sha256(effect_key.encode("utf-8")).digest()[:8], "big", signed=True
    )
    return value or 1


def sent_message_id(result) -> int | None:
    direct = getattr(result, "id", None)
    if direct is not None:
        return int(direct)
    for update in getattr(result, "updates", []) or []:
        message = getattr(update, "message", None)
        found = getattr(message, "id", None)
        if found is not None:
            return int(found)
    return None


class UserWritePacer:
    """Serialize Telegram user writes with a minimum gap between effects."""

    def __init__(self, minimum_interval_seconds: float) -> None:
        self.minimum_interval_seconds = max(0.0, float(minimum_interval_seconds))
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait(self) -> None:
        if self.minimum_interval_seconds <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            delay = self._next_allowed - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_allowed = loop.time() + self.minimum_interval_seconds


class TelegramEffects:
    """The only port through which feature modules may mutate Telegram."""

    def __init__(
        self,
        storage,
        client,
        action_id: int,
        panel_client=None,
        before_user_write: Callable[[], Awaitable[None]] | None = None,
        flood_wait_buffer_seconds: int = FLOOD_WAIT_BUFFER_SECONDS,
    ):
        self.storage = storage
        self.client = client
        self.action_id = action_id
        self.panel_client = panel_client
        self.before_user_write = before_user_write
        self.flood_wait_buffer_seconds = max(0, int(flood_wait_buffer_seconds))

    async def perform(
        self,
        effect_key: str,
        effect_type: str,
        payload: dict[str, Any],
        operation: Callable[[], Awaitable[Any]],
        reconcile: Callable[[], Awaitable[Any | None]] | None = None,
    ) -> Any:
        decision, previous = await self.storage.begin_effect(
            effect_key=effect_key,
            action_id=self.action_id,
            effect_type=effect_type,
            payload=payload,
        )
        if decision == "succeeded":
            return previous
        if decision == "ambiguous":
            reconciled = await reconcile() if reconcile else None
            if reconciled is not None:
                await self.storage.finish_effect(effect_key, reconciled)
                return reconciled
            raise AmbiguousExternalEffect(
                f"efeito {effect_key} ficou ambíguo; revisão necessária antes de repetir"
            )

        while True:
            try:
                result = await operation()
                break
            except errors.FloodWaitError as exc:
                # FLOOD_WAIT means Telegram rejected this invocation and gave
                # the exact retry delay. Keep the same effect/action alive,
                # block the single writer, and retry only after that window.
                wait_seconds = max(1, int(exc.seconds)) + self.flood_wait_buffer_seconds
                log.warning(
                    "Outbox aguardando FloodWait por %ss antes de repetir efeito=%s",
                    wait_seconds,
                    effect_key,
                )
                await asyncio.sleep(wait_seconds)
            except BaseException as exc:
                # A timeout can occur after Telegram accepted the operation. Never
                # assume failure and repeat an active write blindly.
                await self.storage.review_effect(effect_key, exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise AmbiguousExternalEffect(
                    f"resultado externo incerto em {effect_key}: {type(exc).__name__}"
                ) from exc

        await self.storage.finish_effect(effect_key, result)
        return result

    async def send_text(self, peer, text: str, effect_key: str) -> dict[str, Any]:
        return await self._send_text(
            self.client,
            peer,
            text,
            effect_key,
            "send_text",
            pace_user=True,
        )

    async def send_panel_text(
        self, peer, text: str, effect_key: str
    ) -> dict[str, Any]:
        if self.panel_client is None:
            raise RuntimeError("cliente do painel indisponível")
        return await self._send_text(
            self.panel_client,
            peer,
            text,
            effect_key,
            "send_panel_text",
            pace_user=False,
        )

    async def _send_text(
        self,
        client,
        peer,
        text: str,
        effect_key: str,
        effect_type: str,
        *,
        pace_user: bool,
    ) -> dict[str, Any]:
        random_id = stable_random_id(effect_key)

        async def send():
            input_peer = await client.get_input_entity(peer)
            if pace_user and self.before_user_write is not None:
                await self.before_user_write()
            result = await client(
                functions.messages.SendMessageRequest(
                    peer=input_peer,
                    message=text,
                    random_id=random_id,
                    no_webpage=True,
                )
            )
            return {"message_id": sent_message_id(result), "random_id": random_id}

        return await self.perform(
            effect_key,
            effect_type,
            {"peer": str(peer), "text": text, "random_id": random_id},
            send,
        )

    async def delete_messages(
        self, peer, message_ids: list[int], effect_key: str
    ) -> dict[str, Any]:
        ids = [int(message_id) for message_id in message_ids]

        async def delete():
            if self.before_user_write is not None:
                await self.before_user_write()
            await self.client.delete_messages(peer, ids, revoke=True)
            return {"deleted": True, "message_ids": ids}

        return await self.perform(
            effect_key,
            "delete_messages",
            {"peer": str(peer), "message_ids": ids},
            delete,
        )

    async def forward_message(
        self, source_peer, message_id: int, destination_peer, effect_key: str
    ) -> dict[str, Any]:
        """Forward one operator-catalogued media message through the user session."""

        async def forward():
            if self.before_user_write is not None:
                await self.before_user_write()
            result = await self.client.forward_messages(
                destination_peer,
                int(message_id),
                from_peer=source_peer,
                drop_author=True,
                drop_media_captions=True,
            )
            forwarded = result[0] if isinstance(result, (list, tuple)) and result else result
            return {
                "message_id": sent_message_id(forwarded),
                "source_message_id": int(message_id),
            }

        return await self.perform(
            effect_key,
            "forward_message",
            {
                "source_peer": str(source_peer),
                "message_id": int(message_id),
                "destination_peer": str(destination_peer),
            },
            forward,
        )

    async def send_catalogued_media(
        self,
        source_peer,
        message_id: int,
        destination_peer,
        effect_key: str,
        *,
        spoiler: bool = True,
        ttl_seconds: int | None = 30,
    ) -> dict[str, Any]:
        """Copy catalogued Telegram media with spoiler and optional self-destruct TTL."""
        random_id = stable_random_id(effect_key)
        requested_ttl = (
            None
            if ttl_seconds is None
            else max(1, min(60, int(ttl_seconds)))
        )

        async def send():
            source_message = await self.client.get_messages(
                source_peer, ids=int(message_id)
            )
            source_media = getattr(source_message, "media", None)
            if source_message is None or source_media is None:
                raise ValueError("mídia cadastrada não encontrada no Telegram")

            input_peer = await self.client.get_input_entity(destination_peer)

            def build_media(ttl: int | None):
                media = utils.get_input_media(source_media, ttl=ttl)
                if not hasattr(media, "spoiler"):
                    raise TypeError("mídia cadastrada não suporta spoiler")
                media.spoiler = bool(spoiler)
                return media

            async def send_request(media):
                if self.before_user_write is not None:
                    await self.before_user_write()
                return await self.client(
                    functions.messages.SendMediaRequest(
                        peer=input_peer,
                        media=media,
                        message="",
                        random_id=random_id,
                    )
                )

            used_ttl = requested_ttl
            try:
                result = await send_request(build_media(requested_ttl))
            except errors.BadRequestError as exc:
                # A rejected TTL request is known not to have produced a send,
                # so it is safe to retry once without TTL while preserving spoiler.
                if requested_ttl is None or "TTL_MEDIA_INVALID" not in str(exc).upper():
                    raise
                used_ttl = None
                result = await send_request(build_media(None))

            return {
                "message_id": sent_message_id(result),
                "source_message_id": int(message_id),
                "random_id": random_id,
                "spoiler": bool(spoiler),
                "ttl_seconds": used_ttl,
            }

        return await self.perform(
            effect_key,
            "send_catalogued_media",
            {
                "source_peer": str(source_peer),
                "message_id": int(message_id),
                "destination_peer": str(destination_peer),
                "spoiler": bool(spoiler),
                "ttl_seconds": requested_ttl,
                "random_id": random_id,
            },
            send,
        )


class OutboxWriter:
    """Serial dispatcher. Exactly one instance accompanies the user session."""

    def __init__(
        self,
        storage,
        client,
        module_enabled=None,
        panel_client=None,
        *,
        startup_grace_seconds: float = OUTBOX_STARTUP_GRACE_SECONDS,
        user_action_min_interval_seconds: float = USER_ACTION_MIN_INTERVAL_SECONDS,
        user_write_min_interval_seconds: float = USER_WRITE_MIN_INTERVAL_SECONDS,
        flood_wait_buffer_seconds: int = FLOOD_WAIT_BUFFER_SECONDS,
    ):
        self.storage = storage
        self.client = client
        self.module_enabled = module_enabled or (lambda _module_id: True)
        self.panel_client = panel_client
        self.handlers: dict[tuple[str, str], Callable] = {}
        self._stopped = asyncio.Event()
        self.startup_grace_seconds = max(0.0, float(startup_grace_seconds))
        self.user_action_min_interval_seconds = max(
            0.0, float(user_action_min_interval_seconds)
        )
        self.flood_wait_buffer_seconds = max(0, int(flood_wait_buffer_seconds))
        self.user_write_pacer = UserWritePacer(user_write_min_interval_seconds)

    def register(self, module_id: str, action_type: str, handler: Callable) -> None:
        key = (module_id, action_type)
        if key in self.handlers:
            raise ValueError(f"handler duplicado: {key}")
        self.handlers[key] = handler

    def stop(self) -> None:
        self._stopped.set()

    async def _wait_or_stop(self, seconds: float) -> bool:
        if seconds <= 0:
            return self._stopped.is_set()
        try:
            await asyncio.wait_for(self._stopped.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return False
        return True

    async def run(self) -> None:
        # A restart/reconnect must never dump all overdue PV work at once.
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
            handler = self.handlers.get((action["module_id"], action["action_type"]))
            if action["module_id"] != "core" and not self.module_enabled(action["module_id"]):
                await self.storage.fail_action(
                    action["id"],
                    RuntimeError(f"módulo desligado: {action['module_id']}"),
                )
                continue
            if handler is None:
                await self.storage.fail_action(
                    action["id"],
                    RuntimeError(
                        f"sem handler: {action['module_id']}.{action['action_type']}"
                    ),
                )
                continue
            effects = TelegramEffects(
                self.storage,
                self.client,
                action["id"],
                self.panel_client,
                before_user_write=self.user_write_pacer.wait,
                flood_wait_buffer_seconds=self.flood_wait_buffer_seconds,
            )
            try:
                result = await handler(action, effects)
            except asyncio.CancelledError:
                raise
            except AmbiguousExternalEffect as exc:
                log.warning("Ação ambígua requer revisão key=%s", action["action_key"])
                await self.storage.review_action(action["id"], exc)
            except Exception as exc:
                log.exception("Ação da outbox falhou key=%s", action["action_key"])
                await self.storage.fail_action(action["id"], exc)
            else:
                await self.storage.finish_action(action["id"], result)

            # Core actions use the control-bot client. All feature actions use
            # the Telegram user session and therefore share one conservative
            # account-wide cadence, including backlog catch-up after restart.
            if action["module_id"] != "core":
                if await self._wait_or_stop(self.user_action_min_interval_seconds):
                    return
