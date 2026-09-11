"""BOTSON test module: command adapter, durable runs and reports."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from ..catalog import (
    BRAND,
    DIALOGS,
    match_command,
    normalize_text,
    parse_botson_detail,
)
from ..storage import run_id_for
from .botson_engine import (
    PAIR_MARKER,
    BotsonEngine,
    button_policy,
)


def chunks(text: str, max_chars=3600) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts, current, size = [], [], 0
    for line in text.splitlines():
        line_size = len(line) + 1
        if current and size + line_size > max_chars:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += line_size
    if current:
        parts.append("\n".join(current))
    return parts


def status_icon(report: dict) -> str:
    if report.get("failures"):
        return "❌"
    if report.get("warnings"):
        return "⚠️"
    return "✅"


def access_label(report: dict) -> str:
    return {
        "inside": "✅ dentro",
        "pending_approval": "⏳ aguardando aprovação",
        "outside_failed": "❌ fora / falhou ao voltar",
        "failed": "❌ entrada falhou",
        "inside_or_unknown": "⚠️ saída não confirmada",
    }.get(report.get("access", {}).get("final"), "⚪ não confirmado")


def fleet_text(results: list[dict]) -> str:
    ok = sum(1 for item in results if status_icon(item["report"]) == "✅")
    warning = sum(1 for item in results if status_icon(item["report"]) == "⚠️")
    failed = sum(1 for item in results if status_icon(item["report"]) == "❌")
    lines = [
        BRAND["botson_test"],
        f"Secretarias testadas: {len(results)}",
        f"✅ {ok}  ⚠️ {warning}  ❌ {failed}",
        "",
        "Secretarias:",
    ]
    for index, item in enumerate(results, 1):
        lines.append(
            f"{index}. {status_icon(item['report'])} {item['name']} — {access_label(item['report'])}"
        )
    lines += [
        "",
        "Digite:",
        "1 = resumo",
        "1 acesso",
        "1 jornada",
        "1 conversas",
        "1 evidencias",
        "1 tecnico",
        "1 retestar",
    ]
    return "\n".join(lines)


def compact_summary(item: dict) -> str:
    report = item["report"]
    return "\n".join(
        [
            f"{status_icon(report)} {item['name']}",
            item["identity"],
            "",
            f"🔐 Acesso: {access_label(report)}",
            f"🧭 Etapas: {len(report.get('trail', []))}",
            f"👆 Cliques: {report.get('clicked_count', 0)}",
            f"❌ Falhas: {len(report.get('failures', []))}",
            f"⚠️ Alertas: {len(report.get('warnings', []))}",
        ]
    )


def render_access(item: dict) -> str:
    access = item["report"].get("access", {})
    return "\n".join(
        [
            f"🔐 ACESSO — {item['name']}",
            item["identity"],
            f"Estado inicial: {access.get('initial', 'unknown')}",
            f"Saída: {access.get('leave', 'not_run')}",
            f"Reentrada: {access.get('reentry', 'not_run')}",
            f"Estado final: {access_label(item['report'])}",
        ]
    )


def render_journey(item: dict) -> str:
    lines = [f"🧭 JORNADA — {item['name']}"]
    lines.extend(
        f"{index:02d}. {step}"
        for index, step in enumerate(item["report"].get("trail", []), 1)
    )
    return "\n".join(lines)


def render_chats(item: dict) -> str:
    lines = [f"💬 CONVERSAS — {item['name']}"]
    count = 0
    for conversation in item["report"].get("conversations", []):
        lines.append(f"\n📍 {conversation.get('place', 'Conversa')}")
        for message in conversation.get("messages", []):
            count += 1
            lines.append(
                f"{'➡️' if message.get('outgoing') else '⬅️'} "
                f"#{message.get('message_id')} — {message.get('sender', '')}"
            )
            lines.append(message.get("text") or "[sem texto]")
    if not count:
        lines.append("\nNenhuma conversa capturada.")
    return "\n".join(lines)


def render_evidence(item: dict) -> str:
    lines = [f"🔎 EVIDÊNCIAS — {item['name']}"]
    found = 0
    for conversation in item["report"].get("conversations", []):
        for message in conversation.get("messages", []):
            if message.get("link"):
                found += 1
                lines.append(f"🔗 #{message.get('message_id')}: {message['link']}")
            for button in message.get("buttons", []):
                found += 1
                allowed, reason = button_policy(button)
                state = "candidato a clique" if allowed else "não clicado"
                extra = f" → {button.get('url')}" if button.get("url") else ""
                lines.append(
                    f"🔘 {button.get('text') or 'sem texto'}{extra} [{state}: {reason}]"
                )
    if not found:
        lines.append("Nenhum link/botão registrado.")
    return "\n".join(lines)


def render_technical(item: dict) -> str:
    report = item["report"]
    lines = [f"🛠 TÉCNICO — {item['name']}", f"Alvo: {item['target']}"]
    if report.get("warnings"):
        lines += ["", "⚠️ Alertas:"] + [f"• {warning}" for warning in report["warnings"]]
    if report.get("failures"):
        lines += ["", "❌ Falhas:"] + [f"• {failure}" for failure in report["failures"]]
    if not report.get("warnings") and not report.get("failures"):
        lines += ["", "✅ Nenhuma falha/alerta."]
    return "\n".join(lines)


DETAIL_RENDERERS = {
    "summary": compact_summary,
    "access": render_access,
    "journey": render_journey,
    "chats": render_chats,
    "evidence": render_evidence,
    "technical": render_technical,
}


class BotsonModule:
    module_id = "botson"

    def __init__(self, storage, settings):
        self.storage = storage
        self.settings = settings
        self.client = None
        self.me = None
        self.controller_id = settings.botson_controller_id

    async def on_connect(self, client, me) -> None:
        self.client = client
        self.me = me
        if self.controller_id is None:
            self.controller_id = await self.storage.stored_controller_id()
        if self.controller_id is None:
            # Backwards-compatible read of the marker used by the former
            # standalone runner. New pairings are authoritative in PostgreSQL.
            messages = await client.get_messages("me", limit=30, search=PAIR_MARKER)
            for message in messages:
                text = (message.raw_text or "").strip()
                if text.startswith(PAIR_MARKER):
                    raw = text.split("=", 1)[1].strip()
                    if raw.lstrip("-").isdigit():
                        self.controller_id = int(raw)
                        await self.storage.save_controller_id(self.controller_id)
                        break

    async def on_disconnect(self) -> None:
        self.client = None
        self.me = None

    def register_actions(self, writer) -> None:
        writer.register(self.module_id, "reply", self.action_reply)
        writer.register(self.module_id, "run_fleet", self.action_run_fleet)
        writer.register(self.module_id, "show_detail", self.action_show_detail)
        writer.register(self.module_id, "retest_one", self.action_retest_one)

    @staticmethod
    def _event_key(event) -> str:
        chat_id = event.chat_id if event.chat_id is not None else event.sender_id
        return f"{chat_id}:{event.id}"

    async def handle_event(self, event) -> bool:
        if not event.is_private or event.out:
            return False
        text = (event.raw_text or "").strip()
        event_key = self._event_key(event)
        peer = int(event.chat_id if event.chat_id is not None else event.sender_id)

        pair_phrase = (
            f"ativar botson {self.settings.botson_pair_code}".casefold()
            if self.settings.botson_pair_code
            else None
        )
        if (
            self.controller_id is None
            and pair_phrase
            and text.casefold() == pair_phrase
        ):
            outcome = await self.storage.pair_and_enqueue(
                event_key=event_key,
                controller_id=int(event.sender_id),
                reply_peer=peer,
                reply_text=DIALOGS["botson.paired"],
            )
            if outcome == "accepted":
                self.controller_id = int(event.sender_id)
            return True

        if self.controller_id is None or int(event.sender_id) != int(
            self.controller_id
        ):
            return False

        command_id = match_command(text, "user")
        if normalize_text(text) == normalize_text(self.settings.botson_trigger_text):
            command_id = "botson.run"
        if command_id == "botson.run":
            outcome = await self.storage.accept_and_enqueue(
                source="telegram-user",
                event_key=event_key,
                module_id=self.module_id,
                event_payload={
                    "kind": "command",
                    "command_id": command_id,
                    "sender_id": event.sender_id,
                    "chat_id": peer,
                },
                action_type="run_fleet",
                action_payload={"reply_peer": peer, "requested_by": event.sender_id},
                exclusive=True,
            )
            if outcome == "busy":
                await self.storage.enqueue_action(
                    action_key=f"botson:reply:{event_key}:busy",
                    module_id=self.module_id,
                    action_type="reply",
                    payload={"peer": peer, "text": DIALOGS["botson.busy"]},
                )
            return True

        detail = parse_botson_detail(text)
        if detail:
            action_type = "retest_one" if detail.action == "retest" else "show_detail"
            outcome = await self.storage.accept_and_enqueue(
                source="telegram-user",
                event_key=event_key,
                module_id=self.module_id,
                event_payload={
                    "kind": "detail",
                    "index": detail.index,
                    "action": detail.action,
                    "sender_id": event.sender_id,
                },
                action_type=action_type,
                action_payload={
                    "reply_peer": peer,
                    "index": detail.index,
                    "detail": detail.action,
                },
                exclusive=action_type == "retest_one",
            )
            if outcome == "busy":
                await self.storage.enqueue_action(
                    action_key=f"botson:reply:{event_key}:busy",
                    module_id=self.module_id,
                    action_type="reply",
                    payload={"peer": peer, "text": DIALOGS["botson.busy"]},
                )
            return True
        return False

    async def enqueue_panel_run(self, event_key: str) -> str:
        if self.controller_id is None:
            return "BOTSON sem controlador pareado."
        return await self.storage.accept_and_enqueue(
            source="control-bot",
            event_key=event_key,
            module_id=self.module_id,
            event_payload={
                "kind": "panel_command",
                "requested_by": self.settings.admin_id,
            },
            action_type="run_fleet",
            action_payload={
                "reply_peer": self.controller_id,
                "requested_by": self.settings.admin_id,
            },
            exclusive=True,
        )

    async def action_reply(self, action: dict, effects) -> dict:
        payload = action["payload"]
        parts = chunks(payload["text"])
        for index, part in enumerate(parts):
            await effects.send_text(
                payload["peer"], part, f"{action['action_key']}:part:{index}"
            )
        return {"parts": len(parts)}

    async def action_run_fleet(self, action: dict, effects) -> dict:
        run_id = run_id_for(action["action_key"])
        peer = int(action["payload"]["reply_peer"])
        await self.storage.start_run(run_id)
        try:
            await effects.send_text(
                peer,
                f"Teste BOTSON iniciado. 🔎\nVou testar {len(self.settings.botson_previews)} Secretaria(s), incluindo saída e nova entrada.",
                f"{action['action_key']}:started",
            )
            engine = BotsonEngine(self.client, effects, self.settings, run_id)
            results = await engine.run_fleet()
            result = {"results": results, "created_at": time.time()}
            await self.storage.finish_run_and_enqueue_reply(
                run_id=run_id,
                result=result,
                reply_peer=peer,
                reply_text=fleet_text(results),
            )
            return {"run_id": run_id, "tested": len(results)}
        except Exception as exc:
            await self.storage.fail_run_and_enqueue_reply(
                run_id=run_id,
                exc=exc,
                reply_peer=peer,
                reply_text=f"⚠️ Teste interrompido: {type(exc).__name__}.",
            )
            return {"run_id": run_id, "failed": type(exc).__name__}

    async def _latest_results(self):
        latest = await self.storage.latest_successful_run(self.module_id)
        if not latest:
            return None
        finished = latest["finished_at"]
        if finished:
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - finished).total_seconds()
            if age > self.settings.botson_report_ttl_seconds:
                return None
        return latest

    async def action_show_detail(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["reply_peer"])
        latest = await self._latest_results()
        if not latest:
            text = DIALOGS["botson.expired"]
        else:
            results = latest["result"].get("results", [])
            index = int(action["payload"]["index"])
            if index < 0 or index >= len(results):
                text = DIALOGS["botson.not_found"]
            else:
                renderer = DETAIL_RENDERERS[action["payload"]["detail"]]
                text = renderer(results[index])
        parts = chunks(text)
        for index, part in enumerate(parts):
            await effects.send_text(peer, part, f"{action['action_key']}:part:{index}")
        return {"parts": len(parts)}

    async def action_retest_one(self, action: dict, effects) -> dict:
        peer = int(action["payload"]["reply_peer"])
        latest = await self._latest_results()
        if not latest:
            await effects.send_text(
                peer, DIALOGS["botson.expired"], f"{action['action_key']}:expired"
            )
            return {"retested": False, "reason": "expired"}
        results = list(latest["result"].get("results", []))
        index = int(action["payload"]["index"])
        if index < 0 or index >= len(results):
            await effects.send_text(
                peer, DIALOGS["botson.not_found"], f"{action['action_key']}:not-found"
            )
            return {"retested": False, "reason": "not_found"}
        run_id = run_id_for(action["action_key"])
        await self.storage.start_run(run_id)
        try:
            engine = BotsonEngine(self.client, effects, self.settings, run_id)
            target_index = int(results[index].get("target_index", index))
            if target_index < 0 or target_index >= len(self.settings.botson_previews):
                raise IndexError("preview fora da allowlist atual")
            results[index] = await engine.run_one(
                self.settings.botson_previews[target_index], target_index
            )
            result = {
                "results": results,
                "created_at": time.time(),
                "retested_index": index,
            }
            await self.storage.finish_run_and_enqueue_reply(
                run_id=run_id,
                result=result,
                reply_peer=peer,
                reply_text=compact_summary(results[index]),
            )
            return {"run_id": run_id, "retested": True, "index": index}
        except Exception as exc:
            await self.storage.fail_run_and_enqueue_reply(
                run_id=run_id,
                exc=exc,
                reply_peer=peer,
                reply_text=f"⚠️ Teste interrompido: {type(exc).__name__}.",
            )
            return {"run_id": run_id, "failed": type(exc).__name__}
