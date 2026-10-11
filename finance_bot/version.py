"""Build version details shown by the Telegram bot."""

from __future__ import annotations

import os


def version_text() -> str:
    version = os.getenv("APP_VERSION", "development")
    commit = os.getenv("GIT_COMMIT", "local")
    if commit not in {"", "local", "unknown"}:
        commit = commit[:7]
    elif not commit:
        commit = "unknown"
    return f"Версия: {version}\nКоммит: {commit}"
