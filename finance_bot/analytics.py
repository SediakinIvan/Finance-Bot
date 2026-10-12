"""Parse calculated monthly analytics from the Analytics worksheet."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence


class AnalyticsError(RuntimeError):
    """An analytics worksheet value is missing or invalid."""


@dataclass(frozen=True)
class AnalyticsValue:
    label: str
    amount: Decimal


@dataclass(frozen=True)
class MonthlyAnalytics:
    month: int
    year: int
    expenses: Decimal
    income: Decimal
    balance: Decimal
    categories: tuple[AnalyticsValue, ...]


@dataclass(frozen=True)
class CategoryAnalytics:
    category: str
    month: int
    year: int
    total: Decimal
    subcategories: tuple[AnalyticsValue, ...]


_SUMMARY_LABELS = {
    "расходы за месяц": "expenses",
    "доход за месяц": "income",
    "остаток на текущий момент": "balance",
}
_MONTH_LABELS = {"месяц:", "месяц"}
_YEAR_LABELS = {"год:", "год"}


def _cell(rows: Sequence[Sequence[Any]], row: int, column: int) -> str:
    if row >= len(rows) or column >= len(rows[row]):
        return ""
    return str(rows[row][column]).strip()


def _normalized(value: Any) -> str:
    return " ".join(str(value).strip().casefold().split())


def _amount(value: Any, *, field: str) -> Decimal:
    text = str(value).strip().replace("\u00a0", "").replace("\u202f", "")
    text = re.sub(r"р\.|₽", "", text, flags=re.IGNORECASE).replace(" ", "")
    if not text:
        raise AnalyticsError(f"The Analytics sheet has an empty value for {field}.")
    normalized = re.sub(r"[^\d,.\-]", "", text).replace(",", ".")
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise AnalyticsError(
            f"The Analytics sheet has an invalid numeric value for {field}: {value!r}."
        ) from exc


def _required_value(values: dict[str, Decimal], key: str, label: str) -> Decimal:
    try:
        return values[key]
    except KeyError as exc:
        raise AnalyticsError(f"The Analytics sheet is missing the '{label}' value.") from exc


def parse_monthly_analytics(rows: Sequence[Sequence[Any]]) -> MonthlyAnalytics:
    if not rows:
        raise AnalyticsError("The Analytics sheet is empty.")

    summary: dict[str, Decimal] = {}
    month: int | None = None
    year: int | None = None
    category_header: int | None = None
    for row_index in range(len(rows)):
        label = _normalized(_cell(rows, row_index, 0))
        if label in _SUMMARY_LABELS:
            key = _SUMMARY_LABELS[label]
            summary[key] = _amount(_cell(rows, row_index, 1), field=label)
        if _normalized(_cell(rows, row_index, 2)) in _MONTH_LABELS:
            month_value = _cell(rows, row_index, 3)
            if month_value:
                try:
                    month = int(month_value)
                except ValueError as exc:
                    raise AnalyticsError("The Analytics sheet has an invalid month.") from exc
        if _normalized(_cell(rows, row_index, 2)) in _YEAR_LABELS:
            year_value = _cell(rows, row_index, 3)
            if year_value:
                try:
                    year = int(year_value)
                except ValueError as exc:
                    raise AnalyticsError("The Analytics sheet has an invalid year.") from exc
        if label == "траты по категориям (за месяц)":
            category_header = row_index + 1

    if month is None or not 1 <= month <= 12:
        raise AnalyticsError("The Analytics sheet is missing a valid month.")
    if year is None or year < 1:
        raise AnalyticsError("The Analytics sheet is missing a valid year.")

    categories: list[AnalyticsValue] = []
    if category_header is None:
        raise AnalyticsError("The Analytics sheet is missing the monthly category table.")
    for row_index in range(category_header + 1, len(rows)):
        label = _cell(rows, row_index, 0)
        if not label:
            break
        if _normalized(label) == "категория":
            continue
        categories.append(
            AnalyticsValue(
                label=label,
                amount=_amount(
                    _cell(rows, row_index, 1), field=f"category '{label}'"
                ),
            )
        )
    if not categories:
        raise AnalyticsError("The Analytics sheet has no monthly category totals.")

    return MonthlyAnalytics(
        month=month,
        year=year,
        expenses=_required_value(summary, "expenses", "Расходы за месяц"),
        income=_required_value(summary, "income", "Доход за месяц"),
        balance=_required_value(summary, "balance", "Остаток на текущий момент"),
        categories=tuple(categories),
    )


def parse_category_analytics(
    rows: Sequence[Sequence[Any]], category: str
) -> CategoryAnalytics:
    target = _normalized(category)
    monthly = parse_monthly_analytics(rows)
    category_total = next(
        (value.amount for value in monthly.categories if _normalized(value.label) == target),
        None,
    )
    if category_total is None:
        raise AnalyticsError(
            f"The Analytics sheet has no monthly total for category '{category}'."
        )

    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            heading = _normalized(value)
            if not heading.startswith("подкатегории:"):
                continue
            if _normalized(heading.partition(":")[2]) != target:
                continue
            subcategories: list[AnalyticsValue] = []
            for data_row in range(row_index + 2, len(rows)):
                label = _cell(rows, data_row, column)
                if not label:
                    break
                subcategories.append(
                    AnalyticsValue(
                        label=label,
                        amount=_amount(
                            _cell(rows, data_row, column + 1),
                            field=f"subcategory '{label}' in '{category}'",
                        ),
                    )
                )
            if subcategories:
                return CategoryAnalytics(
                    category=category,
                    month=monthly.month,
                    year=monthly.year,
                    total=category_total,
                    subcategories=tuple(subcategories),
                )
            break

    raise AnalyticsError(
        f"The Analytics sheet has no subcategory totals for '{category}'."
    )
