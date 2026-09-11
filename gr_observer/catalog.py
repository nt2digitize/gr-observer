"""Stable module and text-command catalog.

Dictionary insertion order is the operator-visible order.  IDs are stable and
may be stored in the database; display labels and aliases may evolve without
changing the underlying operation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BRAND = {
    "application": "GR Observer",
    "panel": "🔎 OBSERVADOR",
    "botson_test": "🧪 BOTSON — HOMOLOGAÇÃO",
}

CATEGORIES = {
    "core": {"order": 10, "label": "Sistema"},
    "radar": {"order": 20, "label": "Radar"},
    "botson": {"order": 30, "label": "Testar BOTSON"},
}

DIALOGS = {
    "user.private_link_request": "pera aí q vou ver o link certo p vc",
    "admin.no_authorization": "Sem autorização",
    "admin.empty_list": "Nenhum item encontrado.",
    "radar.already_on": "O Radar já está ligado ou conectando.",
    "radar.start_requested": "Observação solicitada. Use /status para conferir.",
    "radar.stopped": "Observação desligada. O painel continua disponível.",
    "botson.paired": "BOTSON ativado para sua conta ✅\n\nAgora é só mandar: testar",
    "botson.busy": "Teste BOTSON já está em andamento.",
    "botson.expired": "Relatório expirou. Envie: testar",
    "botson.not_found": "Secretaria não encontrada.",
}

# Reserved for future message sequences. Keeping the namespace explicit avoids
# scattering campaign copy through business code when active outreach exists.
CAMPAIGNS: dict[str, dict] = {}

MODULES = {
    "radar": {
        "order": 10,
        "label": "Radar",
        "description": "Inventário passivo, regras, permissões, bots, links e origem provável de PV.",
        "default_enabled": False,
        "active_writes": False,
    },
    "botson": {
        "order": 20,
        "label": "Testar BOTSON",
        "description": "Homologação allowlisted de acesso, jornada, conversas e evidências.",
        "default_enabled": False,
        "active_writes": True,
    },
}

# These aliases are deliberately data, not a chain of hard-coded ``if`` tests.
# The numeric ``order`` is also used when rendering command help.
COMMANDS = {
    "core.dashboard": {
        "order": 10,
        "module": "core",
        "surfaces": ("panel",),
        "triggers": ("/start", "/observador", "painel"),
        "help": "abrir painel",
    },
    "core.status": {
        "order": 20,
        "module": "core",
        "surfaces": ("panel",),
        "triggers": ("/status", "status"),
        "help": "ver estado real",
    },
    "core.functions": {
        "order": 30,
        "module": "core",
        "surfaces": ("panel",),
        "triggers": ("/funcoes", "/funções", "funcoes", "funções"),
        "help": "listar costelas/funções",
    },
    "radar.enable": {
        "order": 40,
        "module": "radar",
        "surfaces": ("panel",),
        "triggers": ("/ligar", "ligar radar"),
        "help": "ligar Radar",
    },
    "radar.disable": {
        "order": 50,
        "module": "radar",
        "surfaces": ("panel",),
        "triggers": ("/desligar", "desligar radar"),
        "help": "desligar Radar",
    },
    "botson.enable": {
        "order": 60,
        "module": "botson",
        "surfaces": ("panel",),
        "triggers": ("ligar botson",),
        "help": "habilitar o testador",
    },
    "botson.disable": {
        "order": 70,
        "module": "botson",
        "surfaces": ("panel",),
        "triggers": ("desligar botson",),
        "help": "desabilitar o testador",
    },
    "botson.run": {
        "order": 80,
        "module": "botson",
        "surfaces": ("panel", "user"),
        "triggers": ("/testar_botson", "testar botson", "testar"),
        "help": "executar homologação",
    },
}

BOTSON_DETAILS = {
    "summary": {"order": 10, "aliases": ("resumo",), "help": "resumo"},
    "access": {"order": 20, "aliases": ("acesso",), "help": "acesso"},
    "journey": {"order": 30, "aliases": ("jornada",), "help": "jornada"},
    "chats": {"order": 40, "aliases": ("conversa", "conversas"), "help": "conversas"},
    "evidence": {
        "order": 50,
        "aliases": ("evidencia", "evidencias", "evidência", "evidências"),
        "help": "evidencias",
    },
    "technical": {"order": 60, "aliases": ("tecnico", "técnico"), "help": "tecnico"},
    "retest": {"order": 70, "aliases": ("retestar",), "help": "retestar"},
}

SETTINGS = {
    "command_order": tuple(COMMANDS),
    "botson_detail_order": tuple(BOTSON_DETAILS),
    "report_max_chars": 3600,
}


@dataclass(frozen=True)
class ParsedDetail:
    index: int
    action: str


def normalize_text(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip()).casefold()
    if value.startswith("/"):
        value = value.split("@", 1)[0]
    return value


def validate_catalog() -> None:
    seen: dict[tuple[str, str], str] = {}
    for command_id, spec in COMMANDS.items():
        for surface in spec["surfaces"]:
            for trigger in spec["triggers"]:
                key = (surface, normalize_text(trigger))
                previous = seen.get(key)
                if previous and previous != command_id:
                    raise ValueError(
                        f"gatilho duplicado {key!r}: {previous} / {command_id}"
                    )
                seen[key] = command_id


def match_command(text: str, surface: str) -> str | None:
    wanted = normalize_text(text)
    for command_id, spec in COMMANDS.items():
        if surface in spec["surfaces"] and wanted in {
            normalize_text(x) for x in spec["triggers"]
        }:
            return command_id
    return None


def parse_botson_detail(text: str) -> ParsedDetail | None:
    match = re.fullmatch(r"(\d+)(?:\s+(.+))?", normalize_text(text))
    if not match:
        return None
    raw_action = match.group(2) or "resumo"
    for action_id, spec in BOTSON_DETAILS.items():
        if raw_action in {normalize_text(x) for x in spec["aliases"]}:
            return ParsedDetail(index=int(match.group(1)) - 1, action=action_id)
    return None


validate_catalog()
