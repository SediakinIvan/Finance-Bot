"""Read-only workbook compatibility check before replacing the running bot."""
from pathlib import Path
from tempfile import NamedTemporaryFile

from finance_bot.config import Settings
from finance_bot.v2_sheets import V2GoogleSheetsStore


def main():
    settings = Settings.from_environment()
    directory = Path(settings.category_change_state_dir)
    try:
        with NamedTemporaryFile(dir=directory, prefix=".release-check-"):
            pass
        for user_id in settings.authorized_telegram_user_ids:
            store = V2GoogleSheetsStore(
                credentials_file=str(settings.google_credentials_file),
                spreadsheet_id=settings.spreadsheet_id_for_user(user_id),
                transactions_sheet=settings.transactions_sheet,
                categories_sheet=settings.categories_sheet,
                analytics_sheet=settings.analytics_sheet,
                category_change_state_dir=directory,
            )
            store.category_catalog()
    except Exception:
        raise SystemExit("Release blocked: check schema v2 readiness, Google access and journal permissions. Running bot was not replaced.") from None
    print("Release check passed: all configured workbooks are v2/ready; journal is writable.")


if __name__ == "__main__":
    main()
