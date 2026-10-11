"""Read category choices and append transactions to the designated Sheets tabs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


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
    """Minimal Sheets adapter: category reads and append-only Траты writes."""

    def __init__(
        self,
        credentials_file: str,
        spreadsheet_id: str,
        transactions_sheet: str = "Траты",
        categories_sheet: str = "Категории",
    ) -> None:
        self.credentials_file = credentials_file
        self.spreadsheet_id = spreadsheet_id
        self.transactions_sheet = transactions_sheet
        self.categories_sheet = categories_sheet
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
            worksheet.append_row(
                list(row),
                value_input_option="RAW",
                insert_data_option="INSERT_ROWS",
                table_range="A:F",
            )
        except Exception as exc:
            # Do not automatically retry: a network failure can occur after Sheets
            # accepted the append, and retrying could create a duplicate transaction.
            raise SheetsError(
                f"Could not append to the '{self.transactions_sheet}' sheet. "
                "Check the row in Google Sheets before trying again."
            ) from exc
