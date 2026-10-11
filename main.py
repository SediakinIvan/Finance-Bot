"""Run the Telegram PnL entry bot with local polling."""

from __future__ import annotations

import logging

from dotenv import load_dotenv
from telegram.ext import Application

from finance_bot.bot import register_handlers
from finance_bot.config import ConfigurationError, Settings
from finance_bot.sheets import GoogleSheetsStore


def main() -> None:
	load_dotenv()
	logging.basicConfig(
		level=logging.INFO,
		format="%(asctime)s %(levelname)s %(name)s: %(message)s",
	)
	try:
		settings = Settings.from_environment()
	except ConfigurationError as exc:
		raise SystemExit(f"Configuration error: {exc}") from exc

	store = GoogleSheetsStore(
		credentials_file=str(settings.google_credentials_file),
		spreadsheet_id=settings.spreadsheet_id,
		transactions_sheet=settings.transactions_sheet,
		categories_sheet=settings.categories_sheet,
	)
	application = Application.builder().token(settings.telegram_token).build()
	application.bot_data["settings"] = settings
	application.bot_data["sheets_store"] = store
	register_handlers(application, settings)
	logging.getLogger(__name__).info("Starting Telegram bot polling")
	application.run_polling(allowed_updates=None)


if __name__ == "__main__":
	main()
