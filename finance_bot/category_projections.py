"""Generate the v2 Sheets analytics contract from category IDs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from finance_bot.category_migration import SheetSnapshot, _coordinate, cell_address
from finance_bot.ledger import LedgerError, LedgerRow, normalize_date, parse_amount, require_valid
from finance_bot.taxonomy import HistoricalPair, Period, Taxonomy


DETAIL_SHEET = "_financebot_detail"
LABEL_SHEET = "_financebot_archive_labels"
SNAPSHOT_SHEET = "_financebot_row_snapshots"
ARCHIVE_COLUMNS_SHEET = "_financebot_archive_columns"
LABEL_HEADERS = ("sheet", "cell", "kind", "category_id", "subcategory_id")
DETAIL_HEADERS = ("category_id", "subcategory_id", "category", "subcategory", "amount")
SNAPSHOT_HEADERS = ("transaction_id", "category_id", "subcategory_id", "category_label", "subcategory_label", "transaction_type")


def preferred_report_categories(labels: Sequence[Sequence]) -> tuple[str, ...]:
    """Keep the reference dashboard's block order using its identity index."""
    headings = [row for row in labels[1:] if len(row) >= 4 and row[2] == "heading"]
    if not headings:
        return ()
    title = "Аналитика" if any(row[0] == "Аналитика" for row in headings) else headings[0][0]
    return tuple(dict.fromkeys(row[3] for row in sorted(
        (row for row in headings if row[0] == title), key=lambda row: _coordinate(row[1]))))


def report_blocks(taxonomy: Taxonomy, preferred: Sequence[str] = ()) -> tuple[tuple, ...]:
    """Three block columns, with room for details and a chart in each band."""
    pairs = tuple(dict.fromkeys([
        *(HistoricalPair(item.current_category_id, item.subcategory_id) for item in taxonomy.subcategories
          if item.active or HistoricalPair(item.current_category_id, item.subcategory_id) in taxonomy.historical_pairs),
        *taxonomy.historical_pairs,
    ]))
    groups = {item.category_id: tuple(pair for pair in pairs if pair.category_id == item.category_id)
              for item in taxonomy.categories}
    order = list(dict.fromkeys([*(key for key in preferred if key in groups), *(
        item.category_id for item in taxonomy.categories if len(groups[item.category_id]) > 1)]))
    blocks, start = [], 1
    for index in range(0, len(order), 3):
        band = order[index:index + 3]
        for offset, key in enumerate(band):
            blocks.append((key, start, 7 + offset * 3, groups[key]))
        start += max(18, max(len(groups[key]) for key in band) + 12)
    return tuple(blocks)


def report_label_rows(taxonomy: Taxonomy, preferred: Sequence[str] = (), title: str = "Аналитика") -> list[list]:
    labels = [[title, cell_address(number, 1), "category", item.category_id, ""]
              for number, item in enumerate((c for c in taxonomy.categories if c.transaction_type == "Расход"), 9)]
    for key, row, column, pairs in report_blocks(taxonomy, preferred):
        labels.append([title, cell_address(row, column), "heading", key, ""])
        labels.extend([title, cell_address(row + 2 + offset, column), "subcategory", key, pair.subcategory_id]
                      for offset, pair in enumerate(pairs))
    return labels


def table_sheet(title: str, rows: Sequence[Sequence]) -> SheetSnapshot:
    return SheetSnapshot(title, {
        (row, column): value for row, values in enumerate(rows, 1)
        for column, value in enumerate(values, 1)
    })


def sheet_rows(sheet: SheetSnapshot) -> list[list]:
    width = max((column for _, column in sheet.cells), default=0)
    return [[sheet.cells.get((row, column), "") for column in range(1, width + 1)]
            for row in range(1, sheet.last_row + 1)]


def taxonomy_view(taxonomy: Taxonomy) -> SheetSnapshot:
    return table_sheet("Категории", [("Тип", "Категория", "Подкатегория"), *[
        (taxonomy.category(pair.category_id).transaction_type,
         taxonomy.category(pair.category_id).name,
         taxonomy.subcategory(pair.subcategory_id).name)
        for pair in taxonomy.active_pairs()
    ]])


@dataclass(frozen=True)
class MonthlyTotals:
    expenses: Decimal
    income: Decimal
    categories: dict[str, Decimal]
    subcategories: dict[HistoricalPair, Decimal]

    @property
    def balance(self) -> Decimal:
        return self.income - self.expenses


def calculate_totals(rows: Sequence[LedgerRow], taxonomy: Taxonomy, period: Period) -> MonthlyTotals:
    """Independent reference calculator for migration and formula verification."""
    require_valid(rows)
    categories = {item.category_id: Decimal(0) for item in taxonomy.categories}
    details: dict[HistoricalPair, Decimal] = {}
    income, expenses = Decimal(0), Decimal(0)
    for row in rows:
        if not row.state:
            continue
        day = normalize_date(row.report_date)
        if (day.year, day.month) != (period.year, period.month):
            continue
        parent = taxonomy.category(row.category_id)
        if parent.deactivated_period and parent.deactivated_period < period:
            raise LedgerError("Operation uses a category after its deactivation period.")
        amount = parse_amount(row.values[3])
        categories[parent.category_id] += amount
        pair = HistoricalPair(row.category_id, row.subcategory_id)
        details[pair] = details.get(pair, Decimal(0)) + amount
        if parent.transaction_type == "Расход":
            expenses += amount
        else:
            income += amount
    return MonthlyTotals(expenses, income, categories, details)


def analytics_sheets(
    taxonomy: Taxonomy, period: Period, *, transactions_title: str = "Траты",
    analytics_title: str = "Аналитика",
    preferred_categories: Sequence[str] = (),
) -> tuple[SheetSnapshot, SheetSnapshot]:
    """Layout is generated, while business calculations stay as Sheets formulas.

    A:F user fields and G:K services are installed in this order by migration.
    Runtime row parsers still resolve services by header. A schema layout check
    prevents generated formulas from silently targeting reordered service fields.
    """
    source = "'" + transactions_title.replace("'", "''") + "'!"
    report = "'" + analytics_title.replace("'", "''") + "'!"
    populated = "+".join(f"LEN({source}{column}2:{column})" for column in "ABCDEF")
    category_labels = f"IFNA(VLOOKUP({source}G2:G,'_financebot_categories'!A:C,3,FALSE),\"\")"
    child_labels = f"IFNA(VLOOKUP({source}H2:H,'_financebot_subcategories'!A:C,3,FALSE),\"\")"
    status = (
        f'=ARRAYFORMULA(IF(AND(SUMPRODUCT(--(({populated})>0))=COUNTIF({source}J2:J,"valid"),'
        f'SUMPRODUCT(--(LOWER(TRIM({source}B2:B))<>LOWER(TRIM({category_labels}))))=0,'
        f'SUMPRODUCT(--(LOWER(TRIM({source}C2:C))<>LOWER(TRIM({child_labels}))))=0),"ready","invalid"))'
    )
    # No IFERROR: genuine source errors must remain visible.
    def sum_formula(kind: str, category_id: str | None = None, child_id: str | None = None) -> str:
        args = [f"{source}D2:D", f"{source}E2:E", f'"{kind}"',
                f"{source}K2:K", f'">="&DATE({report}$D$2,{report}$D$1,1)',
                f"{source}K2:K", f'"<"&EDATE(DATE({report}$D$2,{report}$D$1,1),1)']
        if category_id:
            args += [f"{source}G2:G", f'"{category_id}"']
        if child_id:
            args += [f"{source}H2:H", f'"{child_id}"']
        return f'=IF({report}$F$1<>"ready",NA(),SUMIFS({",".join(args)}))'

    summary = table_sheet(analytics_title, [
        ["ТЕКУЩАЯ АНАЛИТИКА", "", "Месяц:", period.month, "Состояние", ""],
        ["", "", "Год:", period.year],
        ["Расходы за месяц"], ["Доход за месяц"], ["Остаток на текущий момент"], [],
        ["Траты по категориям (за месяц)"], ["Категория", "Сумма", "Процент от трат", "category_id"],
    ])
    summary.formulas[1, 6] = status
    summary.formulas[3, 2] = sum_formula("Расход")
    summary.formulas[4, 2] = sum_formula("Доход")
    summary.formulas[5, 2] = "=B4-B3"
    for number, category in enumerate((item for item in taxonomy.categories if item.transaction_type == "Расход"), 9):
        summary.cells[number, 1] = category.name
        summary.cells[number, 4] = category.category_id
        summary.formulas[number, 2] = sum_formula("Расход", category.category_id)
        summary.formulas[number, 3] = f"=IF($B$3=0,0,B{number}/$B$3)"
    details = table_sheet(DETAIL_SHEET, [DETAIL_HEADERS])
    pairs = dict.fromkeys([
        *(HistoricalPair(item.current_category_id, item.subcategory_id) for item in taxonomy.subcategories),
        *taxonomy.historical_pairs,
    ])
    for number, pair in enumerate(pairs, 2):
        parent, child = taxonomy.category(pair.category_id), taxonomy.subcategory(pair.subcategory_id)
        for column, value in enumerate((pair.category_id, pair.subcategory_id, parent.name, child.name), 1):
            details.cells[number, column] = value
        details.formulas[number, 5] = sum_formula(parent.transaction_type, pair.category_id, pair.subcategory_id)
    detail_rows = {pair: number for number, pair in enumerate(pairs, 2)}
    for key, row, column, children in report_blocks(taxonomy, preferred_categories):
        parent = taxonomy.category(key)
        summary.cells[row, column] = "Подкатегории: " + parent.name
        summary.cells[row + 1, column] = "Подкатегория"
        summary.cells[row + 1, column + 1] = "Сумма"
        for offset, pair in enumerate(children):
            summary.cells[row + 2 + offset, column] = taxonomy.subcategory(pair.subcategory_id).name
            summary.formulas[row + 2 + offset, column + 1] = f"='{DETAIL_SHEET}'!E{detail_rows[pair]}"
        first, last = cell_address(row + 2, column + 1), cell_address(row + 1 + len(children), column + 1)
        summary.formulas[row, column + 1] = f"=SUM({first}:{last})" if children else "=0"
    return summary, details
