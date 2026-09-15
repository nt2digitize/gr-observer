"""Deterministic intent classifier for group-reply shadow validation.

This module is deliberately side-effect free.  It does not read Telegram, write
PostgreSQL, enqueue Outbox actions, or decide production sends by itself.  The
current production trigger remains authoritative until a later, explicit
promotion PR.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class GroupIntentDecision:
    """One explainable classification result.

    ``confidence`` is an operator-facing tier, not a statistical probability.
    ``should_reply`` expresses what the *candidate* policy would do if promoted;
    production code does not consume this module yet.
    """

    intent: str
    confidence: str
    should_reply: bool
    rule: str


def _plain(value: str) -> str:
    """Normalize accents, punctuation and whitespace for stable matching."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = without_marks.casefold()
    lowered = re.sub(r"[^a-z0-9@]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


# Explicit negative intent wins before any positive rule.  This prevents phrases
# such as "não quero ver sua esposa" from matching the positive fragment inside.
_NEGATIVE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "negative.do_not_send",
        re.compile(r"\b(?:nao|nunca)\s+(?:manda|mande|envia|envie|mostra|mostre)\b"),
    ),
    (
        "negative.do_not_want",
        re.compile(r"\b(?:nao|nunca)\s+(?:quero|queremos|quero ver|quero conhecer)\b"),
    ),
    (
        "negative.stop",
        re.compile(r"\b(?:para|pare|parar)\s+(?:de\s+)?(?:mandar|enviar|mostrar|responder)\b"),
    ),
)


# Strong intent is self-contained: the same message identifies the subject and a
# request/interest.  These rules are intentionally narrower than a bag of words.
_STRONG_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "strong.want_partner",
        re.compile(
            r"\b(?:quem\s+quer|quero|quer|querem|queria|gostaria\s+de)\s+"
            r"(?:ver|conhecer)\s+(?:(?:a|uma|sua|minha)\s+)?"
            r"(?:esposa|mulher|parceira)\b"
        ),
    ),
    (
        "strong.show_partner",
        re.compile(
            r"\b(?:mostra|mostre|mostrar|manda|mande|mandar|envia|envie)\s+"
            r"(?:(?:a|uma|sua|minha)\s+)?(?:esposa|mulher|parceira)\b"
        ),
    ),
    (
        "strong.partner_media",
        re.compile(
            r"\b(?:tem|manda|mande|mostra|mostre|quero|quer)\s+"
            r"(?:foto|fotos|video|videos)\s+"
            r"(?:(?:da|de|da\s+sua|de\s+sua|da\s+minha|de\s+minha)\s+)?"
            r"(?:esposa|mulher|parceira)\b"
        ),
    ),
    (
        "strong.partner_here",
        re.compile(r"\btem\s+(?:esposa|mulher|parceira)\s+(?:ai|aqui)\b"),
    ),
    (
        "strong.partner_where",
        re.compile(r"\bcade\s+(?:(?:a|sua|minha)\s+)?(?:esposa|mulher|parceira)\b"),
    ),
    (
        "strong.partner_private",
        re.compile(r"\b(?:esposa|mulher|parceira)\s+(?:no|na)\s+(?:pv|privado)\b"),
    ),
)


# Contextual intent is meaningful, but does not name the subject strongly enough.
# It may only become actionable when Telegram supplies objective reply context.
_CONTEXT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("context.want_see", re.compile(r"^(?:eu\s+)?quero\s+ver$")),
    ("context.want_know", re.compile(r"^(?:eu\s+)?quero\s+conhecer$")),
    ("context.send_photo", re.compile(r"^(?:me\s+)?manda\s+(?:a\s+)?foto$")),
    ("context.send_video", re.compile(r"^(?:me\s+)?manda\s+(?:o\s+)?video$")),
    ("context.show_her", re.compile(r"^(?:me\s+)?mostra\s+ela$")),
    ("context.where_her", re.compile(r"^cade\s+ela$")),
    ("context.yours", re.compile(r"^(?:e\s+)?a\s+sua$")),
)


# Weak intent is intentionally tiny and ambiguous.  It requires a direct reply to
# a message managed by this application, not merely generic conversational context.
_WEAK_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("weak.send", re.compile(r"^(?:manda|mande|envia|envie|manda ai)$")),
    ("weak.want", re.compile(r"^(?:quero|eu quero)$")),
    ("weak.yes", re.compile(r"^(?:sim|pode|bora)$")),
    ("weak.where", re.compile(r"^(?:cade|e ai)$")),
    ("weak.show", re.compile(r"^(?:mostra|mostre)$")),
)


def classify_group_intent(
    text: str,
    *,
    reply_to_managed: bool = False,
    reply_to_relevant: bool = False,
) -> GroupIntentDecision:
    """Classify one group message without any external side effect.

    Policy candidate:
    - strong: can stand alone;
    - contextual: requires objective relevant reply context;
    - weak: requires a direct reply to a message managed by GR-OBSERVER;
    - none/negative: never replies.

    ``reply_to_relevant`` is deliberately an explicit input.  A future integration
    may derive it from a narrow, auditable Telegram relation; this classifier does
    not infer broad conversation history on its own.
    """
    normalized = _plain(text)
    if not normalized:
        return GroupIntentDecision("none", "none", False, "empty")

    for rule, pattern in _NEGATIVE_RULES:
        if pattern.search(normalized):
            return GroupIntentDecision("negative", "negative", False, rule)

    for rule, pattern in _STRONG_RULES:
        if pattern.search(normalized):
            return GroupIntentDecision("partner_request", "strong", True, rule)

    for rule, pattern in _CONTEXT_RULES:
        if pattern.fullmatch(normalized):
            context_ok = bool(reply_to_managed or reply_to_relevant)
            return GroupIntentDecision(
                "partner_request" if context_ok else "ambiguous",
                "contextual",
                context_ok,
                rule,
            )

    for rule, pattern in _WEAK_RULES:
        if pattern.fullmatch(normalized):
            return GroupIntentDecision(
                "partner_request" if reply_to_managed else "ambiguous",
                "weak",
                bool(reply_to_managed),
                rule,
            )

    return GroupIntentDecision("none", "none", False, "no_match")
