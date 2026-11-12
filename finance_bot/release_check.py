"""Read-only workbook compatibility check before replacing the running bot."""
from pathlib import Path
from tempfile import NamedTemporaryFile

from finance_bot.config import Settings
from finance_bot.category_schema import SchemaError
from finance_bot.v2_sheets import V2GoogleSheetsStore


def failure_reason(error):
    """Only controlled schema messages and exception types, never API bodies."""
    chain = []
    current = error
    while current is not None and id(current) not in {id(item) for item in chain}:
        chain.append(current)
        current = current.__cause__ or current.__context__
    for item in chain:
        if isinstance(item, SchemaError):
            return str(item)
    for item in chain:
        kind = type(item).__name__
        if kind == "WorksheetNotFound":
            return "Missing v2 service worksheet: configured workbook has not completed migration."
        if kind in {"SpreadsheetNotFound", "PermissionError", "APIError"}:
            status = getattr(getattr(item, "response", None), "status_code", None)
            return f"Google workbook access failed ({kind}, HTTP {status})."
    return "Failure type: " + type(chain[-1]).__name__


def main():
    stage = "configuration"
    try:
        settings = Settings.from_environment()
        directory = Path(settings.category_change_state_dir)
        stage = "persistent journal permissions"
        with NamedTemporaryFile(dir=directory, prefix=".release-check-"):
            pass
        for number, user_id in enumerate(sorted(settings.authorized_telegram_user_ids), 1):
            role = "primary" if user_id == settings.allowed_telegram_user_id else "additional"
            stage = f"{role} workbook {number} ({settings.spreadsheet_id_for_user(user_id)})"
            store = V2GoogleSheetsStore(
                credentials_file=str(settings.google_credentials_file),
                spreadsheet_id=settings.spreadsheet_id_for_user(user_id),
                transactions_sheet=settings.transactions_sheet,
                categories_sheet=settings.categories_sheet,
                analytics_sheet=settings.analytics_sheet,
                category_change_state_dir=directory,
            )
            pending = store.category_change_status(user_id)
            if pending is not None:
                stage = f"{role} workbook {number}: recovery journal compatibility"
                if store.category_recovery_ready(user_id) is not True:
                    raise SchemaError("Pending request requires operator recovery before release.")
            else:
                store.category_catalog()
                store.load_taxonomy()
    except Exception as exc:
        reason = failure_reason(exc).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Release compatibility check::{stage}: {reason}", flush=True)
        raise SystemExit("Release blocked before container replacement.") from None
    print("Release check passed: all configured workbooks are v2/ready or have a supported recovery journal; journal is writable.")


if __name__ == "__main__":
    main()
