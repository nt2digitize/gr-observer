"""Pure offline simulation of a lightweight PV response memory.

This module is intentionally NOT wired into Observer, PvReplyModule, schema,
Outbox or Telegram.  It exists only to test the smallest useful design before
any production integration is proposed.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import normalize_text


# Small, explicit retrieval-intent catalog.  Order is data: the first match wins.
INTENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "price",
        (
            "quanto custa",
            "qual o valor",
            "quanto e",
            "quanto é",
            "quanto ta",
            "quanto tá",
            "qnt custa",
            "qnt ta",
            "qnt tá",
            "preco",
            "preço",
            "valor do vip",
            "valor vip",
        ),
    ),
    (
        "payment",
        ("pix", "pagamento", "pagar", "cartao", "cartão"),
    ),
    (
        "access",
        ("como entra", "como entrar", "como funciona", "manda o link", "link"),
    ),
    (
        "live",
        ("ao vivo", "live"),
    ),
    (
        "preview",
        ("previa", "prévia", "amostra"),
    ),
    (
        "new_content",
        ("conteudo novo", "conteúdo novo", "tem algo novo", "novidade"),
    ),
)

# Demonstration threshold only.  A production policy must be approved separately.
SIMULATION_RELIABLE_AFTER = 3


def classify_intent(text: str) -> str:
    """Reduce transient PV text to a small non-sensitive intent label."""
    normalized = normalize_text(text)
    for intent, markers in INTENT_RULES:
        if any(marker in normalized for marker in markers):
            return intent
    return "unknown"


@dataclass
class MemoryEntry:
    intent: str
    response_text: str
    normalized_response: str
    times_seen: int = 1

    @property
    def status(self) -> str:
        if self.times_seen >= SIMULATION_RELIABLE_AFTER:
            return "reliable"
        if self.times_seen >= 2:
            return "recurring"
        return "new"


class PvResponseMemorySimulator:
    """In-memory proof of concept; performs no I/O and no Telegram action."""

    def __init__(self) -> None:
        self.pending_intent: dict[int, str] = {}
        self.entries: dict[tuple[str, str], MemoryEntry] = {}

    def observe_inbound(self, user_id: int, text: str) -> str:
        """Classify a lead message without retaining its original text."""
        intent = classify_intent(text)
        self.pending_intent[int(user_id)] = intent
        return intent

    def observe_outbound(
        self,
        user_id: int,
        text: str,
        *,
        origin: str,
    ) -> MemoryEntry | None:
        """Learn only a human response attached to a known current intent.

        ``origin`` is explicit in the simulator.  A future integration would
        derive automation provenance from the existing Outbox/effect journal.
        """
        if origin != "human":
            return None

        intent = self.pending_intent.get(int(user_id), "unknown")
        normalized_response = normalize_text(text)
        if intent == "unknown" or not normalized_response:
            return None

        key = (intent, normalized_response)
        entry = self.entries.get(key)
        if entry is None:
            entry = MemoryEntry(
                intent=intent,
                response_text=text.strip(),
                normalized_response=normalized_response,
            )
            self.entries[key] = entry
        else:
            entry.times_seen += 1
        return entry

    def responses_for(self, intent: str) -> list[MemoryEntry]:
        """Return learned answers strongest-first for one intent."""
        found = [entry for entry in self.entries.values() if entry.intent == intent]
        return sorted(
            found,
            key=lambda entry: (-entry.times_seen, entry.normalized_response),
        )
