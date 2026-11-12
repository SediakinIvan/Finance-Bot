"""Install a reviewed v2 plan into an explicitly identified Google Sheets copy."""

from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from finance_bot.category_migration import read_google_spreadsheet
from finance_bot.category_projections import (
    DETAIL_SHEET, SNAPSHOT_SHEET, calculate_totals, sheet_rows,
)
from finance_bot.category_schema import (
    AccessMode, METADATA_SHEET, MigrationState, REGISTRY_HEADERS,
    WorkbookMetadata, parse_registry,
)
from finance_bot.ledger import reconcile_rows, require_valid
from finance_bot.migration_apply import (
    MigrationError, MigrationPlan, _private_json, fingerprint,
    snapshot_payload, verify_migrated_copy,
)
from finance_bot.taxonomy import Period


def _quote(title: str) -> str:
    return "'" + title.replace("'", "''") + "'!"


def native_formula(formula: str, spreadsheet: Any) -> str:
    """Render generated canonical formulas for the workbook's input locale.

    Preserve commas inside literal strings and quoted sheet names. The live
    migration currently supports the project's Russian and English locales;
    unknown locales require an explicit compatibility check before any write.
    """
    locale = getattr(spreadsheet, "locale", "en_US")
    if locale in {"en_US", "en_GB"}:
        return formula
    if locale != "ru_RU":
        raise MigrationError(f"Formula input locale is not verified: {locale}")
    result, quote = [], ""
    index = 0
    while index < len(formula):
        char = formula[index]
        if quote:
            result.append(char)
            if char == quote:
                if index + 1 < len(formula) and formula[index + 1] == quote:
                    result.append(formula[index + 1])
                    index += 1
                else:
                    quote = ""
        elif char in {'"', "'"}:
            quote = char
            result.append(char)
        else:
            result.append(";" if char == "," else char)
        index += 1
    return "".join(result)


def ledger_dropdown_table_updates(metadata: dict) -> list[dict]:
    """Allow row-specific B/C validation without removing the native table.

    A typed table dropdown owns one rule for the entire column. Apps Script's
    dependent per-row validation requires those two columns to be untyped.
    Preserve all other column types, table formatting, range, names and values.
    """
    requests = []
    for sheet in metadata.get("sheets", []):
        if sheet.get("properties", {}).get("title") != "Траты":
            continue
        for table in sheet.get("tables", []):
            columns = deepcopy(table.get("columnProperties", []))
            start = table.get("range", {}).get("startColumnIndex", 0)
            changed = False
            for column in columns:
                absolute = start + column.get("columnIndex", 0)
                if absolute in {1, 2} and (
                    column.get("columnType", "COLUMN_TYPE_UNSPECIFIED") != "COLUMN_TYPE_UNSPECIFIED"
                    or "dataValidationRule" in column
                ):
                    column["columnType"] = "COLUMN_TYPE_UNSPECIFIED"
                    column.pop("dataValidationRule", None)
                    changed = True
            if changed:
                requests.append({"updateTable": {"table": {
                    "tableId": table["tableId"], "columnProperties": columns,
                }, "fields": "columnProperties"}})
    return requests


def _write_sheet(spreadsheet: Any, sheet) -> None:
    formulas = {key: native_formula(value, spreadsheet) for key, value in sheet.formulas.items()}
    existing = {item.title: item for item in spreadsheet.worksheets()}
    rows = sheet_rows(sheet)
    height = max(1, len(rows), max((r for r, _ in sheet.formulas), default=1))
    width = max(1, max((len(row) for row in rows), default=1),
                max((c for _, c in sheet.formulas), default=1))
    worksheet = existing.get(sheet.title)
    if worksheet is None:
        worksheet = spreadsheet.add_worksheet(title=sheet.title, rows=max(100, height), cols=max(11, width))
    elif worksheet.row_count < height or worksheet.col_count < width:
        worksheet.resize(rows=max(worksheet.row_count, height), cols=max(worksheet.col_count, width))
    from finance_bot.category_migration import cell_address
    if sheet.title == "Траты":
        # A:F and original formatting are never rewritten by this installer.
        data = [
            {"range": _quote(sheet.title) + "G1:K1", "values": [rows[0][6:11]]},
            {"range": _quote(sheet.title) + f"G2:K{len(rows)}",
             "values": [row[6:11] for row in rows[1:]]},
        ] if len(rows) > 1 else [
            {"range": _quote(sheet.title) + "G1:K1", "values": [rows[0][6:11]]},
        ]
    else:
        worksheet.clear()
        data = [{"range": _quote(sheet.title) + "A1", "values": rows}]
    spreadsheet.values_batch_update({"valueInputOption": "RAW", "data": data})
    if sheet.formulas:
        spreadsheet.values_batch_update({
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": _quote(sheet.title) + cell_address(*coordinate),
                      "values": [[formula]]} for coordinate, formula in formulas.items()],
        })


def install_copy(
    source, plan: MigrationPlan, target: Any, native_backup: Any,
    state_directory: Path, original_script: Path, *, confirmed: bool,
    maintenance_confirmed: bool,
) -> None:
    """Never creates a production copy implicitly; operator supplies both copies.

    Native backup preserves workbook features absent from the extracted snapshot.
    Installation stops in applying/maintenance until native verification succeeds.
    """
    if confirmed is not True or maintenance_confirmed is not True:
        raise MigrationError("Confirm installation and suspend bot/manual writes first.")
    if target.id != plan.spreadsheet_id or native_backup.id == target.id:
        raise MigrationError("Target and backup must be different, explicitly selected spreadsheets.")
    if fingerprint(source) != plan.source_digest or fingerprint(plan.output) != plan.plan_digest:
        raise MigrationError("Migration source or plan changed.")
    if not original_script.is_file() or not original_script.read_bytes():
        raise MigrationError("Original Apps Script backup is required.")
    state_path = state_directory / "native-install.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state["plan_digest"] != plan.plan_digest or state["target_id"] != target.id:
            raise MigrationError("Native resume belongs to another plan or target.")
    else:
        if fingerprint(read_google_spreadsheet(target)) != plan.source_digest:
            raise MigrationError("Native target differs from the reviewed source.")
        if fingerprint(read_google_spreadsheet(native_backup)) != plan.source_digest:
            raise MigrationError("Native backup differs from the reviewed source.")
        state = {"plan_digest": plan.plan_digest, "target_id": target.id,
                 "backup_id": native_backup.id, "completed": [],
                 "source": snapshot_payload(source),
                 "script": original_script.read_text(encoding="utf-8")}
        _private_json(state_path, state)
    # Verify original fields before each resume. Service fields may already exist.
    current = read_google_spreadsheet(target)
    for name, sheet in source.sheets.items():
        if name in {"Аналитика", "Категории"}:
            continue
        observed = current.sheets.get(name)
        if observed is None:
            raise MigrationError("An original sheet disappeared during installation.")
        for key, value in sheet.cells.items():
            if observed.cells.get(key, "") != value:
                raise MigrationError("Original data changed; native installation stopped.")
    for name in [METADATA_SHEET, *[key for key in plan.output.sheets if key != METADATA_SHEET]]:
        if name in state["completed"]:
            continue
        # Existing archives/legacy categories remain untouched.
        if name not in source.sheets or name in {"Траты", "Аналитика", "Категории"}:
            _write_sheet(target, plan.output.sheets[name])
        state["completed"].append(name)
        _private_json(state_path, state)
    if not state.get("dependent_dropdown_columns_prepared"):
        table_metadata = target.fetch_sheet_metadata(params={
            "fields": "sheets(properties(sheetId,title),tables)",
        })
        requests = ledger_dropdown_table_updates(table_metadata)
        if requests:
            state["original_table_metadata"] = table_metadata
            _private_json(state_path, state)
            target.batch_update({"requests": requests})
        state["dependent_dropdown_columns_prepared"] = True
        _private_json(state_path, state)
    if not state.get("dashboard_presentation_installed"):
        from finance_bot.category_projections import LABEL_SHEET
        from finance_bot.report_presentation import apply_report_presentation
        apply_report_presentation(target, plan.output.sheets["Аналитика"], sheet_rows(plan.output.sheets[LABEL_SHEET]))
        target.batch_update({"requests": [{"updateDimensionProperties": {
            "range": {"sheetId": target.worksheet("Траты").id, "dimension": "COLUMNS", "startIndex": 6, "endIndex": 11},
            "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}}]})
        state["dashboard_presentation_installed"] = True
        _private_json(state_path, state)
    # Service columns and registries need protection before manual editing resumes.
    worksheets = {sheet.title: sheet for sheet in target.worksheets()}
    requests = []
    for name, worksheet in worksheets.items():
        if name.startswith("_financebot_") or name == "Категории":
            requests.append({"addProtectedRange": {"protectedRange": {
                "range": {"sheetId": worksheet.id}, "description": "FinanceBot v2 service registry",
                "warningOnly": False,
            }}})
    ledger = worksheets["Траты"]
    requests.append({"addProtectedRange": {"protectedRange": {
        "range": {"sheetId": ledger.id, "startColumnIndex": 6, "endColumnIndex": 11},
        "description": "FinanceBot operation IDs", "warningOnly": False,
    }}})
    # The authenticated installing account can maintain service fields. Spreadsheet
    # owners can bypass Google protections and must observe the maintenance window.
    if not state.get("protections_installed"):
        target.batch_update({"requests": requests})
        state["protections_installed"] = True
        _private_json(state_path, state)


def verify_and_finalize_copy(
    source, plan: MigrationPlan, target: Any, script_proof: dict, *,
    confirmed: bool,
) -> None:
    """Read native computed values and publish ready/open as the final write."""
    if confirmed is not True or target.id != plan.spreadsheet_id:
        raise MigrationError("Confirm the exact target before finalization.")
    if (script_proof.get("spreadsheet_id") != target.id
            or script_proof.get("schema_version") != 2
            or script_proof.get("verified") is not True):
        raise MigrationError("Run verifyFinanceBotMigration in the installed Apps Script first.")
    current = read_google_spreadsheet(target)
    verify_migrated_copy(source, current)
    meta = WorkbookMetadata.from_rows(sheet_rows(current.sheets[METADATA_SHEET]))
    if script_proof.get("taxonomy_revision") != meta.taxonomy_revision:
        raise MigrationError("Apps Script proof refers to another taxonomy revision.")
    if meta.migration_state is not MigrationState.APPLYING or meta.access_mode is not AccessMode.MAINTENANCE:
        raise MigrationError("Unexpected migration state before finalization.")
    registry = parse_registry(meta, {name: sheet_rows(current.sheets[name]) for name in REGISTRY_HEADERS})
    ledger = reconcile_rows(sheet_rows(current.sheets["Траты"]), registry, {})
    require_valid(ledger)
    view = current.sheets["Аналитика"]
    period = Period(int(float(view.cell(2, 4))), int(float(view.cell(1, 4))))
    totals = calculate_totals(ledger, registry, period)
    if view.cell(1, 6) != "ready":
        raise MigrationError("Native formulas are not ready.")
    from decimal import Decimal
    expected = {(3, 2): totals.expenses, (4, 2): totals.income, (5, 2): totals.balance}
    for row in range(9, view.last_row + 1):
        category_id = view.cell(row, 4)
        if category_id:
            expected[row, 2] = totals.categories[category_id]
    for coordinate, value in expected.items():
        try:
            observed = Decimal(view.cell(*coordinate))
        except Exception as exc:
            raise MigrationError("A native formula result is missing or invalid.") from exc
        if not observed.is_finite() or abs(observed - value) > Decimal("0.005"):
            raise MigrationError("Native formula result differs from the reference calculation.")
    details = current.sheets[DETAIL_SHEET]
    for row in range(2, details.last_row + 1):
        from finance_bot.taxonomy import HistoricalPair
        pair = HistoricalPair(details.cell(row, 1), details.cell(row, 2))
        if not pair.category_id:
            continue
        value = totals.subcategories.get(pair, Decimal(0))
        observed = Decimal(details.cell(row, 5))
        if not observed.is_finite() or abs(observed - value) > Decimal("0.005"):
            raise MigrationError("Native subcategory totals differ.")
    # Recheck originals immediately before publishing ready, not only before sums.
    last = read_google_spreadsheet(target)
    if fingerprint(last) != fingerprint(current):
        raise MigrationError("Workbook changed during verification; do not publish ready.")
    ready = replace(meta, migration_state=MigrationState.READY, access_mode=AccessMode.OPEN)
    target.worksheet(METADATA_SHEET).update(values=ready.to_rows(), range_name="A1:B7", value_input_option="RAW")
