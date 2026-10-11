"""Environment-backed configuration for local and VPS deployments."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when required environment configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_telegram_user_id: int
    google_credentials_file: Path
    spreadsheet_id: str
    transactions_sheet: str = "Траты"
    categories_sheet: str = "Категории"
    timezone: str = "Europe/Moscow"

    @classmethod
    def from_environment(cls) -> "Settings":
        required = {
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            "TELEGRAM_ALLOWED_USER_ID": os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip(),
            "GOOGLE_CREDENTIALS_FILE": os.getenv("GOOGLE_CREDENTIALS_FILE", "").strip(),
            "GOOGLE_SHEET_ID": os.getenv("GOOGLE_SHEET_ID", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigurationError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )

        try:
            allowed_user_id = int(required["TELEGRAM_ALLOWED_USER_ID"])
        except ValueError as exc:
            raise ConfigurationError("TELEGRAM_ALLOWED_USER_ID must be an integer.") from exc
        if allowed_user_id <= 0:
            raise ConfigurationError("TELEGRAM_ALLOWED_USER_ID must be a positive integer.")

        credentials_file = Path(required["GOOGLE_CREDENTIALS_FILE"]).expanduser()
        if not credentials_file.is_file():
            raise ConfigurationError(
                f"Google credentials file does not exist: {credentials_file}"
            )

        return cls(
            telegram_token=required["TELEGRAM_BOT_TOKEN"],
            allowed_telegram_user_id=allowed_user_id,
            google_credentials_file=credentials_file,
            spreadsheet_id=required["GOOGLE_SHEET_ID"],
            transactions_sheet=os.getenv("GOOGLE_TRANSACTIONS_SHEET", "Траты").strip(),
            categories_sheet=os.getenv("GOOGLE_CATEGORIES_SHEET", "Категории").strip(),
            timezone=os.getenv("BOT_TIMEZONE", "Europe/Moscow").strip(),
        )
