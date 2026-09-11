"""Environment configuration for the composition root."""

from __future__ import annotations

import os
from dataclasses import dataclass

REQUIRED_PANEL_ENV = (
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "CONTROL_BOT_TOKEN",
    "ADMIN_USER_ID",
    "DATABASE_URL",
)


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.startswith("__SET_IN_RAILWAY_UI__"):
        raise RuntimeError(f"Variável obrigatória ausente: {name}")
    return value


def csv_env(name: str) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in os.getenv(name, "").split(",") if item.strip()
    )


def env_bool(name: str, default: bool = False) -> bool:
    fallback = "1" if default else "0"
    return os.getenv(name, fallback).strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
        "sim",
    }


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    control_bot_token: str
    admin_id: int
    database_url: str
    user_session_string: str
    history_limit: int
    scan_interval_minutes: int
    pv_preview_link: str
    pv_reply_auto_enable: bool
    pv_reply_delay_seconds: int
    pv_followup_min_hours: float
    pv_followup_max_hours: float
    pv_followup_max_cycles: int
    pv_weekly_interval_hours: float
    botson_previews: tuple[str, ...]
    botson_bot_targets: tuple[str, ...]
    botson_controller_id: int | None
    botson_pair_code: str | None
    botson_trigger_text: str
    botson_max_clicks: int
    botson_max_depth: int
    botson_entry_wait_seconds: float
    botson_recovery_wait_seconds: float
    botson_report_ttl_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        controller = os.getenv("BOTSON_CONTROLLER_ID", "").strip()
        followup_min = max(1.0, float(os.getenv("PV_FOLLOWUP_MIN_HOURS", "23")))
        followup_max = max(1.0, float(os.getenv("PV_FOLLOWUP_MAX_HOURS", "25")))
        return cls(
            api_id=int(required("TELEGRAM_API_ID")),
            api_hash=required("TELEGRAM_API_HASH"),
            control_bot_token=required("CONTROL_BOT_TOKEN"),
            admin_id=int(required("ADMIN_USER_ID")),
            database_url=required("DATABASE_URL").replace(
                "postgres://", "postgresql://", 1
            ),
            user_session_string=os.getenv("USER_SESSION_STRING", "").strip(),
            history_limit=max(0, min(50, int(os.getenv("HISTORY_LIMIT", "20")))),
            scan_interval_minutes=max(
                1, int(os.getenv("SCAN_INTERVAL_MINUTES", "60"))
            ),
            pv_preview_link=os.getenv("PV_PREVIEW_LINK", "").strip(),
            pv_reply_auto_enable=env_bool("PV_REPLY_AUTO_ENABLE", False),
            pv_reply_delay_seconds=max(
                0, int(os.getenv("PV_REPLY_DELAY_SECONDS", "60"))
            ),
            pv_followup_min_hours=min(followup_min, followup_max),
            pv_followup_max_hours=max(followup_min, followup_max),
            pv_followup_max_cycles=max(
                0, min(7, int(os.getenv("PV_FOLLOWUP_MAX_CYCLES", "7")))
            ),
            pv_weekly_interval_hours=max(
                24.0, float(os.getenv("PV_WEEKLY_INTERVAL_HOURS", "168"))
            ),
            botson_previews=csv_env("BOTSON_PREVIEW_ALLOWLIST"),
            botson_bot_targets=csv_env("BOTSON_BOT_TARGETS"),
            botson_controller_id=int(controller)
            if controller.lstrip("-").isdigit()
            else None,
            botson_pair_code=os.getenv("BOTSON_PAIR_CODE", "").strip() or None,
            botson_trigger_text=os.getenv("BOTSON_TRIGGER_TEXT", "testar")
            .strip()
            .casefold(),
            botson_max_clicks=max(1, int(os.getenv("BOTSON_MAX_CLICKS", "12"))),
            botson_max_depth=max(0, int(os.getenv("BOTSON_MAX_DEPTH", "5"))),
            botson_entry_wait_seconds=max(
                1.0, float(os.getenv("BOTSON_ENTRY_WAIT_SECONDS", "12"))
            ),
            botson_recovery_wait_seconds=max(
                1.0, float(os.getenv("BOTSON_RECOVERY_WAIT_SECONDS", "20"))
            ),
            botson_report_ttl_seconds=max(
                60, int(os.getenv("BOTSON_REPORT_TTL_SECONDS", "21600"))
            ),
        )

    def module_blocker(self, module_id: str) -> str | None:
        if not self.user_session_string:
            return "Falta configurar USER_SESSION_STRING no Railway"
        if module_id == "pv_reply" and not self.pv_preview_link:
            return "Falta configurar PV_PREVIEW_LINK no Railway"
        if module_id == "botson":
            if not self.botson_previews:
                return "Falta configurar BOTSON_PREVIEW_ALLOWLIST"
            if self.botson_controller_id is None and not self.botson_pair_code:
                return "Configure BOTSON_CONTROLLER_ID ou BOTSON_PAIR_CODE"
        return None


def missing_panel_env() -> list[str]:
    return [name for name in REQUIRED_PANEL_ENV if not os.getenv(name, "").strip()]
