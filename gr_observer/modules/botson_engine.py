"""BOTSON homologation engine using the monolith's shared Telegram session.

Reads use the shared client. Every active operation is routed through
``TelegramEffects`` and receives a deterministic effect key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone

from telethon import errors, events, utils
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

log = logging.getLogger("gr-observer.botson.engine")

PAIR_MARKER = "BOTSON_SENTINEL_CONTROLLER_ID="
SAFE_HINTS = (
    "ver conteúdo",
    "ver conteudo",
    "assistir",
    "vip",
    "continuar",
    "começar",
    "comecar",
    "abrir",
    "menu",
    "voltar",
    "próximo",
    "proximo",
    "entrar",
    "acessar",
    "preview",
    "prévia",
    "previa",
)
BLOCK_HINTS = (
    "pagar",
    "pagamento",
    "comprar",
    "checkout",
    "pix",
    "cartão",
    "cartao",
    "assinar",
    "subscribe",
    "renovar",
    "cancelar",
    "ban",
    "kick",
    "remover",
    "apagar",
    "delete",
    "confirmar compra",
)
RECOVERY_TEXT = "você saiu da prévia"
RECOVERY_BUTTON_HINT = "voltar ao"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(event: str, **data) -> None:
    log.info(
        json.dumps(
            {"event": event, "observed_at": utcnow(), **data},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    )


def buttons(message) -> list[dict]:
    result = []
    markup = getattr(message, "reply_markup", None)
    if not markup:
        return result
    for row_index, row in enumerate(getattr(markup, "rows", []) or []):
        for col_index, button in enumerate(getattr(row, "buttons", []) or []):
            raw = getattr(button, "data", None)
            result.append(
                {
                    "row": row_index,
                    "col": col_index,
                    "text": (getattr(button, "text", None) or "").strip(),
                    "url": getattr(button, "url", None),
                    "data": raw.decode("utf-8", errors="replace")
                    if isinstance(raw, (bytes, bytearray))
                    else None,
                }
            )
    return result


def marked_id(entity) -> int | None:
    try:
        return int(utils.get_peer_id(entity))
    except Exception:
        return None


def name_of(entity) -> str:
    title = getattr(entity, "title", None)
    if title:
        return title
    full = " ".join(
        item
        for item in (
            getattr(entity, "first_name", None),
            getattr(entity, "last_name", None),
        )
        if item
    ).strip()
    return full or getattr(entity, "username", None) or "Sem nome"


def username_of(entity) -> str | None:
    return getattr(entity, "username", None) or None


def link_of(entity, message_id=None) -> str | None:
    username = username_of(entity)
    if username:
        return f"https://t.me/{username}" + (f"/{message_id}" if message_id else "")
    chat_id = marked_id(entity)
    if chat_id is not None and str(chat_id).startswith("-100") and message_id:
        return f"https://t.me/c/{str(abs(chat_id))[3:]}/{message_id}"
    return None


def entity_label(entity, message_id=None) -> str:
    parts = [name_of(entity)]
    if username_of(entity):
        parts.append(f"@{username_of(entity)}")
    if marked_id(entity) is not None:
        parts.append(f"ID {marked_id(entity)}")
    if link_of(entity, message_id):
        parts.append(link_of(entity, message_id))
    return " | ".join(parts)


def body_of(message) -> str:
    text = (message.raw_text or "").strip()
    if text:
        return text
    return "[mídia]" if getattr(message, "media", None) else "[mensagem sem texto]"


async def sender_label(message) -> str:
    try:
        sender = await message.get_sender()
    except Exception:
        sender = None
    if not sender:
        return getattr(message, "post_author", None) or "Origem não identificada"
    parts = [name_of(sender)]
    if username_of(sender):
        parts.append(f"@{username_of(sender)}")
    if getattr(sender, "id", None) is not None:
        parts.append(f"ID {sender.id}")
    return " | ".join(parts)


def invite_hash(value: str) -> str | None:
    for marker in (
        "t.me/+",
        "telegram.me/+",
        "t.me/joinchat/",
        "telegram.me/joinchat/",
    ):
        if marker in value:
            return value.split(marker, 1)[1].split("?", 1)[0].strip("/")
    return None


def telegram_bot_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.match(
        r"https?://(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)(?:\?.*)?$", url.strip()
    )
    if not match:
        return None
    username = match.group(1)
    return username if username.lower().endswith("bot") else None


def button_policy(button: dict) -> tuple[bool, str]:
    text = (button.get("text") or "").casefold()
    raw_data = button.get("data") or ""
    if isinstance(raw_data, (bytes, bytearray)):
        raw_data = raw_data.decode("utf-8", errors="replace")
    data = str(raw_data).casefold()
    url = button.get("url")
    if any(word in text or word in data for word in BLOCK_HINTS):
        return False, "bloqueado por segurança"
    bot_username = telegram_bot_from_url(url)
    if bot_username:
        return True, f"abrir bot @{bot_username}"
    if url:
        return False, "URL externa não aberta pelo Telethon"
    if button.get("data") and any(word in text for word in SAFE_HINTS):
        return True, "callback de navegação permitido"
    return False, "não classificado como navegação segura"


def _target_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class BotsonEngine:
    def __init__(self, client, effects, settings, run_id: str):
        self.client = client
        self.effects = effects
        self.settings = settings
        self.run_id = run_id

    async def capture(self, entity, limit=12, min_id=None) -> list[dict]:
        kwargs = {"limit": limit}
        if min_id is not None:
            kwargs["min_id"] = min_id
        items = await self.client.get_messages(entity, **kwargs)
        captured = []
        for message in reversed(items):
            item = {
                "message_id": message.id,
                "sender": await sender_label(message),
                "text": body_of(message),
                "link": link_of(entity, message.id),
                "buttons": buttons(message),
                "outgoing": bool(message.out),
            }
            captured.append(item)
            emit("captured_message", chat_id=marked_id(entity), **item)
        return captured

    async def resolve_preview(self, target: str):
        parsed = invite_hash(target)
        if parsed:
            return "invite", parsed
        if target.lstrip("-").isdigit():
            wanted = int(target)
            async for dialog in self.client.iter_dialogs():
                if int(dialog.id) == wanted:
                    return "entity", dialog.entity
            raise ValueError("preview_id_not_visible_to_sentinel")
        return "entity", await self.client.get_entity(target.lstrip("@"))

    async def is_member(self, entity) -> bool:
        if entity is None:
            return False
        try:
            await self.client.get_permissions(entity, "me")
            return True
        except errors.UserNotParticipantError:
            return False
        except Exception:
            try:
                await self.client.get_messages(entity, limit=1)
                return True
            except Exception:
                return False

    async def _resolve_after_join(self, target: str, fallback=None):
        if fallback is not None:
            return fallback
        try:
            kind, value = await self.resolve_preview(target)
            return value if kind == "entity" else None
        except Exception:
            return None

    async def join_preview(self, target: str, cached_entity=None, stage="join"):
        entity_box = {"entity": cached_entity}
        effect_key = f"{self.run_id}:{stage}:join:{_target_token(target)}"

        async def operation():
            parsed = invite_hash(target)
            try:
                if parsed:
                    result = await self.client(ImportChatInviteRequest(parsed))
                    entity_box["entity"] = (getattr(result, "chats", []) or [None])[0]
                else:
                    entity = cached_entity
                    if entity is None:
                        _, entity = await self.resolve_preview(target)
                    entity_box["entity"] = entity
                    try:
                        await self.client(JoinChannelRequest(entity))
                    except errors.UserAlreadyParticipantError:
                        pass
                entity = entity_box["entity"]
                return {"status": "ok", "chat_id": marked_id(entity)}
            except errors.InviteRequestSentError:
                return {
                    "status": "join_request_sent",
                    "chat_id": marked_id(cached_entity),
                }

        async def reconcile():
            entity = await self._resolve_after_join(target, entity_box["entity"])
            entity_box["entity"] = entity
            if entity and await self.is_member(entity):
                return {
                    "status": "ok",
                    "chat_id": marked_id(entity),
                    "reconciled": True,
                }
            return None

        try:
            result = await self.effects.perform(
                effect_key,
                "join_preview",
                {"target_ref": _target_token(target), "stage": stage},
                operation,
                reconcile,
            )
            entity = await self._resolve_after_join(target, entity_box["entity"])
            status = result.get("status", "failed:unknown")
            emit(
                "preview_join",
                target_ref=_target_token(target),
                status=status,
                chat_id=marked_id(entity),
            )
            return entity, status
        except Exception as exc:
            emit(
                "preview_join",
                target_ref=_target_token(target),
                status="failed",
                error=type(exc).__name__,
            )
            return cached_entity, f"failed:{type(exc).__name__}"

    async def leave_preview(self, entity, stage="leave") -> str:
        chat_id = marked_id(entity)
        effect_key = f"{self.run_id}:{stage}:leave:{chat_id}"

        async def operation():
            await self.client(LeaveChannelRequest(entity))
            return {"status": "ok", "chat_id": chat_id}

        async def reconcile():
            if not await self.is_member(entity):
                return {"status": "ok", "chat_id": chat_id, "reconciled": True}
            return None

        try:
            result = await self.effects.perform(
                effect_key,
                "leave_preview",
                {"chat_id": chat_id, "stage": stage},
                operation,
                reconcile,
            )
            emit("preview_leave", chat_id=chat_id, status=result.get("status", "ok"))
            return result.get("status", "ok")
        except Exception as exc:
            emit(
                "preview_leave",
                chat_id=chat_id,
                status="failed",
                error=type(exc).__name__,
            )
            return f"failed:{type(exc).__name__}"

    async def wait_for_membership(self, entity, seconds: float) -> bool:
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            if await self.is_member(entity):
                return True
            await asyncio.sleep(2)
        return await self.is_member(entity)

    async def walk_bot(self, entity, report: dict, visited_bots: set, depth=0) -> None:
        if (
            depth > self.settings.botson_max_depth
            or report["clicked_count"] >= self.settings.botson_max_clicks
        ):
            return
        key = (marked_id(entity), depth)
        if key in visited_bots:
            return
        visited_bots.add(key)
        place = entity_label(entity)
        before = await self.client.get_messages(entity, limit=1)
        before_id = before[0].id if before else 0
        send_key = f"{self.run_id}:bot-start:{marked_id(entity)}:{depth}"
        sent = await self.effects.send_text(entity, "/start", send_key)
        report["trail"].append(
            f"BOT {place}: enviei /start (#{sent.get('message_id') or '?'})."
        )
        await asyncio.sleep(3)
        new_messages = await self.capture(entity, limit=20, min_id=before_id)
        report["conversations"].append({"place": place, "messages": new_messages})
        if not any(not message["outgoing"] for message in new_messages):
            report["failures"].append(f"{name_of(entity)} não respondeu ao /start.")
            return

        processed = set()
        while report["clicked_count"] < self.settings.botson_max_clicks:
            latest = await self.client.get_messages(entity, limit=6)
            candidate = None
            for message in latest:
                if message.out:
                    continue
                for button in buttons(message):
                    action_key = (message.id, button["row"], button["col"])
                    if action_key in processed:
                        continue
                    processed.add(action_key)
                    allowed, reason = button_policy(button)
                    label = button.get("text") or "sem texto"
                    if not allowed:
                        report["trail"].append(
                            f"BOT {name_of(entity)}: botão “{label}” VISTO e NÃO CLICADO — {reason}."
                        )
                        continue
                    candidate = (message, button, reason)
                    break
                if candidate:
                    break
            if not candidate:
                break

            message, button, reason = candidate
            label = button.get("text") or "sem texto"
            bot_username = telegram_bot_from_url(button.get("url"))
            if bot_username:
                try:
                    next_entity = await self.client.get_entity(bot_username)
                    report["clicked_count"] += 1
                    report["trail"].append(
                        f"BOT {name_of(entity)}: segui “{label}” → @{bot_username}."
                    )
                    await self.walk_bot(next_entity, report, visited_bots, depth + 1)
                except Exception as exc:
                    report["failures"].append(
                        f"Não consegui abrir @{bot_username}: {type(exc).__name__}."
                    )
                continue

            old_id = message.id
            effect_key = (
                f"{self.run_id}:click:{marked_id(entity)}:{message.id}:"
                f"{button['row']}:{button['col']}"
            )

            async def click(message=message, button=button):
                await message.click(button["row"], button["col"])
                return {"clicked": True, "message_id": message.id}

            try:
                await self.effects.perform(
                    effect_key,
                    "safe_callback",
                    {
                        "chat_id": marked_id(entity),
                        "message_id": message.id,
                        "row": button["row"],
                        "col": button["col"],
                        "label": label,
                    },
                    click,
                )
                report["clicked_count"] += 1
                report["trail"].append(
                    f"BOT {name_of(entity)}: CLIQUEI “{label}” — {reason}."
                )
                await asyncio.sleep(2.5)
                responses = await self.capture(entity, limit=12, min_id=old_id)
                if responses:
                    report["conversations"].append(
                        {"place": place, "messages": responses}
                    )
                    report["trail"].append(
                        f"BOT {name_of(entity)}: após “{label}”, capturei {len(responses)} atualização(ões)."
                    )
            except errors.FloodWaitError as exc:
                report["failures"].append(
                    f"Telegram pediu espera de {exc.seconds}s; teste interrompido para respeitar limite."
                )
                return
            except Exception as exc:
                report["failures"].append(
                    f"Falha ao clicar “{label}” em {name_of(entity)}: {type(exc).__name__}."
                )

    @staticmethod
    def recovery_url_from_message(message) -> str | None:
        text = (message.raw_text or "").casefold()
        for button in buttons(message):
            label = (button.get("text") or "").casefold()
            url = button.get("url")
            if url and (RECOVERY_BUTTON_HINT in label or RECOVERY_TEXT in text):
                return url
        return None

    async def find_recent_recovery_url(self) -> str | None:
        async for dialog in self.client.iter_dialogs(limit=80):
            entity = dialog.entity
            if getattr(entity, "bot", False) is not True:
                continue
            try:
                messages = await self.client.get_messages(entity, limit=12)
            except Exception:
                continue
            for message in messages:
                if message.out:
                    continue
                url = self.recovery_url_from_message(message)
                if url:
                    emit("recovery_link_found", source="history", message_id=message.id)
                    return url
        return None

    async def wait_new_recovery_url(self) -> str | None:
        future = asyncio.get_running_loop().create_future()

        async def handler(event):
            if not event.is_private or event.out:
                return
            url = self.recovery_url_from_message(event.message)
            if url and not future.done():
                emit("recovery_link_found", source="live", message_id=event.message.id)
                future.set_result(url)

        self.client.add_event_handler(handler, events.NewMessage(incoming=True))
        try:
            return await asyncio.wait_for(
                future, timeout=self.settings.botson_recovery_wait_seconds
            )
        except asyncio.TimeoutError:
            return None
        finally:
            self.client.remove_event_handler(handler)

    async def join_using_recovery(self, entity, report: dict, source: str, stage: str):
        url = await self.find_recent_recovery_url()
        if not url:
            report["trail"].append(
                f"ACESSO: não encontrei link de recuperação no PV ({source})."
            )
            return entity, "recovery_link_not_found"
        report["trail"].append(
            "ACESSO: encontrei no PV o botão de retorno enviado pelo BOTSON e usei esse link."
        )
        joined, status = await self.join_preview(url, cached_entity=entity, stage=stage)
        return joined or entity, status

    async def run_access_cycle(
        self, target: str, entity, report: dict, started_member: bool
    ):
        access = {
            "initial": "inside" if started_member else "outside",
            "leave": "not_run",
            "reentry": "not_run",
            "final": "unknown",
        }
        if started_member:
            access["final"] = "inside"
            report["access"] = access
            return entity, True
        report["trail"].append(
            "ACESSO: sentinela começou FORA; buscando no PV o caminho real de retorno."
        )
        entity, status = await self.join_using_recovery(
            entity, report, "estado inicial fora", "initial-recovery"
        )
        access["reentry"] = status
        if status == "join_request_sent":
            access["final"] = "pending_approval"
            report["warnings"].append(
                "Acesso: pedido de entrada enviado pelo link de recuperação; aguardando aprovação do BOTSON."
            )
            report["trail"].append(
                "ACESSO: pedido de reentrada enviado pelo link recebido no PV."
            )
            report["access"] = access
            return entity, False
        if status != "ok":
            access["final"] = "failed"
            report["failures"].append(
                f"Acesso: não conseguiu usar o retorno do PV ({status})."
            )
            report["access"] = access
            return entity, False
        access["final"] = "inside"
        report["trail"].append(
            "ACESSO: voltou para a prévia usando o link recebido no PV."
        )
        report["access"] = access
        return entity, True

    async def run_exit_reentry_end(self, entity, report: dict) -> None:
        access = report["access"]
        report["trail"].append(
            "ACESSO FINAL: iniciando teste real sair → receber PV → pedir reentrada."
        )
        waiter = asyncio.create_task(self.wait_new_recovery_url())
        await asyncio.sleep(0)
        leave_status = await self.leave_preview(entity, stage="final")
        access["leave"] = leave_status
        if leave_status != "ok":
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            access["final"] = "inside_or_unknown"
            report["failures"].append(
                f"Acesso final: não conseguiu sair ({leave_status})."
            )
            return
        report["trail"].append(
            "ACESSO FINAL: saiu da prévia e aguardou a mensagem privada de recuperação."
        )
        recovery_url = await waiter
        if not recovery_url:
            access["reentry"] = "recovery_message_not_received"
            access["final"] = "outside_failed"
            report["failures"].append(
                "Acesso final: saiu, mas não recebeu no PV o link de retorno dentro da janela de teste."
            )
            return
        report["trail"].append(
            "ACESSO FINAL: recebeu no PV o botão de retorno do BOTSON e usou o link."
        )
        _, status = await self.join_preview(
            recovery_url, cached_entity=entity, stage="final-recovery"
        )
        access["reentry"] = status
        if status in {"ok", "join_request_sent"}:
            approved = await self.wait_for_membership(
                entity, self.settings.botson_entry_wait_seconds
            )
            if approved:
                access["final"] = "inside"
                report["trail"].append(
                    "ACESSO FINAL: reentrada realizada/aprovada; terminou DENTRO."
                )
            else:
                access["final"] = "pending_approval"
                report["warnings"].append(
                    "Acesso final: pedido enviado pelo link do PV e ainda pendente de aprovação."
                )
            return
        access["final"] = "outside_failed"
        report["failures"].append(
            f"Acesso final: recebeu o link no PV, mas não conseguiu pedir/reentrar ({status})."
        )

    async def run_secretary_test(self, target: str) -> dict:
        report = {
            "trail": [],
            "conversations": [],
            "warnings": [],
            "failures": [],
            "clicked_count": 0,
            "access": {},
        }
        cached_entity = None
        try:
            kind, value = await self.resolve_preview(target)
            if kind == "entity":
                cached_entity = value
        except Exception:
            pass
        started_member = bool(cached_entity and await self.is_member(cached_entity))
        entity, can_test = await self.run_access_cycle(
            target, cached_entity, report, started_member
        )
        if can_test and entity:
            messages = await self.capture(entity, limit=12)
            latest_id = messages[-1]["message_id"] if messages else None
            place = entity_label(entity, latest_id)
            report["trail"].append(f"Acessei a prévia {place}.")
            report["trail"].append(
                f"Li {len(messages)} mensagens recentes procurando pontos de entrada do funil."
            )
            report["conversations"].append({"place": place, "messages": messages})
            discovered_bots = set(self.settings.botson_bot_targets)
            for message in messages:
                for button in message["buttons"]:
                    bot = telegram_bot_from_url(button.get("url"))
                    if bot:
                        discovered_bots.add(bot)
                        report["trail"].append(
                            f"Prévia: encontrei “{button.get('text') or 'sem texto'}” → @{bot}; marcado para teste ativo."
                        )
            if not discovered_bots:
                report["warnings"].append(
                    "Nenhum bot de destino foi descoberto nesta Secretaria."
                )
            else:
                visited = set()
                for bot_target in sorted(discovered_bots):
                    try:
                        bot = await self.client.get_entity(bot_target.lstrip("@"))
                        await self.walk_bot(bot, report, visited)
                    except Exception as exc:
                        report["failures"].append(
                            f"Bot @{bot_target.lstrip('@')}: {type(exc).__name__}."
                        )
            await self.run_exit_reentry_end(entity, report)
        emit(
            "secretary_test_finished",
            target_ref=_target_token(target),
            failures=len(report["failures"]),
            warnings=len(report["warnings"]),
            clicked_count=report["clicked_count"],
            access=report["access"],
        )
        return report

    async def identify_secretary(self, target: str, report: dict) -> tuple[str, str]:
        try:
            kind, value = await self.resolve_preview(target)
            if kind == "entity":
                return name_of(value), entity_label(value)
        except Exception:
            pass
        conversations = report.get("conversations", [])
        if conversations:
            place = conversations[0].get("place") or target
            return place.split(" | ", 1)[0], place
        return target, target

    async def run_one(self, target: str, target_index: int | None = None) -> dict:
        report = await self.run_secretary_test(target)
        name, identity = await self.identify_secretary(target, report)
        return {
            "target": (
                f"invite:{_target_token(target)}" if invite_hash(target) else target
            ),
            "target_index": target_index,
            "name": name,
            "identity": identity,
            "report": report,
            "tested_at": time.time(),
        }

    async def run_fleet(self) -> list[dict]:
        results = []
        for index, target in enumerate(self.settings.botson_previews):
            results.append(await self.run_one(target, index))
        return results
