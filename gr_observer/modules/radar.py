"""Passive Radar module.

This is the original observer behavior moved behind a module boundary.  It has
no Telegram mutation port and therefore cannot send, click, join or leave.
"""

from __future__ import annotations

import asyncio
import logging
import re

from telethon import errors, functions, utils

from ..catalog import DIALOGS
from ..domain import kind_of, now, title_of

log = logging.getLogger("gr-observer.radar")

RULE_WORDS = re.compile(
    r"\b(regras?|proibid[oa]s?|não pode|nao pode|ban|divulga(?:ção|cao)|flood|spam|links?|pv|privado)\b",
    re.I,
)
URL_RE = re.compile(r"(?:https?://|t\.me/|telegram\.me/)[^\s<>]+", re.I)


class RadarModule:
    module_id = "radar"

    def __init__(self, pool, settings, pause_callback):
        self.pool = pool
        self.settings = settings
        self.pause_callback = pause_callback
        self.client = None
        self.me = None
        self.scan_task: asyncio.Task | None = None

    async def on_connect(self, client, me) -> None:
        self.client = client
        self.me = me
        if not self.scan_task or self.scan_task.done():
            self.scan_task = asyncio.create_task(
                self._guarded_scan_loop(), name="radar-scan"
            )

    async def on_disconnect(self) -> None:
        task, self.scan_task = self.scan_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.client = None
        self.me = None

    async def _guarded_scan_loop(self) -> None:
        try:
            await self.scan_loop()
        except asyncio.CancelledError:
            raise
        except errors.FloodWaitError as exc:
            await self.pause_callback(
                self.module_id,
                f"Pausado por FloodWait ({exc.seconds}s); revisar antes de ligar",
            )
        except Exception as exc:
            log.exception("Varredura do Radar parou")
            await self.pause_callback(
                self.module_id,
                f"Observação parada: {type(exc).__name__}. Confira a sessão e a conexão.",
            )

    async def save_chat(self, entity, **values) -> None:
        chat_id = utils.get_peer_id(entity)
        await self.pool.execute(
            """INSERT INTO chats(chat_id,title,username,kind,last_seen) VALUES($1,$2,$3,$4,$5)
               ON CONFLICT(chat_id) DO UPDATE SET
               title=$2,username=$3,kind=$4,last_seen=$5""",
            chat_id,
            title_of(entity),
            getattr(entity, "username", None),
            kind_of(entity),
            now(),
        )
        if values:
            allowed = {
                "can_text",
                "can_media",
                "can_links",
                "slowmode_seconds",
                "risk",
                "last_scanned",
            }
            for key, value in {
                key: value for key, value in values.items() if key in allowed
            }.items():
                await self.pool.execute(
                    f"UPDATE chats SET {key}=$1 WHERE chat_id=$2", value, chat_id
                )

    async def inspect_permissions(self, entity) -> None:
        kind = kind_of(entity)
        can_text = can_media = can_links = None
        slowmode = None
        try:
            permissions = await self.client.get_permissions(entity, self.me)
            if kind == "channel":
                rights = getattr(entity, "admin_rights", None)
                can_text = bool(
                    getattr(entity, "creator", False)
                    or (rights and getattr(rights, "post_messages", False))
                )
                can_media = can_text
                can_links = None if can_text else False
            else:
                can_text = getattr(permissions, "send_messages", None)
                can_media = (
                    getattr(permissions, "send_media", None)
                    if can_text is True
                    else can_text
                )
                # Link previews do not prove that advertising/URLs are allowed.
                can_links = False if can_text is False else None
            try:
                full = await self.client(
                    functions.channels.GetFullChannelRequest(entity)
                )
                slowmode = getattr(full.full_chat, "slowmode_seconds", None)
            except errors.FloodWaitError:
                raise
            except Exception:
                pass
        except errors.FloodWaitError:
            raise
        except Exception as exc:
            log.info("Permissão incerta chat=%s erro=%s", entity.id, type(exc).__name__)
        risk = (
            "low"
            if can_text and can_links
            else "medium"
            if can_text
            else "blocked"
            if can_text is False
            else "unknown"
        )
        await self.save_chat(
            entity,
            can_text=can_text,
            can_media=can_media,
            can_links=can_links,
            slowmode_seconds=slowmode,
            risk=risk,
            last_scanned=now(),
        )

    async def inspect_history(self, entity) -> None:
        async for message in self.client.iter_messages(
            entity, limit=self.settings.history_limit
        ):
            text = message.raw_text or ""
            if text and RULE_WORDS.search(text):
                await self.pool.execute(
                    """INSERT INTO visible_rules(chat_id,message_id,excerpt,observed_at)
                       VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING""",
                    utils.get_peer_id(entity),
                    message.id,
                    text[:600],
                    now(),
                )
            await self.capture_links(utils.get_peer_id(entity), message.id, text)
            await self.capture_bot(
                utils.get_peer_id(entity), await message.get_sender(), "history"
            )

    async def capture_links(self, chat_id, message_id, text) -> None:
        for url in URL_RE.findall(text or ""):
            await self.pool.execute(
                """INSERT INTO discovered_links(chat_id,message_id,url,observed_at)
                   VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING""",
                chat_id,
                message_id,
                url.rstrip(".,);]"),
                now(),
            )

    async def capture_bot(self, chat_id, sender, evidence) -> None:
        if not sender or not getattr(sender, "bot", False):
            return
        display = " ".join(
            filter(
                None,
                [
                    getattr(sender, "first_name", None),
                    getattr(sender, "last_name", None),
                ],
            )
        )
        await self.pool.execute(
            """INSERT INTO observed_bots(chat_id,bot_id,username,display_name,evidence,last_seen)
               VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(chat_id,bot_id) DO UPDATE SET
               username=$3,display_name=$4,evidence=$5,last_seen=$6""",
            chat_id,
            sender.id,
            getattr(sender, "username", None),
            display,
            evidence,
            now(),
        )

    async def scan_all(self) -> None:
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            if kind_of(entity) not in {"group", "channel"}:
                continue
            try:
                await self.save_chat(entity)
                await self.inspect_permissions(entity)
                await self.inspect_history(entity)
                await asyncio.sleep(1.2)
            except errors.FloodWaitError:
                raise
            except Exception as exc:
                log.warning(
                    "Falha de leitura chat=%s erro=%s", entity.id, type(exc).__name__
                )

    async def scan_loop(self) -> None:
        while True:
            await self.pool.execute(
                "DELETE FROM group_interactions WHERE observed_at < NOW() - INTERVAL '7 days'"
            )
            await self.pool.execute(
                "DELETE FROM private_origins WHERE detected_at < NOW() - INTERVAL '7 days'"
            )
            await self.pool.execute(
                "DELETE FROM drafts WHERE created_at < NOW() - INTERVAL '7 days'"
            )
            await self.scan_all()
            await asyncio.sleep(self.settings.scan_interval_minutes * 60)

    async def detect_directed(self, event, sender) -> None:
        text = event.raw_text or ""
        mentioned = bool(
            self.me.username and f"@{self.me.username.lower()}" in text.lower()
        )
        replied = False
        if event.is_reply:
            original = await event.get_reply_message()
            replied = bool(original and original.sender_id == self.me.id)
        if sender and not getattr(sender, "bot", False) and (mentioned or replied):
            await self.pool.execute(
                """INSERT INTO group_interactions(
                   user_id,chat_id,message_id,interaction_type,observed_at)
                   VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING""",
                sender.id,
                event.chat_id,
                event.id,
                "reply" if replied else "mention",
                now(),
            )

    async def detect_private_origin(self, event, sender) -> None:
        if not sender or getattr(sender, "bot", False):
            return
        row = await self.pool.fetchrow(
            """SELECT chat_id FROM group_interactions WHERE user_id=$1
               AND observed_at > NOW() - INTERVAL '7 days'
               ORDER BY observed_at DESC LIMIT 1""",
            sender.id,
        )
        if not row:
            return
        await self.pool.execute(
            """INSERT INTO private_origins(
               user_id,username,source_chat_id,confidence,detected_at)
               VALUES($1,$2,$3,'probable',$4) ON CONFLICT(user_id) DO UPDATE SET
               username=$2,source_chat_id=$3,confidence='probable',detected_at=$4""",
            sender.id,
            getattr(sender, "username", None),
            row["chat_id"],
            now(),
        )
        if re.search(
            r"\b(link|grupo|canal|vip|pv|privado)\b", event.raw_text or "", re.I
        ):
            await self.pool.execute(
                """INSERT INTO drafts(chat_id,user_id,reason,suggested_text,status,created_at)
                   VALUES($1,$2,'pedido de link no privado',$3,'pending',$4)""",
                row["chat_id"],
                sender.id,
                DIALOGS["user.private_link_request"],
                now(),
            )

    async def record_manual_post(self, event) -> bool:
        """A successful manual post remains direct evidence of permission."""
        if not (event.out and (event.is_group or event.is_channel)):
            return False
        entity = await event.get_chat()
        has_media = bool(getattr(event, "media", None))
        has_link = bool(URL_RE.search(event.raw_text or ""))
        evidence = {"can_text": True, "last_scanned": now()}
        if has_media:
            evidence["can_media"] = True
        if has_link:
            evidence.update(can_links=True, risk="low")
        await self.save_chat(entity, **evidence)
        return True

    async def handle_event(self, event) -> bool:
        if await self.record_manual_post(event):
            return False
        sender = await event.get_sender()
        if event.is_group or event.is_channel:
            entity = await event.get_chat()
            chat_id = utils.get_peer_id(entity)
            await self.save_chat(entity)
            await self.capture_bot(chat_id, sender, "new_message")
            await self.capture_links(chat_id, event.id, event.raw_text or "")
            if RULE_WORDS.search(event.raw_text or ""):
                await self.pool.execute(
                    """INSERT INTO visible_rules(chat_id,message_id,excerpt,observed_at)
                       VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING""",
                    chat_id,
                    event.id,
                    (event.raw_text or "")[:600],
                    now(),
                )
            await self.detect_directed(event, sender)
        elif event.is_private:
            await self.detect_private_origin(event, sender)
        return False
