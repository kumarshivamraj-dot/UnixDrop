from __future__ import annotations


HEALTH_CHECK_CLIPBOARD_PREFIX = "deskbridge-health-"
HEALTH_CHECK_CLIPBOARD_SOURCE = "health-check"


def is_health_check_clipboard_text(text: str) -> bool:
    return text.startswith(HEALTH_CHECK_CLIPBOARD_PREFIX)
