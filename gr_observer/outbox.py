"""Single active writer and effect journal for Telegram side effects."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telethon import functions

log = logging.getLogger("gr-observer.outbox")


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


class TelegramEffects:
    """The only port through which feature modules may mutate Telegram."""

    def __init__(self, storage, client, action_id: int):
        self.storage = storage
        self.client = client
        self.action_id = action_id

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
        try:
            result = await operation()
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
        random_id = stable_random_id(effect_key)

        async def send():
            input_peer = await self.client.get_input_entity(peer)
            result = await self.client(
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
            "send_text",
            {"peer": str(peer), "text": text, "random_id": random_id},
            send,
        )


class OutboxWriter:
    """Serial dispatcher. Exactly one instance accompanies the user session."""

    def __init__(self, storage, client, module_enabled=None):
        self.storage = storage
        self.client = client
        self.module_enabled = module_enabled or (lambda _module_id: True)
        self.handlers: dict[tuple[str, str], Callable] = {}
        self._stopped = asyncio.Event()

    def register(self, module_id: str, action_type: str, handler: Callable) -> None:
        key = (module_id, action_type)
        if key in self.handlers:
            raise ValueError(f"handler duplicado: {key}")
        self.handlers[key] = handler

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        while not self._stopped.is_set():
            action = await self.storage.claim_next_action()
            if not action:
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            handler = self.handlers.get((action["module_id"], action["action_type"]))
            if not self.module_enabled(action["module_id"]):
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
            effects = TelegramEffects(self.storage, self.client, action["id"])
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
