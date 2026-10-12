"""Read category choices and append transactions to the designated Sheets tabs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from finance_bot.analytics import (
    AnalyticsError,
    CategoryAnalytics,
    MonthlyAnalytics,
    parse_category_analytics,
    parse_monthly_analytics,
)


class SheetsError(RuntimeError):
    """A Google Sheets read or write operation failed."""


@dataclass(frozen=True)
class CategoryOption:
    transaction_type: str
    category: str
    subcategory: str


_HEADER_ALIASES = {
    "type": {"тип", "доход/расход", "тип операции", "transaction type", "type"},
    "category": {"категория", "category"},
    "subcategory": {"подкатегория", "subcategory", "под категория"},
}


def _normalize_header(value: Any) -> str:
    return " ".join(str(value).strip().casefold().split())


def parse_taxonomy_rows(rows: Sequence[Sequence[Any]]) -> list[CategoryOption]:
    """Parse a row-per-option Categories sheet with Тип/Категория/Подкатегория headers."""
    if not rows:
        raise SheetsError("The Категории sheet is empty.")

    header = [_normalize_header(value) for value in rows[0]]
    indexes: dict[str, int] = {}
    for field, aliases in _HEADER_ALIASES.items():
        for index, value in enumerate(header):
            if value in aliases:
                indexes[field] = index
                break
    missing_fields = {"type", "category", "subcategory"} - indexes.keys()
    if missing_fields:
        raise SheetsError(
            "The Категории sheet must have headers Тип, Категория, Подкатегория."
        )

    options: list[CategoryOption] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows[1:]:
        values = list(row)
        if not any(str(value).strip() for value in values):
            continue

        def cell(field: str) -> str:
            index = indexes[field]
            return str(values[index]).strip() if index < len(values) else ""

        raw_type = cell("type").casefold()
        if raw_type in {"расход", "expense", "spending"}:
            transaction_type = "Расход"
            category = cell("category")
        elif raw_type in {"доход", "income"}:
            transaction_type = "Доход"
            category = "Доход"
        else:
            continue

        subcategory = cell("subcategory")
        if not category or not subcategory:
            continue
        key = (transaction_type, category, subcategory)
        if key not in seen:
            seen.add(key)
            options.append(CategoryOption(*key))

    if not options:
        raise SheetsError("The Категории sheet has no usable category rows.")
    return options


class GoogleSheetsStore:
    """Minimal Sheets adapter for category reads and Траты transactions."""

    def __init__(
        self,
        credentials_file: str,
        spreadsheet_id: str,
        transactions_sheet: str = "Траты",
        categories_sheet: str = "Категории",
        analytics_sheet: str = "Аналитика",
    ) -> None:
        self.credentials_file = credentials_file
        self.spreadsheet_id = spreadsheet_id
        self.transactions_sheet = transactions_sheet
        self.categories_sheet = categories_sheet
        self.analytics_sheet = analytics_sheet
        self._spreadsheet: Any | None = None

    def _get_spreadsheet(self) -> Any:
        if self._spreadsheet is None:
            try:
                import gspread
            except ImportError as exc:
                raise SheetsError(
                    "Google Sheets dependencies are missing. Install requirements.txt."
                ) from exc
            try:
                client = gspread.service_account(filename=self.credentials_file)
                self._spreadsheet = client.open_by_key(self.spreadsheet_id)
            except Exception as exc:
                raise SheetsError(
                    "Could not open the configured Google spreadsheet. Check its ID, "
                    "credentials, and sharing permissions."
                ) from exc
        return self._spreadsheet

    def load_taxonomy(self) -> list[CategoryOption]:
        try:
            worksheet = self._get_spreadsheet().worksheet(self.categories_sheet)
            return parse_taxonomy_rows(worksheet.get_all_values())
        except SheetsError:
            raise
        except Exception as exc:
            raise SheetsError(
                f"Could not read the '{self.categories_sheet}' sheet."
            ) from exc

    def append_transaction(self, row: Sequence[str | int]) -> None:
        if len(row) != 6:
            raise ValueError("A transaction row must contain exactly six values.")
        try:
            worksheet = self._get_spreadsheet().worksheet(self.transactions_sheet)
            first_data_row = 2
            if worksheet.row_count >= first_data_row:
                dates = worksheet.get(f"A{first_data_row}:A{worksheet.row_count}")
                available_rows = worksheet.row_count - first_data_row + 1
                empty_offset = next(
                    (
                        offset
                        for offset, values in enumerate(dates)
                        if not values or not str(values[0]).strip()
                    ),
                    None,
                )
                if empty_offset is None and len(dates) < available_rows:
                    empty_offset = len(dates)
                if empty_offset is None:
                    worksheet.add_rows(1)
                    target_row = worksheet.row_count
                else:
                    target_row = first_data_row + empty_offset
            else:
                worksheet.add_rows(1)
                target_row = first_data_row

            worksheet.update(
                values=[list(row)],
                range_name=f"A{target_row}:F{target_row}",
                value_input_option="RAW",
            )
        except Exception as exc:
            raise SheetsError(
                f"Could not write to the '{self.transactions_sheet}' sheet. "
                "Check the row in Google Sheets before trying again."
            ) from exc

    def get_recent_transactions(self, limit: int = 10) -> list[list[str]]:
        if limit <= 0:
            raise ValueError("The transaction limit must be positive.")
        try:
            worksheet = self._get_spreadsheet().worksheet(self.transactions_sheet)
            if worksheet.row_count < 2:
                return []
            rows = worksheet.get(f"A2:F{worksheet.row_count}")
            transactions: list[list[str]] = []
            for values in rows:
                if not values or not str(values[0]).strip():
                    continue
                transaction = [str(value) for value in values[:6]]
                transaction.extend([""] * (6 - len(transaction)))
                transactions.append(transaction)
            return transactions[-limit:]
        except Exception as exc:
            raise SheetsError(
                f"Could not read recent rows from the '{self.transactions_sheet}' sheet."
            ) from exc

    def get_monthly_analytics(self) -> MonthlyAnalytics:
        try:
            worksheet = self._get_spreadsheet().worksheet(self.analytics_sheet)
            return parse_monthly_analytics(worksheet.get_all_values())
        except AnalyticsError as exc:
            raise SheetsError(str(exc)) from exc
        except Exception as exc:
            raise SheetsError(
                f"Could not read the '{self.analytics_sheet}' sheet."
            ) from exc

    def get_category_analytics(self, category: str) -> CategoryAnalytics:
        try:
            worksheet = self._get_spreadsheet().worksheet(self.analytics_sheet)
            return parse_category_analytics(worksheet.get_all_values(), category)
        except AnalyticsError as exc:
            raise SheetsError(str(exc)) from exc
        except Exception as exc:
            raise SheetsError(
                f"Could not read the '{self.analytics_sheet}' sheet."
            ) from exc
