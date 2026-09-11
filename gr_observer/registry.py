"""Ordered module registry: the ribs attached to the single runtime spine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .catalog import MODULES


@dataclass
class RegisteredModule:
    module_id: str
    implementation: Any
    enabled: bool = False
    reason: str = "Pausado"

    @property
    def spec(self) -> dict:
        return MODULES[self.module_id]


class ModuleRegistry:
    def __init__(self) -> None:
        self._modules: dict[str, RegisteredModule] = {}

    def register(self, module_id: str, implementation: Any) -> None:
        if module_id not in MODULES:
            raise KeyError(f"módulo sem catálogo: {module_id}")
        if module_id in self._modules:
            raise ValueError(f"módulo duplicado: {module_id}")
        self._modules[module_id] = RegisteredModule(module_id, implementation)

    def get(self, module_id: str) -> RegisteredModule:
        return self._modules[module_id]

    def ordered(self) -> list[RegisteredModule]:
        return sorted(self._modules.values(), key=lambda item: item.spec["order"])

    def enabled(self) -> list[RegisteredModule]:
        return [item for item in self.ordered() if item.enabled]

    def apply_states(self, states: dict[str, dict]) -> None:
        for item in self._modules.values():
            state = states.get(item.module_id, {})
            item.enabled = bool(
                state.get("enabled", MODULES[item.module_id]["default_enabled"])
            )
            item.reason = state.get("reason", "Pausado")
