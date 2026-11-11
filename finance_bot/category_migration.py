"""Read-only preflight for category migration; no apply or repair side effects."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import posixpath
import re
import time
from typing import Any
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from finance_bot.taxonomy import normalized_name


NAMESPACE = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@dataclass
class SheetSnapshot:
    title: str
    cells: dict[tuple[int, int], Any] = field(default_factory=dict)
    formulas: dict[tuple[int, int], str] = field(default_factory=dict)
    validations: list[dict[str, Any]] = field(default_factory=list)
    protected_ranges: list[dict[str, Any]] = field(default_factory=list)

    def cell(self, row: int, column: int) -> str:
        value = self.cells.get((row, column), "")
        return str(value).strip() if value is not None else ""

    @property
    def last_row(self) -> int:
        return max((row for row, _ in self.cells), default=0)


@dataclass
class WorkbookSnapshot:
    source_fingerprint: str
    sheets: dict[str, SheetSnapshot]
    limitations: tuple[str, ...] = ()


def _coordinate(address: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", address)
    if not match:
        raise ValueError("Invalid cell address in workbook.")
    column = 0
    for character in match[1]:
        column = column * 26 + ord(character) - ord("A") + 1
    return int(match[2]), column


def cell_address(row: int, column: int) -> str:
    letters = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return f"{letters}{row}"


def read_xlsx(path: Path) -> WorkbookSnapshot:
    """Extract stored values and formulas without opening Excel or recalculating."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sheets: dict[str, SheetSnapshot] = {}
    with ZipFile(path, "r") as archive:
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = [
                "".join(item.itertext())
                for item in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("s:si", NAMESPACE)
            ]
        relationships = {
            item.attrib["Id"]: item.attrib["Target"]
            for item in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        }
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        for sheet in workbook.findall("s:sheets/s:sheet", NAMESPACE):
            relation_id = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            target = relationships[relation_id]
            target = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            root = ET.fromstring(archive.read(target))
            snapshot = SheetSnapshot(sheet.attrib["name"])
            for cell in root.findall("s:sheetData/s:row/s:c", NAMESPACE):
                coordinate = _coordinate(cell.attrib["r"])
                stored = cell.find("s:v", NAMESPACE)
                value = stored.text or "" if stored is not None else ""
                if cell.attrib.get("t") == "s":
                    value = strings[int(value)]
                elif cell.attrib.get("t") == "inlineStr":
                    value = "".join(cell.find("s:is", NAMESPACE).itertext())
                elif cell.attrib.get("t", "n") == "n" and value:
                    number = float(value)
                    value = int(number) if number.is_integer() else number
                snapshot.cells[coordinate] = value
                formula = cell.find("s:f", NAMESPACE)
                if formula is not None:
                    # A shared-formula follower has no text. Inventory it without
                    # treating it as a blank formula or inventing a recalculation.
                    snapshot.formulas[coordinate] = formula.text or "[shared formula]"
            for validation in root.findall("s:dataValidations/s:dataValidation", NAMESPACE):
                snapshot.validations.append({
                    "range": validation.attrib.get("sqref", ""),
                    "type": validation.attrib.get("type", ""),
                })
            protection = root.find("s:sheetProtection", NAMESPACE)
            if protection is not None:
                snapshot.protected_ranges.append({"sheet_protection": True})
            sheets[snapshot.title] = snapshot
    return WorkbookSnapshot(digest, sheets, (
        "XLSX export only: live spreadsheet, Apps Script and triggers are not inspected.",
        "Stored formula results were not recalculated; Google protected ranges may not survive export.",
    ))


def _read_google(call):
    """Retry only GET rate limits; never retry an ambiguous financial write."""
    for attempt in range(6):
        try:
            return call()
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) != 429 or attempt == 5:
                raise
            time.sleep(2 ** (attempt + 1))


def read_google_spreadsheet(spreadsheet: Any) -> WorkbookSnapshot:
    """Only calls Sheets GET operations. The caller supplies an authorized client."""
    metadata = _read_google(lambda: spreadsheet.fetch_sheet_metadata(params={
        "includeGridData": "true",
        "fields": "sheets(properties(title),protectedRanges(range),data(startRow,startColumn,rowData(values(dataValidation))))",
    }))
    by_title = {item["properties"]["title"]: item for item in metadata.get("sheets", [])}
    sheets = {}
    fingerprint_parts = []
    worksheets = _read_google(spreadsheet.worksheets)
    batched = {}
    if callable(getattr(type(spreadsheet), "values_batch_get", None)):
        ranges = ["'" + item.title.replace("'", "''") + "'!A1:" + cell_address(item.row_count, item.col_count)
                  for item in worksheets]
        for option in ("UNFORMATTED_VALUE", "FORMULA"):
            result = _read_google(lambda: spreadsheet.values_batch_get(ranges, params={"valueRenderOption": option}))
            values = result.get("valueRanges", [])
            if len(values) != len(worksheets):
                raise ValueError("Incomplete native workbook batch read.")
            for item, value_range in zip(worksheets, values):
                matrix = value_range.get("values", [])
                width = max((len(row) for row in matrix), default=0)
                # Match get_all_values padding, preserving snapshot fingerprints.
                batched[item.title, option] = [row + [""] * (width - len(row)) for row in matrix]
    for worksheet in worksheets:
        snapshot = SheetSnapshot(worksheet.title)
        values = (batched[worksheet.title, "UNFORMATTED_VALUE"] if batched else
                  _read_google(lambda: worksheet.get_all_values(value_render_option="UNFORMATTED_VALUE")))
        formulas = (batched[worksheet.title, "FORMULA"] if batched else
                    _read_google(lambda: worksheet.get_all_values(value_render_option="FORMULA")))
        for row_number, row in enumerate(values, start=1):
            for column, value in enumerate(row, start=1):
                snapshot.cells[row_number, column] = value
        for row_number, row in enumerate(formulas, start=1):
            for column, value in enumerate(row, start=1):
                if isinstance(value, str) and value.startswith("="):
                    snapshot.formulas[row_number, column] = value
        properties = by_title.get(worksheet.title, {})
        snapshot.protected_ranges = properties.get("protectedRanges", [])
        for grid in properties.get("data", []):
            for row_offset, row in enumerate(grid.get("rowData", [])):
                for column_offset, cell in enumerate(row.get("values", [])):
                    rule = cell.get("dataValidation")
                    if rule:
                        snapshot.validations.append({
                            "range": cell_address(grid.get("startRow", 0) + row_offset + 1, grid.get("startColumn", 0) + column_offset + 1),
                            "type": rule.get("condition", {}).get("type", ""),
                        })
        sheets[worksheet.title] = snapshot
        fingerprint_parts.append((worksheet.title, values, formulas))
    fingerprint = hashlib.sha256(json.dumps(fingerprint_parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return WorkbookSnapshot(fingerprint, sheets, (
        "Read-only preflight is not an atomic snapshot; recheck under maintenance before applying.",
        "Apps Script source and triggers require a separate backup and inspection.",
    ))


def inspect_workbook(workbook: WorkbookSnapshot) -> dict[str, Any]:
    """Return structural findings, never transaction amounts, dates or notes."""
    findings: list[dict[str, Any]] = []
    pairs: set[tuple[str, str, str]] = set()
    display_pairs: list[dict[str, str]] = []

    def issue(code: str, sheet: str, address: str, **details: Any) -> None:
        findings.append({"code": code, "sheet": sheet, "cell": address, **details})

    for title in ("Траты", "Категории", "Аналитика"):
        if title not in workbook.sheets:
            issue("missing_sheet", title, "")

    categories = workbook.sheets.get("Категории", SheetSnapshot("Категории"))
    header = {normalized_name(categories.cell(1, column)): column for column in range(1, 1 + max((c for _, c in categories.cells), default=0))}
    if not {"тип", "категория", "подкатегория"} <= header.keys():
        issue("invalid_taxonomy_header", categories.title, "A1")
    else:
        counts: Counter[tuple[str, str, str]] = Counter()
        for row in range(2, categories.last_row + 1):
            raw = tuple(categories.cell(row, header[key]) for key in ("тип", "категория", "подкатегория"))
            if not any(raw):
                continue
            key = tuple(normalized_name(value) for value in raw)
            if key[0] not in {"доход", "расход"} or not all(key):
                issue("invalid_taxonomy_row", categories.title, f"A{row}")
                continue
            counts[key] += 1
            pairs.add(key)
            if counts[key] == 1:
                display_pairs.append(dict(zip(("type", "category", "subcategory"), raw)))
            else:
                issue("duplicate_taxonomy_pair", categories.title, f"A{row}", category=raw[1], subcategory=raw[2])

    operations = workbook.sheets.get("Траты", SheetSnapshot("Траты"))
    transaction_count = 0
    expected = ("Дата", "Категория", "Подкатегория", "Сумма", "Доход/Расход", "Примечание")
    if tuple(operations.cell(1, col) for col in range(1, 7)) != expected:
        issue("invalid_transaction_header", operations.title, "A1:F1")
    for row in range(2, operations.last_row + 1):
        fields = [operations.cell(row, col) for col in range(1, 7)]
        if not any(fields):
            continue
        transaction_count += 1
        if not fields[0]:
            issue("missing_transaction_date", operations.title, f"A{row}")
        key = tuple(normalized_name(value) for value in (fields[4], fields[1], fields[2]))
        if key[0] == "вклад":
            issue("unsupported_legacy_type", operations.title, f"E{row}")
        elif key not in pairs:
            issue("unmapped_transaction_pair", operations.title, f"B{row}:C{row}", category=fields[1], subcategory=fields[2])

    known_categories = {key[1] for key in pairs if key[0] == "расход"}
    summary = workbook.sheets.get("Архив")
    archive_columns = []
    if summary:
        for column in range(5, 1 + max((c for _, c in summary.cells), default=0)):
            label = summary.cell(1, column)
            if label:
                archive_columns.append({"cell": cell_address(1, column), "category": label})
                if normalized_name(label) not in known_categories:
                    issue("unmapped_archive_category", summary.title, cell_address(1, column), category=label)

    label_index = []
    for sheet in workbook.sheets.values():
        if sheet.title != "Аналитика" and not sheet.title.startswith("Архив ("):
            continue
        # Find the summary category table by its label, not historic row numbers.
        for (row, col), value in sheet.cells.items():
            label = str(value).strip()
            if normalized_name(label) == "траты по категориям (за месяц)":
                for data_row in range(row + 2, sheet.last_row + 1):
                    category = sheet.cell(data_row, col)
                    if not category:
                        break
                    label_index.append({"sheet": sheet.title, "cell": cell_address(data_row, col), "kind": "category", "category": category})
                    if normalized_name(category) not in known_categories:
                        issue("unmapped_report_category", sheet.title, cell_address(data_row, col), category=category)
            if not normalized_name(label).startswith("подкатегории:"):
                continue
            parent = label.partition(":")[2].strip()
            kind = "доход" if normalized_name(parent) == "доход" else "расход"
            label_index.append({"sheet": sheet.title, "cell": cell_address(row, col), "kind": "heading", "category": parent})
            for data_row in range(row + 2, sheet.last_row + 1):
                child = sheet.cell(data_row, col)
                if not child:
                    break
                entry = {"sheet": sheet.title, "cell": cell_address(data_row, col), "kind": "subcategory", "category": parent, "subcategory": child}
                label_index.append(entry)
                if (kind, normalized_name(parent), normalized_name(child)) not in pairs:
                    issue("unmapped_report_subcategory", sheet.title, entry["cell"], category=parent, subcategory=child)
                formula = sheet.formulas.get((data_row, col + 1), "")
                # The legacy SUMIFS contract uses C:C followed by a literal criterion.
                match = re.search(r"!?\$?C:\$?C\s*[,;]\s*\"([^\"]+)\"", formula, re.IGNORECASE)
                if match and normalized_name(match[1]) != normalized_name(child):
                    issue("report_label_differs_from_criterion", sheet.title, entry["cell"], category=parent, subcategory=child, criterion=match[1])

    return {
        "source_fingerprint": workbook.source_fingerprint,
        "mode": "read_only", "can_apply": False,
        "limitations": list(workbook.limitations),
        "sheets": [
            {"title": sheet.title, "formula_count": len(sheet.formulas),
             "validations": sheet.validations, "protected_range_count": len(sheet.protected_ranges)}
            for sheet in workbook.sheets.values()
        ],
        "transaction_count": transaction_count,
        "taxonomy_pairs": display_pairs,
        "archive_columns": archive_columns,
        "report_labels": label_index,
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--xlsx", type=Path)
    source.add_argument("--spreadsheet-id")
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.xlsx and args.report.resolve() == args.xlsx.resolve():
        parser.error("The report must not overwrite the source workbook.")
    if args.xlsx:
        workbook = read_xlsx(args.xlsx)
    else:
        if not args.credentials:
            parser.error("--credentials is required with --spreadsheet-id")
        import gspread
        client = gspread.service_account(filename=str(args.credentials), scopes=[
            "https://www.googleapis.com/auth/spreadsheets.readonly",
        ])
        workbook = read_google_spreadsheet(client.open_by_key(args.spreadsheet_id))
    report = inspect_workbook(workbook)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Read-only preflight: {len(report['sheets'])} sheets; {len(report['findings'])} findings. Report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
