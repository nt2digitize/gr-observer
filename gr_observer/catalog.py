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
    "pv_reply": {"order": 30, "label": "Atendimento PV"},
    "botson": {"order": 40, "label": "Testar BOTSON"},
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

# Text is centralized here; the private invite itself stays in Railway's
# ``PV_PREVIEW_LINK`` variable and is never committed to the public repository.
PV_GREETING_VARIANTS = (
    "Quer ver minha esposa puta?",
    "Quer ver a minha puta?",
    "Quer ver minha safada?",
    "Quer ver minha esposa bem safada?",
    "Quer ver minha puta aprontando?",
    "Quer ver minha esposa sem-vergonha?",
    "Tá a fim de ver minha safada?",
    "Quer ver como minha esposa é puta?",
    "Quer conhecer a minha safada?",
    "Quer ver a minha mulher bem puta?",
)

LIVE_INVITE_VARIANTS = (
    "Minha safada entrou ao vivo agora 😈 Quer o link?",
    "Minha esposa tá ao vivo agora 🔥 Quer assistir?",
    "Ela acabou de entrar ao vivo 😈 Posso mandar o link?",
    "Minha puta esposa abriu a live agora. Quer ver?",
    "Tem live dela rolando agora 🔥 Quer que eu mande o link?",
)

LIVE_REMARKETING_VARIANTS = (
    "Quer gozar pra ela ao vivo na live? Posso mandar o link?",
    "Ela ainda tá ao vivo 😈 Vai querer o link?",
    "Ainda dá tempo de assistir minha safada ao vivo. Quer entrar?",
    "Minha esposa ainda está na live 🔥 Posso te mandar o acesso?",
    "Vai perder ela ao vivo? Responde “manda” que eu envio o link",
)

CAMPAIGNS = {
    "pv.greeting": {
        "order": 10,
        "module": "pv_reply",
        "text": PV_GREETING_VARIANTS[0],
    },
    "pv.link_invite": {
        "order": 20,
        "module": "pv_reply",
        "text": "Entra no grupo de prévias dela! Posso mandar o link",
    },
    "pv.preview_link": {
        "order": 30,
        "module": "pv_reply",
        "text": "{preview_link}",
    },
    "pv.followup": {
        "order": 40,
        "module": "pv_reply",
        "text": "Gostou? Já gozou pra ela??",
    },
    "pv.weekly_question": {
        "order": 50,
        "module": "pv_reply",
        "text": "E aí, safado! Tá gozando muito?",
    },
    "pv.weekly_reentry": {
        "order": 60,
        "module": "pv_reply",
        "text": (
            "Se ainda não entrou ou se saiu, entra de novo. "
            "Abre em duas telas e goza pra ela ver 😈"
        ),
    },
    "pv.live_optin": {
        "order": 70,
        "module": "pv_reply",
        "text": "Quer que eu te avise quando ela entrar ao vivo?",
    },
    "pv.two_screens.prompt": {
        "order": 80,
        "module": "pv_reply",
        "text": "Faz duas telas?",
    },
    "pv.two_screens.question": {
        "order": 90,
        "module": "pv_reply",
        "text": "Prefere fazer nos peitos, na buceta ou no cu?",
    },
    "pv.two_screens.limit": {
        "order": 100,
        "module": "pv_reply",
        "text": "Só pode escolher um",
    },
    "pv.two_screens.followup": {
        "order": 110,
        "module": "pv_reply",
        "text": "Se ficar bom, depois eu mando outro",
    },
    "pv.two_screens.retry": {
        "order": 120,
        "module": "pv_reply",
        "text": "Escolhe uma: peitos, buceta ou cu",
    },
}

# Order matters: opt-out must win over a generic negative answer.
PV_RESPONSE_RULES = {
    "opt_out": {
        "order": 10,
        "exact": ("pare", "parar", "stop"),
        "contains": (
            "não quero",
            "nao quero",
            "não envie",
            "nao envie",
            "não me mande",
            "nao me mande",
        ),
    },
    "negative": {
        "order": 20,
        "exact": ("não", "nao", "ainda não", "ainda nao"),
        "contains": (
            "não entrei",
            "nao entrei",
            "não consegui entrar",
            "nao consegui entrar",
            "manda o link",
            "mande o link",
            "qual o link",
            "cadê o link",
            "cade o link",
        ),
    },
    "positive": {
        "order": 30,
        "exact": ("sim", "entrei", "gostei"),
        "contains": (
            "já entrei",
            "ja entrei",
            "já estou",
            "ja estou",
            "estou no canal",
            "tô no canal",
            "to no canal",
        ),
    },
}

MODULES = {
    "radar": {
        "order": 10,
        "dispatch_order": 30,
        "label": "Radar",
        "description": "Inventário passivo, regras, permissões, bots, links e origem provável de PV.",
        "default_enabled": False,
        "active_writes": False,
    },
    "pv_reply": {
        "order": 20,
        "dispatch_order": 20,
        "label": "Atendimento PV",
        "description": "Recepção em duas etapas e lembretes progressivos no privado.",
        "initial_reason": "Desligado por padrão; requer ativação consciente",
        "default_enabled": False,
        "active_writes": True,
    },
    "botson": {
        "order": 30,
        "dispatch_order": 10,
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
    "pv_reply.enable": {
        "order": 60,
        "module": "pv_reply",
        "surfaces": ("panel",),
        "triggers": ("ligar atendimento", "/ligar_atendimento"),
        "help": "ligar atendimento automático no PV",
    },
    "pv_reply.disable": {
        "order": 70,
        "module": "pv_reply",
        "surfaces": ("panel",),
        "triggers": ("desligar atendimento", "/desligar_atendimento"),
        "help": "desligar atendimento automático no PV",
    },
    "pv_reply.preview": {
        "order": 80,
        "module": "pv_reply",
        "surfaces": ("panel",),
        "triggers": ("ver mensagens pv", "/mensagens_pv"),
        "help": "conferir textos e intervalos do atendimento",
    },
    "botson.enable": {
        "order": 90,
        "module": "botson",
        "surfaces": ("panel",),
        "triggers": ("ligar botson",),
        "help": "habilitar o testador",
    },
    "botson.disable": {
        "order": 100,
        "module": "botson",
        "surfaces": ("panel",),
        "triggers": ("desligar botson",),
        "help": "desabilitar o testador",
    },
    "botson.run": {
        "order": 110,
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
    "campaign_order": tuple(CAMPAIGNS),
    "pv_response_order": tuple(PV_RESPONSE_RULES),
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
    campaign_orders = [spec["order"] for spec in CAMPAIGNS.values()]
    if len(campaign_orders) != len(set(campaign_orders)):
        raise ValueError("ordem duplicada no catálogo de campanhas")
    response_orders = [spec["order"] for spec in PV_RESPONSE_RULES.values()]
    if len(response_orders) != len(set(response_orders)):
        raise ValueError("ordem duplicada nas respostas do Atendimento PV")
    module_orders = [spec["order"] for spec in MODULES.values()]
    dispatch_orders = [spec["dispatch_order"] for spec in MODULES.values()]
    if len(module_orders) != len(set(module_orders)):
        raise ValueError("ordem duplicada no catálogo de módulos")
    if len(dispatch_orders) != len(set(dispatch_orders)):
        raise ValueError("ordem de despacho duplicada no catálogo de módulos")


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
