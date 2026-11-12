"""Reconcile manually editable ledger rows against stable category identities."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import math
from typing import Any, Sequence
from uuid import UUID

from finance_bot.category_schema import TRANSACTION_HEADERS, SchemaError
from finance_bot.taxonomy import Taxonomy, TaxonomyError, new_id, normalized_name


class LedgerError(ValueError):
    """One or more rows need correction before reporting or archiving."""


def normalize_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            number = float(value)
            if not math.isfinite(number) or number < 1:
                raise ValueError
            return date(1899, 12, 30) + timedelta(days=math.floor(number))
        except (ValueError, OverflowError) as exc:
            raise LedgerError("invalid_date") from exc
    if isinstance(value, str):
        for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value.strip(), pattern).date()
            except ValueError:
                pass
    raise LedgerError("invalid_date")


def sheet_date(value: Any) -> int:
    return (normalize_date(value) - date(1899, 12, 30)).days


def parse_amount(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise LedgerError("invalid_amount")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise LedgerError("invalid_amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise LedgerError("invalid_amount")
    return amount


@dataclass(frozen=True)
class RowIdentity:
    transaction_id: str
    category_id: str
    subcategory_id: str
    category_label: str
    subcategory_label: str
    transaction_type: str


@dataclass(frozen=True)
class LedgerRow:
    row_number: int
    values: tuple[Any, ...]
    category_id: str
    subcategory_id: str
    transaction_id: str
    state: str
    report_date: int | None

    def service_values(self) -> list[Any]:
        return [self.category_id, self.subcategory_id, self.transaction_id,
                self.state, self.report_date if self.report_date is not None else ""]

    def identity(self) -> RowIdentity:
        return RowIdentity(
            self.transaction_id, self.category_id, self.subcategory_id,
            str(self.values[1]), str(self.values[2]), str(self.values[4]),
        )


def _is_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def resolve_new_pair(taxonomy: Taxonomy, kind: Any, category: Any, child: Any) -> tuple[str, str]:
    matches = []
    for pair in taxonomy.active_pairs():
        parent = taxonomy.category(pair.category_id)
        subcategory = taxonomy.subcategory(pair.subcategory_id)
        if (normalized_name(parent.transaction_type) == normalized_name(str(kind))
                and normalized_name(parent.name) == normalized_name(str(category))
                and normalized_name(subcategory.name) == normalized_name(str(child))):
            matches.append((pair.category_id, pair.subcategory_id))
    if len(matches) != 1:
        raise LedgerError("invalid_category_pair")
    return matches[0]


def reconcile_rows(
    rows: Sequence[Sequence[Any]], taxonomy: Taxonomy,
    snapshots: dict[str, RowIdentity],
) -> tuple[LedgerRow, ...]:
    """Pure reconciliation: caller applies returned service fields after a recheck.

    Only trusted snapshots can preserve an inactive/moved historical pair. A raw
    row with forged IDs is not proof that the historical relationship is valid.
    User-facing A:F values are never overwritten by this function.
    """
    if not rows or tuple(rows[0][:6]) != TRANSACTION_HEADERS[:6]:
        raise SchemaError("Unexpected user-facing ledger headers.")
    headers = [str(value) for value in rows[0]]
    if len(set(headers)) != len(headers) or not set(TRANSACTION_HEADERS) <= set(headers):
        raise SchemaError("Missing or duplicate ledger service headers.")
    indexes = {name: headers.index(name) for name in TRANSACTION_HEADERS[6:]}
    ids = [str(row[indexes["transaction_id"]]) for row in rows[1:]
           if len(row) > indexes["transaction_id"] and row[indexes["transaction_id"]]]
    counts = Counter(ids)
    result = []
    for number, source in enumerate(rows[1:], 2):
        values = tuple([*source[:6], *([""] * max(0, 6 - len(source)))])
        fields = {key: source[index] if index < len(source) else "" for key, index in indexes.items()}
        if not any(str(value).strip() for value in values):
            # Clearing a row removes its identity; snapshots are rebuilt by ID.
            result.append(LedgerRow(number, values, "", "", "", "", None))
            continue
        transaction_id = str(fields["transaction_id"])
        category_id, child_id = "", ""
        previous = snapshots.get(transaction_id)
        report_date = None
        state = "valid"
        try:
            if transaction_id and (not _is_uuid(transaction_id) or counts[transaction_id] > 1):
                raise LedgerError("duplicate_or_invalid_transaction_id")
            transaction_id = transaction_id or new_id()
            previous = snapshots.get(transaction_id)
            unchanged = previous is not None and (
                normalized_name(str(values[1])), normalized_name(str(values[2])),
                normalized_name(str(values[4])),
            ) == (
                normalized_name(previous.category_label), normalized_name(previous.subcategory_label),
                normalized_name(previous.transaction_type),
            )
            if unchanged:
                category_id, child_id = previous.category_id, previous.subcategory_id
                if (str(fields["category_id"]), str(fields["subcategory_id"])) != (category_id, child_id):
                    raise LedgerError("identity_tampered")
                taxonomy.validate_pair(category_id, child_id, new_operation=False)
            else:
                category_id, child_id = resolve_new_pair(taxonomy, values[4], values[1], values[2])
            report_date = sheet_date(values[0])
            parse_amount(values[3])
            if len(str(values[5])) > 1000:
                raise LedgerError("note_too_long")
        except (LedgerError, TaxonomyError) as exc:
            state = str(exc) if isinstance(exc, LedgerError) else "invalid_category_pair"
            if previous is not None and state not in {"duplicate_or_invalid_transaction_id", "identity_tampered"}:
                # An incomplete manual edit must not erase its trusted old pair:
                # restoring the old labels may be necessary after deactivation.
                category_id, child_id = previous.category_id, previous.subcategory_id
        result.append(LedgerRow(number, values, category_id, child_id, transaction_id, state, report_date))
    return tuple(result)


def require_valid(rows: Sequence[LedgerRow]) -> None:
    invalid = [row.row_number for row in rows if row.state not in {"", "valid"}]
    if invalid:
        raise LedgerError("Correct ledger rows: " + ", ".join(map(str, invalid)))
