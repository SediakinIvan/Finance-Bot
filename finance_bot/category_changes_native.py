"""Apply a prepared category change with a durable maintenance journal."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from finance_bot.category_changes import CategoryChange
from finance_bot.category_migration import cell_address, read_google_spreadsheet
from finance_bot.category_projections import DETAIL_SHEET, sheet_rows, table_sheet
from finance_bot.category_schema import AccessMode, METADATA_SHEET, WorkbookMetadata
from finance_bot.migration_apply import MigrationError, _private_json, fingerprint, payload_snapshot, snapshot_payload
from finance_bot.native_migration import _quote, _write_sheet, native_formula


def apply_change_to_google_copy(
    source, change: CategoryChange, target, directory: Path, *,
    confirmed: bool, maintenance_confirmed: bool,
) -> None:
    if confirmed is not True or maintenance_confirmed is not True or target.id != change.spreadsheet_id:
        raise MigrationError("Confirm the exact category plan and suspend manual writes.")
    if fingerprint(source) != change.source_digest or fingerprint(change.output) != change.target_digest:
        raise MigrationError("Category source or plan was modified.")
    from finance_bot.dashboard_changes import validate_presentation, apply_presentation
    path = directory / (change.request_id + "-native.json")
    current = read_google_spreadsheet(target)
    if path.exists():
        state = json.loads(path.read_text())
        if (state["target_digest"] != change.target_digest or state["source_digest"] != change.source_digest
                or state.get("presentation_digest") != change.presentation_digest):
            raise MigrationError("Request ID already belongs to another plan.")
        if state["finished"]:
            meta = WorkbookMetadata.from_rows(sheet_rows(current.sheets[METADATA_SHEET]))
            meta.require_ready(target.id, expected_revision=change.expected_revision + 1)
            return
    else:
        validate_presentation(target, change.presentation, change.presentation_digest)
        if fingerprint(current) != change.source_digest:
            raise MigrationError("Workbook changed since category preview.")
        meta = WorkbookMetadata.from_rows(sheet_rows(current.sheets[METADATA_SHEET]))
        meta.require_ready(target.id, expected_revision=change.expected_revision)
        state = {"source_digest": change.source_digest, "target_digest": change.target_digest,
                 "presentation_digest": change.presentation_digest,
                 "completed": [], "finished": False, "source": snapshot_payload(source)}
        _private_json(path, state)
    validate_presentation(target, change.presentation, change.presentation_digest, resume=True)
    from dataclasses import replace
    meta = WorkbookMetadata.from_rows(sheet_rows(source.sheets[METADATA_SHEET]))
    pending = replace(meta, access_mode=AccessMode.MAINTENANCE, pending_request_id=change.request_id)
    if "pending" not in state["completed"]:
        _write_sheet(target, table_sheet(METADATA_SHEET, pending.to_rows()))
        state["completed"].append("pending")
        _private_json(path, state)
    for name, expected in change.output.sheets.items():
        if name == METADATA_SHEET or name in state["completed"]:
            continue
        if source.sheets.get(name) == expected:
            # No remote mutation requires no per-sheet read/write cycle. The
            # complete final readback below still verifies untouched sheets.
            state["completed"].append(name)
            _private_json(path, state)
            continue
        observed = read_google_spreadsheet(target)
        live_meta = WorkbookMetadata.from_rows(sheet_rows(observed.sheets[METADATA_SHEET]))
        if live_meta.pending_request_id != change.request_id or live_meta.taxonomy_revision != change.expected_revision:
            raise MigrationError("Pending category operation changed externally.")
        # Financial values must stay unchanged through every rename/move step.
        for title, original in source.sheets.items():
            if title != "Траты" and title != "Архив" and not title.startswith("Архив ("):
                continue
            actual = observed.sheets.get(title)
            if actual is None:
                raise MigrationError("An original financial sheet disappeared.")
            for key, value in original.cells.items():
                # Names are allowed to change; financial values and dates are not.
                if title == "Траты" and key[0] > 1 and key[1] not in {2, 3}:
                    if actual.cells.get(key, "") != value:
                        raise MigrationError("A financial operation changed during maintenance.")
                elif title != "Траты" and isinstance(value, (int, float)):
                    if actual.cells.get(key, "") != value:
                        raise MigrationError("An archive amount changed during maintenance.")
        if name == "Аналитика":
            original = source.sheets[name]
            raw, formulas = [], []
            for key in sorted(set(original.cells) | set(expected.cells) | set(original.formulas) | set(expected.formulas)):
                if key in expected.formulas:
                    if original.formulas.get(key) != expected.formulas[key]:
                        formulas.append({"range": _quote(name) + cell_address(*key),
                                         "values": [[native_formula(expected.formulas[key], target)]]})
                elif original.cells.get(key, "") != expected.cells.get(key, "") or key in original.formulas:
                    raw.append({"range": _quote(name) + cell_address(*key), "values": [[expected.cells.get(key, "")]]})
            for option, changes in (("RAW", raw), ("USER_ENTERED", formulas)):
                if changes:
                    target.values_batch_update({"valueInputOption": option, "data": changes})
        elif name.startswith("_financebot_") or name == "Категории":
            _write_sheet(target, expected)
        else:
            original = source.sheets.get(name)
            changes = []
            for key, value in expected.cells.items():
                if original is None or original.cells.get(key, "") != value:
                    # Only changed labels: never clear/rewrite the whole archive.
                    changes.append({"range": _quote(name) + cell_address(*key), "values": [[value]]})
            if changes:
                target.values_batch_update({"valueInputOption": "RAW", "data": changes})
        state["completed"].append(name)
        _private_json(path, state)
    apply_presentation(target, change.presentation, change.presentation_digest)
    from finance_bot.category_dropdowns import apply_category_dropdowns
    apply_category_dropdowns(target, change.output)
    # Check every cell this plan changes. Formula values require native Sheets
    # recalculation, but formula texts and all identity/label fields are exact.
    actual = read_google_spreadsheet(target)
    for name, expected in change.output.sheets.items():
        if name == METADATA_SHEET:
            continue
        observed = actual.sheets[name]
        for key, value in expected.cells.items():
            if key in expected.formulas:
                continue
            if observed.cells.get(key, "") != value:
                raise MigrationError("Category projection readback failed.")
        if {key: native_formula(value, target) for key, value in expected.formulas.items()} != observed.formulas:
            raise MigrationError("Category formula projection readback failed.")
        if any(value not in ("", None) and key not in expected.cells and key not in expected.formulas
               for key, value in observed.cells.items()):
            raise MigrationError("Unexpected values appeared during category maintenance.")
    _write_sheet(target, change.output.sheets[METADATA_SHEET])
    state["finished"] = True
    _private_json(path, state)
