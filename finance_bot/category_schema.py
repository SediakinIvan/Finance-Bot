"""Versioned worksheet contract. This module never creates or repairs sheets."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from finance_bot.taxonomy import (
    Category, HistoricalPair, Period, Subcategory, Taxonomy, TaxonomyError,
)


SCHEMA_VERSION = 2
METADATA_SHEET = "_financebot_meta"
CATEGORY_SHEET = "_financebot_categories"
SUBCATEGORY_SHEET = "_financebot_subcategories"
HISTORY_SHEET = "_financebot_category_history"
TRANSACTION_HEADERS = (
    "Дата", "Категория", "Подкатегория", "Сумма", "Доход/Расход", "Примечание",
    "category_id", "subcategory_id", "transaction_id", "validation_state", "report_date",
)
REGISTRY_HEADERS = {
    CATEGORY_SHEET: (
        "category_id", "type", "name", "active", "deactivated_period",
    ),
    SUBCATEGORY_SHEET: (
        "subcategory_id", "current_category_id", "name", "active",
    ),
    HISTORY_SHEET: ("category_id", "subcategory_id"),
}


class SchemaError(TaxonomyError):
    """The workbook is not safe to use under the v2 contract."""


class MigrationState(str, Enum):
    PLANNED = "planned"
    APPLYING = "applying"
    READY = "ready"
    FAILED = "failed"


class AccessMode(str, Enum):
    OPEN = "open"
    MAINTENANCE = "maintenance"


def _integer(value: Any, label: str) -> int:
    text = str(value)
    if isinstance(value, bool) or not text.isascii() or not text.isdecimal():
        raise SchemaError(f"Invalid {label}.")
    return int(text)


@dataclass(frozen=True)
class WorkbookMetadata:
    spreadsheet_id: str
    schema_version: int
    taxonomy_revision: int
    migration_state: MigrationState
    access_mode: AccessMode
    pending_request_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.spreadsheet_id, str) or not self.spreadsheet_id.strip():
            raise SchemaError("Metadata is missing its spreadsheet scope.")
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise SchemaError("Unsupported workbook schema version.")
        if type(self.taxonomy_revision) is not int or self.taxonomy_revision < 0:
            raise SchemaError("Invalid taxonomy revision.")
        if not isinstance(self.migration_state, MigrationState) or not isinstance(
            self.access_mode, AccessMode
        ) or not isinstance(self.pending_request_id, str):
            raise SchemaError("Invalid workbook state.")

    @classmethod
    def from_rows(cls, rows: Sequence[Sequence[Any]]) -> WorkbookMetadata:
        if not rows or list(rows[0]) != ["key", "value"]:
            raise SchemaError("Missing metadata key/value header.")
        values: dict[str, str] = {}
        for row in rows[1:]:
            if not any(str(value).strip() for value in row):
                continue
            if len(row) != 2 or not str(row[0]).strip() or str(row[0]) in values:
                raise SchemaError("Malformed or duplicate metadata key.")
            values[str(row[0])] = str(row[1])
        required = {
            "spreadsheet_id", "schema_version", "taxonomy_revision", "migration_state",
            "access_mode", "pending_request_id",
        }
        if not required <= values.keys():
            raise SchemaError("Incomplete metadata; finish migration before use.")
        try:
            return cls(
                values["spreadsheet_id"],
                _integer(values["schema_version"], "schema version"),
                _integer(values["taxonomy_revision"], "taxonomy revision"),
                MigrationState(values["migration_state"]),
                AccessMode(values["access_mode"]),
                values["pending_request_id"],
            )
        except ValueError as exc:
            raise SchemaError("Invalid workbook metadata.") from exc

    def to_rows(self) -> list[list[str | int]]:
        return [
            ["key", "value"],
            ["spreadsheet_id", self.spreadsheet_id],
            ["schema_version", self.schema_version],
            ["taxonomy_revision", self.taxonomy_revision],
            ["migration_state", self.migration_state.value],
            ["access_mode", self.access_mode.value],
            ["pending_request_id", self.pending_request_id],
        ]

    def require_ready(self, spreadsheet_id: str, *, expected_revision: int | None = None) -> None:
        """Both report readers and writers must pass this gate before accessing data."""
        if self.spreadsheet_id != spreadsheet_id:
            raise SchemaError("Metadata belongs to another spreadsheet.")
        if (
            self.migration_state is not MigrationState.READY
            or self.access_mode is not AccessMode.OPEN
            or self.pending_request_id
        ):
            raise SchemaError("Workbook is not ready; finish migration or recovery.")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision != self.taxonomy_revision
        ):
            raise SchemaError("Taxonomy changed; refresh the selection.")


def read_table(
    rows: Sequence[Sequence[Any]], required: Sequence[str]
) -> list[dict[str, Any]]:
    if not rows:
        raise SchemaError("Missing worksheet header.")
    headers = [str(value).strip() for value in rows[0]]
    if len(set(headers)) != len(headers) or not set(required) <= set(headers):
        raise SchemaError("Missing or duplicate service headers.")
    if any(not header for header in headers):
        raise SchemaError("Empty service header.")
    result = []
    for row in rows[1:]:
        if not any(str(value).strip() for value in row):
            continue
        if len(row) > len(headers):
            raise SchemaError("Unexpected values outside the worksheet header.")
        result.append({
            header: row[index] if index < len(row) else ""
            for index, header in enumerate(headers)
        })
    return result


def _activity(value: Any) -> bool:
    if type(value) is bool:
        return value
    if str(value).strip().upper() not in {"TRUE", "FALSE"}:
        raise SchemaError("Activity must be TRUE or FALSE.")
    return str(value).strip().upper() == "TRUE"


def parse_registry(
    metadata: WorkbookMetadata,
    tables: Mapping[str, Sequence[Sequence[Any]]],
) -> Taxonomy:
    try:
        categories = tuple(
            Category(
                str(row["category_id"]), str(row["type"]), str(row["name"]),
                _activity(row["active"]),
                Period.parse(str(row["deactivated_period"])) if row["deactivated_period"] else None,
            )
            for row in read_table(tables[CATEGORY_SHEET], REGISTRY_HEADERS[CATEGORY_SHEET])
        )
        subcategories = tuple(
            Subcategory(
                str(row["subcategory_id"]), str(row["current_category_id"]),
                str(row["name"]), _activity(row["active"]),
            )
            for row in read_table(tables[SUBCATEGORY_SHEET], REGISTRY_HEADERS[SUBCATEGORY_SHEET])
        )
        pairs = tuple(
            HistoricalPair(str(row["category_id"]), str(row["subcategory_id"]))
            for row in read_table(tables[HISTORY_SHEET], REGISTRY_HEADERS[HISTORY_SHEET])
        )
        return Taxonomy(
            metadata.spreadsheet_id, metadata.taxonomy_revision,
            categories, subcategories, pairs,
        )
    except (KeyError, TaxonomyError) as exc:
        raise SchemaError("Invalid category registry; repair the migration before use.") from exc


class RegistryReader:
    """Read and validate one workbook without a process-global registry cache."""

    def __init__(self, spreadsheet: Any, spreadsheet_id: str) -> None:
        self.spreadsheet = spreadsheet
        self.spreadsheet_id = spreadsheet_id

    def load(self, *, expected_revision: int | None = None) -> Taxonomy:
        if self.spreadsheet.id != self.spreadsheet_id:
            raise SchemaError("Unexpected Google spreadsheet.")
        if callable(getattr(type(self.spreadsheet), "values_batch_get", None)):
            def metadata():
                result = self.spreadsheet.values_get(
                    "'" + METADATA_SHEET + "'", params={"valueRenderOption": "UNFORMATTED_VALUE"})
                rows = [list(row) + [""] * (2 - len(row)) for row in result.get("values", [])]
                return WorkbookMetadata.from_rows(rows)
            first = metadata()
            first.require_ready(self.spreadsheet_id, expected_revision=expected_revision)
            ranges = ["'" + name + "'" for name in REGISTRY_HEADERS]
            result = self.spreadsheet.values_batch_get(ranges, params={"valueRenderOption": "UNFORMATTED_VALUE"})
            batches = result.get("valueRanges", [])
            if len(batches) != len(REGISTRY_HEADERS):
                raise SchemaError("Incomplete category registry read.")
            tables = {}
            for name, batch in zip(REGISTRY_HEADERS, batches):
                rows = batch.get("values", [])
                width = max(len(REGISTRY_HEADERS[name]), max((len(row) for row in rows), default=0))
                tables[name] = [list(row) + [""] * (width - len(row)) for row in rows]
            registry = parse_registry(first, tables)
            last = metadata()
            if first != last:
                raise SchemaError("Workbook state changed while reading the registry.")
            last.require_ready(self.spreadsheet_id, expected_revision=expected_revision)
            return registry
        first = WorkbookMetadata.from_rows(
            self.spreadsheet.worksheet(METADATA_SHEET).get_all_values()
        )
        first.require_ready(self.spreadsheet_id, expected_revision=expected_revision)
        tables = {
            name: self.spreadsheet.worksheet(name).get_all_values()
            for name in REGISTRY_HEADERS
        }
        registry = parse_registry(first, tables)
        last = WorkbookMetadata.from_rows(
            self.spreadsheet.worksheet(METADATA_SHEET).get_all_values()
        )
        if first != last:
            raise SchemaError("Workbook state changed while reading the registry.")
        last.require_ready(self.spreadsheet_id, expected_revision=expected_revision)
        return registry
