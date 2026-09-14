"""Explicit integration adapter for the Group Attendance rib.

This module contains no import-time mutation. The composition root calls
``register_group_reply`` explicitly and selects ``GroupControlPanel`` explicitly,
so the module catalog remains the single source of IDs/order and Observer.setup
remains owned by the application spine.
"""

from __future__ import annotations

from .catalog import match_command
from .clean_menu_panel import CleanMenuPanelMixin
from .modules.group_reply_contacts import GroupReplyWithContacts
from .panel import ControlPanel as BaseControlPanel
from .pv_message_panel import PvMessageEditorPanelMixin


def register_group_reply(application) -> None:
    """Attach the Group Attendance rib to the existing central registry."""
    application.registry.register(
        "group_reply",
        GroupReplyWithContacts(application.storage, application.settings),
    )


class GroupControlPanel(
    CleanMenuPanelMixin,
    PvMessageEditorPanelMixin,
    BaseControlPanel,
):
    """Operator panel with compact menus, group controls and PV copy editor."""

    async def on_message(self, event) -> None:
        if not self.app.is_admin(event):
            return
        command_id = match_command(event.raw_text or "", "panel")
        if command_id == "group_reply.enable":
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
            result = await self.app.set_module_enabled("group_reply", enabled)
            await event.answer(result[:200], alert=True)
            await self.show_functions(event)
            return
        await super().on_callback(event)
