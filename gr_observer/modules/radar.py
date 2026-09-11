"""Passive Radar module.

This is the original observer behavior moved behind a module boundary.  It has
no Telegram mutation port and therefore cannot send, click, join or leave.
"""

from __future__ import annotations

import asyncio
import logging
import re

from telethon import errors, functions, types, utils

from ..catalog import DIALOGS
from ..domain import kind_of, now, title_of

log = logging.getLogger("gr-observer.radar")

RULE_WORDS = re.compile(
    r"\b(regras?|proibid[oa]s?|não pode|nao pode|ban|divulga(?:ção|cao)|flood|spam|links?|pv|privado)\b",
    re.I,
)
URL_RE = re.compile(r"(?:https?://|t\.me/|telegram\.me/)[^\s<>]+", re.I)
TELEGRAM_LINK_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:(?:joinchat/|\+)([A-Za-z0-9_-]+)|@?([A-Za-z0-9_]{5,}))/?$",
    re.I,
)


def telegram_link_target(url: str) -> tuple[str, str] | None:
    match = TELEGRAM_LINK_RE.fullmatch(url.strip().rstrip(".,);]"))
    if not match:
        return None
    if match.group(1):
        return "invite", match.group(1)
    return "public", match.group(2)


class RadarModule:
    module_id = "radar"

    def __init__(self, pool, settings, pause_callback, notify_callback=None):
        self.pool = pool
        self.settings = settings
        self.pause_callback = pause_callback
        self.notify_callback = notify_callback
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
            """INSERT INTO chats(chat_id,title,username,kind,last_seen,membership_status)
               VALUES($1,$2,$3,$4,$5,'joined')
               ON CONFLICT(chat_id) DO UPDATE SET
               title=$2,username=$3,kind=$4,last_seen=$5,membership_status='joined'""",
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
        try:
            chat_id = int(utils.get_peer_id(entity))
        except TypeError:  # lightweight test doubles
            chat_id = int(entity.id)
        previous = None
        if hasattr(self.pool, "fetchrow"):
            previous = await self.pool.fetchrow(
                "SELECT can_text FROM chats WHERE chat_id=$1", chat_id
            )
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
        if (
            kind == "group"
            and previous
            and previous["can_text"] is False
            and can_text is True
            and self.notify_callback is not None
            and not await self.pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM group_repost_state WHERE chat_id=$1)",
                chat_id,
            )
        ):
            await self.notify_callback(
                f"🟢 {title_of(entity)} abriu para mensagens agora. Entre e publique seu texto-modelo."
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
            url = url.rstrip(".,);]")
            await self.pool.execute(
                """INSERT INTO discovered_links(chat_id,message_id,url,observed_at)
                   VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING""",
                chat_id,
                message_id,
                url,
                now(),
            )
            if telegram_link_target(url):
                await self.pool.execute(
                    """INSERT INTO link_targets(url,source_chat_id,first_seen)
                       VALUES($1,$2,$3) ON CONFLICT(url) DO UPDATE SET
                       source_chat_id=COALESCE(link_targets.source_chat_id,$2)""",
                    url,
                    chat_id,
                    now(),
                )

    async def audit_link(self, link_id: int) -> str:
        row = await self.pool.fetchrow(
            "SELECT url FROM link_targets WHERE id=$1", link_id
        )
        if not row or self.client is None:
            return "Radar desligado ou link não encontrado"
        url = str(row["url"])
        target = telegram_link_target(url)
        preview_title = None
        preview_kind = None
        request_needed = False
        if not target:
            status, error = "invalid", "formato não reconhecido"
            entity = None
        else:
            kind, value = target
            entity = None
            try:
                if kind == "invite":
                    invite = await self.client(
                        functions.messages.CheckChatInviteRequest(value)
                    )
                    if isinstance(invite, types.ChatInviteAlready):
                        entity = invite.chat
                        status, error = "joined", None
                    else:
                        status, error = "not_joined", None
                        # A private invite preview normally exposes enough flags
                        # to separate broadcast channels from discussion groups,
                        # even though it does not expose a stable chat id yet.
                        preview_title = getattr(invite, "title", None)
                        preview_kind = (
                            "channel" if getattr(invite, "broadcast", False) else "group"
                        )
                        request_needed = bool(
                            getattr(invite, "request_needed", False)
                        )
                else:
                    entity = await self.client.get_entity(value)
                    status = (
                        "not_joined" if getattr(entity, "left", False) else "joined"
                    )
                    error = None
            except (errors.InviteHashExpiredError, errors.InviteHashInvalidError) as exc:
                status, error = "invalid", type(exc).__name__
            except errors.FloodWaitError:
                raise
            except Exception as exc:
                status, error = "inaccessible", type(exc).__name__
        target_chat_id = utils.get_peer_id(entity) if entity is not None else None
        title = title_of(entity) if entity is not None else preview_title
        target_kind = kind_of(entity) if entity is not None else preview_kind
        request_needed = False if entity is not None else request_needed
        await self.pool.execute(
            """UPDATE link_targets SET access_status=$2,target_chat_id=$3,
               title=COALESCE($4,title),kind=COALESCE($5,kind),last_error=$6,
               last_checked=$7,request_needed=$8 WHERE id=$1""",
            link_id,
            status,
            target_chat_id,
            title,
            target_kind,
            error,
            now(),
            request_needed,
        )
        if entity is not None and status == "joined":
            await self.save_chat(entity)
        return status

    async def audit_pending_links(self, limit: int = 40) -> None:
        rows = await self.pool.fetch(
            """SELECT id FROM link_targets WHERE disposition='active'
               ORDER BY last_checked NULLS FIRST,first_seen LIMIT $1""",
            limit,
        )
        for row in rows:
            await self.audit_link(int(row["id"]))
            await asyncio.sleep(0.4)

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
        seen_chat_ids: set[int] = set()
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            if kind_of(entity) not in {"group", "channel"}:
                continue
            try:
                seen_chat_ids.add(int(utils.get_peer_id(entity)))
                await self.save_chat(entity)
                await self.inspect_permissions(entity)
                history_due = await self.pool.fetchval(
                    """SELECT last_history_scanned IS NULL OR
                       last_history_scanned < NOW() - INTERVAL '6 hours'
                       FROM chats WHERE chat_id=$1""",
                    int(utils.get_peer_id(entity)),
                )
                if history_due:
                    await self.inspect_history(entity)
                    await self.pool.execute(
                        "UPDATE chats SET last_history_scanned=NOW() WHERE chat_id=$1",
                        int(utils.get_peer_id(entity)),
                    )
                await asyncio.sleep(1.2)
            except errors.FloodWaitError:
                raise
            except Exception as exc:
                log.warning(
                    "Falha de leitura chat=%s erro=%s", entity.id, type(exc).__name__
                )
        if seen_chat_ids:
            await self.pool.execute(
                """UPDATE chats SET membership_status='left'
                   WHERE membership_status='joined'
                   AND NOT(chat_id=ANY($1::bigint[]))""",
                list(seen_chat_ids),
            )
        await self.audit_pending_links()

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
