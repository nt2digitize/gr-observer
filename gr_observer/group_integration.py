"""Small integration layer for the optional Group Attendance rib.

Kept separate so the existing Radar/PV/BOTSON composition stays untouched.
Importing this module patches the catalog and control-panel adapter, then wraps
Observer.setup to register the new rib before it is used.
"""

from __future__ import annotations

import asyncio

from . import application
from .catalog import COMMANDS, MODULES, match_command
from .modules.group_reply import GroupReplyModule
from .panel import ControlPanel as BaseControlPanel


MODULES.setdefault(
    "group_reply",
    {
        "order": 25,
        "dispatch_order": 25,
        "label": "Atendimento de Grupos",
        "description": "Detecta pedidos allowlisted, espera 3–7 min e responde com cooldown e rastreio Grupo → PV.",
        "initial_reason": "Desligado por padrão; configure GROUP_REPLY_ALLOWLIST",
        "default_enabled": False,
        "active_writes": True,
    },
)
COMMANDS.setdefault(
    "group_reply.enable",
    {
        "order": 81,
        "module": "group_reply",
        "surfaces": ("panel",),
        "triggers": ("ligar atendimento grupos", "/ligar_grupos"),
        "help": "ligar respostas automáticas nos grupos permitidos",
    },
)
COMMANDS.setdefault(
    "group_reply.disable",
    {
        "order": 82,
        "module": "group_reply",
        "surfaces": ("panel",),
        "triggers": ("desligar atendimento grupos", "/desligar_grupos"),
        "help": "desligar respostas automáticas nos grupos",
    },
)
COMMANDS.setdefault(
    "group_reply.preview",
    {
        "order": 83,
        "module": "group_reply",
        "surfaces": ("panel",),
        "triggers": ("ver atendimento grupos", "/atendimento_grupos"),
        "help": "ver grupos, atraso, cooldown e quantidade de respostas",
    },
)


class ExtendedControlPanel(BaseControlPanel):
    async def on_message(self, event) -> None:
        if not self.app.is_admin(event):
            return
        command_id = match_command(event.raw_text or "", "panel")
        if command_id == "group_reply.enable":
            item = self.app.registry.get("group_reply")
            if not item.implementation.allowlist_raw:
                await event.respond(
                    "Atendimento de Grupos não ligado: configure GROUP_REPLY_ALLOWLIST com IDs, @usernames ou links privados dos grupos permitidos.",
                    parse_mode=None,
                )
                return
            await event.respond(
                await self.app.set_module_enabled("group_reply", True), parse_mode=None
            )
            return
        if command_id == "group_reply.disable":
            await event.respond(
                await self.app.set_module_enabled("group_reply", False), parse_mode=None
            )
            return
        if command_id == "group_reply.preview":
            await event.respond(
                self.app.registry.get("group_reply").implementation.preview(),
                parse_mode=None,
                link_preview=False,
            )
            return
        await super().on_message(event)

    async def on_callback(self, event) -> None:
        if not self.app.is_admin(event):
            await super().on_callback(event)
            return
        data = event.data.decode("utf-8", errors="replace")
        if data in {"module:group_reply:on", "module:group_reply:off"}:
            enabled = data.endswith(":on")
            item = self.app.registry.get("group_reply")
            if enabled and not item.implementation.allowlist_raw:
                await event.answer(
                    "Configure GROUP_REPLY_ALLOWLIST antes de ligar.", alert=True
                )
                return
            result = await self.app.set_module_enabled("group_reply", enabled)
            await event.answer(result[:200], alert=True)
            await self.show_functions(event)
            return
        await super().on_callback(event)


application.ControlPanel = ExtendedControlPanel
_original_setup = application.Observer.setup


async def _setup_with_group_reply(self) -> None:
    await _original_setup(self)
    if "group_reply" in getattr(self.registry, "_modules", {}):
        return
    await self.pool.execute(
        """INSERT INTO module_control(module_id,enabled,reason)
           VALUES('group_reply',FALSE,'Desligado por padrão; configure GROUP_REPLY_ALLOWLIST')
           ON CONFLICT(module_id) DO NOTHING"""
    )
    module = GroupReplyModule(self.storage, self.settings)
    self.registry.register("group_reply", module)
    self.registry.apply_states(await self.storage.module_states())

    # A worker may already exist because another rib was enabled at startup.
    # Register the new action handler immediately if its writer is live.
    if self.writer is not None:
        key = ("group_reply", "send_group_reply")
        if key not in self.writer.handlers:
            module.register_actions(self.writer)

    item = self.registry.get("group_reply")
    if item.enabled:
        if not module.allowlist_raw:
            item.enabled = False
            item.reason = "Falta configurar GROUP_REPLY_ALLOWLIST"
            await self.storage.set_module_state(item.module_id, False, item.reason)
        elif self.user is not None:
            await self._connect_module("group_reply")
        elif not self.worker or self.worker.done():
            self.worker = asyncio.create_task(
                self.user_runtime(), name="telegram-user-runtime"
            )


application.Observer.setup = _setup_with_group_reply
