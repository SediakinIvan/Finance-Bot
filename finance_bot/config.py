"""Environment-backed configuration for local and VPS deployments."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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
    analytics_sheet: str = "Аналитика"
    timezone: str = "Europe/Moscow"
    additional_user_sheets: dict[int, str] = field(default_factory=dict)

    @property
    def authorized_telegram_user_ids(self) -> frozenset[int]:
        return frozenset(
            {self.allowed_telegram_user_id, *self.additional_user_sheets}
        )

    def spreadsheet_id_for_user(self, user_id: int) -> str:
        if user_id == self.allowed_telegram_user_id:
            return self.spreadsheet_id
        try:
            return self.additional_user_sheets[user_id]
        except KeyError as exc:
            raise ConfigurationError(
                f"No spreadsheet is configured for Telegram user {user_id}."
            ) from exc

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

        additional_user_sheets: dict[int, str] = {}
        configured_user_sheets = os.getenv("TELEGRAM_USER_SHEETS", "").strip()
        for entry in filter(None, (part.strip() for part in configured_user_sheets.split(","))):
            user_id_text, separator, spreadsheet_id = entry.partition(":")
            if not separator or not user_id_text.isdigit() or not spreadsheet_id.strip():
                raise ConfigurationError(
                    "TELEGRAM_USER_SHEETS entries must use the format "
                    "TELEGRAM_ID:SPREADSHEET_ID, separated by commas."
                )
            user_id = int(user_id_text)
            if user_id <= 0:
                raise ConfigurationError(
                    "Telegram user IDs in TELEGRAM_USER_SHEETS must be positive integers."
                )
            if user_id == allowed_user_id or user_id in additional_user_sheets:
                raise ConfigurationError(
                    f"Telegram user ID {user_id} is configured more than once."
                )
            spreadsheet_id = spreadsheet_id.strip()
            if spreadsheet_id == required["GOOGLE_SHEET_ID"] or (
                spreadsheet_id in additional_user_sheets.values()
            ):
                raise ConfigurationError(
                    "A spreadsheet ID cannot be assigned to multiple Telegram users."
                )
            additional_user_sheets[user_id] = spreadsheet_id

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
            analytics_sheet=os.getenv("GOOGLE_ANALYTICS_SHEET", "Аналитика").strip(),
            timezone=os.getenv("BOT_TIMEZONE", "Europe/Moscow").strip(),
            additional_user_sheets=additional_user_sheets,
        )
