"""Deterministic human-like reading and typing timing for automated user-facing writes."""

from __future__ import annotations

import hashlib
import math
import re

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
MIN_WRITING_DELAY_SECONDS = 3
MAX_INLINE_TYPING_SECONDS = 8.0


def _fraction(key: str, salt: str) -> float:
    raw = hashlib.sha256(f"{salt}:{key}".encode("utf-8")).digest()[:8]
    return int.from_bytes(raw, "big") / ((1 << 64) - 1)


def visible_text_for_timing(text: str) -> str:
    """Keep wording, but collapse URLs so long links do not fake long typing."""
    value = " ".join((text or "").strip().split())
    return _URL_RE.sub(" link ", value).strip()


def typing_seconds(text: str, key: str, *, cap: float = MAX_INLINE_TYPING_SECONDS) -> float:
    """Human typing window with retry-stable speed variation.

    Typing speed stays roughly between 3.5 and 4.25 visible characters/second.
    The inline phase is capped so the single Writer is never held for a long wait;
    any remaining human wait belongs in durable Outbox scheduling.
    """
    visible = len(visible_text_for_timing(text))
    if visible <= 0:
        return 0.0
    cps = 3.5 + (0.75 * _fraction(key, "typing-speed"))
    return round(max(0.8, min(float(cap), visible / cps)), 2)


def reading_seconds(text: str, key: str) -> float:
    """Short pre-typing comprehension/read interval proportional to message size."""
    value = visible_text_for_timing(text)
    if not value:
        return 0.0
    words = max(1, len(value.split()))
    # Around 210-260 wpm, with a small retry-stable cognitive pause.
    wpm = 210.0 + (50.0 * _fraction(key, "reading-speed"))
    seconds = (words / wpm) * 60.0
    pause = 0.7 + (0.8 * _fraction(key, "reading-pause"))
    return round(min(6.0, max(1.0, seconds + pause)), 2)


def minimum_human_delay_seconds(text: str, key: str) -> int:
    """Minimum total delay needed for read/think + visible typing; never zero."""
    total = reading_seconds(text, key) + typing_seconds(text, key)
    return max(MIN_WRITING_DELAY_SECONDS, int(math.ceil(total)))


def split_human_delay(total_delay: int, text: str, key: str) -> tuple[float, float]:
    """Split a durable total interval into pre-wait and final typing window."""
    minimum = minimum_human_delay_seconds(text, key)
    total = max(int(total_delay), minimum)
    typing = min(float(total), typing_seconds(text, key))
    prewait = max(0.0, float(total) - typing)
    return prewait, typing
