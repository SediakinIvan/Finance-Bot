"""Resumable migration plans, private backups and verification on a copy."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Callable
from uuid import NAMESPACE_URL, uuid5, uuid4

from finance_bot.category_migration import SheetSnapshot, WorkbookSnapshot, inspect_workbook
from finance_bot.category_projections import (
    ARCHIVE_COLUMNS_SHEET, LABEL_HEADERS, LABEL_SHEET, SNAPSHOT_HEADERS,
    SNAPSHOT_SHEET, analytics_sheets, sheet_rows, table_sheet, taxonomy_view,
    preferred_report_categories, report_label_rows,
)
from finance_bot.category_schema import (
    AccessMode, CATEGORY_SHEET, HISTORY_SHEET, METADATA_SHEET, MigrationState,
    REGISTRY_HEADERS, SUBCATEGORY_SHEET, TRANSACTION_HEADERS, WorkbookMetadata,
    parse_registry,
)
from finance_bot.ledger import LedgerRow, reconcile_rows, require_valid
from finance_bot.migration_mapping import ReviewedMapping, compile_label_index
from finance_bot.taxonomy import Period, Taxonomy


class MigrationError(ValueError):
    pass


def snapshot_payload(snapshot: WorkbookSnapshot) -> dict:
    return {
        "source_fingerprint": snapshot.source_fingerprint,
        "limitations": snapshot.limitations,
        "sheets": {
            name: {"cells": [[*key, value] for key, value in sorted(sheet.cells.items())],
                   "formulas": [[*key, value] for key, value in sorted(sheet.formulas.items())],
                   "validations": sheet.validations, "protected_ranges": sheet.protected_ranges}
            for name, sheet in sorted(snapshot.sheets.items())
        },
    }


def payload_snapshot(payload: dict) -> WorkbookSnapshot:
    return WorkbookSnapshot(payload["source_fingerprint"], {
        name: SheetSnapshot(name,
            {(r, c): value for r, c, value in sheet["cells"]},
            {(r, c): value for r, c, value in sheet["formulas"]},
            sheet["validations"], sheet["protected_ranges"])
        for name, sheet in payload["sheets"].items()
    }, tuple(payload["limitations"]))


def fingerprint(snapshot: WorkbookSnapshot) -> str:
    payload = snapshot_payload(snapshot)
    payload.pop("source_fingerprint")
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _private_json(path: Path, value: dict) -> None:
    """Atomic local state. Backup paths must be outside the public repository."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True)
class MigrationPlan:
    spreadsheet_id: str
    source_fingerprint: str
    source_digest: str
    plan_digest: str
    output: WorkbookSnapshot


def registry_sheets(taxonomy: Taxonomy) -> dict[str, SheetSnapshot]:
    return {
        CATEGORY_SHEET: table_sheet(CATEGORY_SHEET, [REGISTRY_HEADERS[CATEGORY_SHEET], *[
            [item.category_id, item.transaction_type, item.name, item.active,
             str(item.deactivated_period) if item.deactivated_period else ""]
            for item in taxonomy.categories
        ]]),
        SUBCATEGORY_SHEET: table_sheet(SUBCATEGORY_SHEET, [REGISTRY_HEADERS[SUBCATEGORY_SHEET], *[
            [item.subcategory_id, item.current_category_id, item.name, item.active]
            for item in taxonomy.subcategories
        ]]),
        HISTORY_SHEET: table_sheet(HISTORY_SHEET, [REGISTRY_HEADERS[HISTORY_SHEET], *[
            [item.category_id, item.subcategory_id] for item in taxonomy.historical_pairs
        ]]),
    }


def prepare_migration(
    source: WorkbookSnapshot, taxonomy: Taxonomy, reviewed: ReviewedMapping,
) -> MigrationPlan:
    """No mutation. All conflicting historical identities must already be reviewed."""
    if METADATA_SHEET in source.sheets:
        raise MigrationError("This source already has migration metadata; resume the saved plan.")
    index = compile_label_index(source, taxonomy, reviewed)
    report = inspect_workbook(source)
    blockers = [item for item in report["findings"] if item["code"] in {
        "missing_sheet", "invalid_transaction_header", "missing_transaction_date",
        "unmapped_transaction_pair", "unsupported_legacy_type",
    }]
    if blockers:
        raise MigrationError("Resolve legacy operation findings before applying migration.")
    output = deepcopy(source)
    output.sheets.update(registry_sheets(taxonomy))
    output.sheets["Категории"] = taxonomy_view(taxonomy)
    ledger = output.sheets["Траты"]
    # Refuse to overwrite any pre-existing custom columns beyond the legacy A:F.
    if any(column > 6 and value not in ("", None) for (_, column), value in ledger.cells.items()):
        raise MigrationError("Ledger has custom columns; explicit layout migration is required.")
    for column, name in enumerate(TRANSACTION_HEADERS, 1):
        ledger.cells[1, column] = name
    for row in range(2, ledger.last_row + 1):
        if any(ledger.cell(row, column) for column in range(1, 7)):
            ledger.cells[row, 9] = str(uuid5(NAMESPACE_URL, f"financebot:{taxonomy.spreadsheet_id}:{source.source_fingerprint}:row:{row}"))
    rows = reconcile_rows(sheet_rows(ledger), taxonomy, {})
    require_valid(rows)
    for row in rows:
        for column, value in enumerate(row.service_values(), 7):
            ledger.cells[row.row_number, column] = value
    output.sheets[SNAPSHOT_SHEET] = table_sheet(SNAPSHOT_SHEET, [SNAPSHOT_HEADERS, *[
        list(asdict(row.identity()).values()) for row in rows if row.state
    ]])
    legacy_labels = [LABEL_HEADERS, *[
        list(asdict(item).values()) for item in index if item.sheet != "Аналитика"
    ]]
    preferred = preferred_report_categories([LABEL_HEADERS, *[list(asdict(item).values()) for item in index]])
    output.sheets[LABEL_SHEET] = table_sheet(LABEL_SHEET, [*legacy_labels, *report_label_rows(taxonomy, preferred)])
    archive_columns = [["category_id", "column"]]
    for item in index:
        if item.sheet == "Архив":
            column = 0
            for letter in item.cell.rstrip("0123456789"):
                column = column * 26 + ord(letter) - 64
            archive_columns.append([item.category_id, column])
    output.sheets[ARCHIVE_COLUMNS_SHEET] = table_sheet(ARCHIVE_COLUMNS_SHEET, archive_columns)
    analytics = source.sheets["Аналитика"]
    try:
        period = Period(int(float(analytics.cell(2, 4))), int(float(analytics.cell(1, 4))))
    except (ValueError, OverflowError) as exc:
        raise MigrationError("Invalid selected reporting period.") from exc
    for projection in analytics_sheets(taxonomy, period, preferred_categories=preferred):
        output.sheets[projection.title] = projection
    metadata = WorkbookMetadata(taxonomy.spreadsheet_id, 2, taxonomy.revision,
                                MigrationState.APPLYING, AccessMode.MAINTENANCE)
    output.sheets[METADATA_SHEET] = table_sheet(METADATA_SHEET, metadata.to_rows())
    digest = fingerprint(output)
    return MigrationPlan(taxonomy.spreadsheet_id, source.source_fingerprint,
                         fingerprint(source), digest, output)


def verify_migrated_copy(source: WorkbookSnapshot, output: WorkbookSnapshot) -> None:
    """Check that migration preserves financial source fields and archive values."""
    for name, original in source.sheets.items():
        if name in {"Аналитика", "Категории"}:
            continue  # It is a generated view; compare totals separately in Sheets.
        current = output.sheets.get(name)
        if current is None:
            raise MigrationError("An original sheet disappeared.")
        for coordinate, value in original.cells.items():
            if name == "Траты" and coordinate[1] > 6:
                continue
            if current.cells.get(coordinate, "") != value:
                raise MigrationError("Original financial fields or archive values changed.")
        if original.formulas != current.formulas:
            raise MigrationError("Original formulas outside generated analytics changed.")
        if name == "Траты":
            for coordinate, value in current.cells.items():
                if coordinate[1] <= 6 and value not in ("", None) and original.cells.get(coordinate, "") != value:
                    raise MigrationError("Unexpected financial source values appeared.")
    meta = WorkbookMetadata.from_rows(sheet_rows(output.sheets[METADATA_SHEET]))
    registry = parse_registry(meta, {name: sheet_rows(output.sheets[name]) for name in REGISTRY_HEADERS})
    rows = sheet_rows(output.sheets["Траты"])
    reconciled = reconcile_rows(rows, registry, {})
    require_valid(reconciled)
    for row in reconciled:
        if row.state and list(rows[row.row_number - 1][6:11]) != row.service_values():
            raise MigrationError("Stored operation identities failed verification.")


def apply_to_copy(
    source: WorkbookSnapshot, plan: MigrationPlan, directory: Path,
    script_source: Path, *, confirmed: bool,
    checkpoint: Callable[[str], None] = lambda _: None,
) -> WorkbookSnapshot:
    """Apply to a private local snapshot copy, with durable per-sheet recovery.

    This is not installation in Google Sheets. A native copy and Apps Script
    verification are separately required before a live workbook can be ready.
    """
    if confirmed is not True or fingerprint(source) != plan.source_digest:
        raise MigrationError("Confirm the reviewed plan against the unchanged source.")
    if fingerprint(plan.output) != plan.plan_digest:
        raise MigrationError("Saved migration plan was modified.")
    if not script_source.is_file() or not script_source.read_bytes():
        raise MigrationError("The original Apps Script source is required for backup.")
    directory.mkdir(parents=True, exist_ok=True)
    backup_path, state_path = directory / "backup.json", directory / "state.json"
    if backup_path.exists():
        backup = json.loads(backup_path.read_text())
        if backup["source_digest"] != plan.source_digest or backup["plan_digest"] != plan.plan_digest:
            raise MigrationError("This backup belongs to another source or plan.")
    else:
        _private_json(backup_path, {
            "source_digest": plan.source_digest, "plan_digest": plan.plan_digest,
            "spreadsheet_id": plan.spreadsheet_id, "workbook": snapshot_payload(source),
            "script": script_source.read_text(encoding="utf-8"),
        })
        checkpoint("backup")
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state["plan_digest"] != plan.plan_digest:
            raise MigrationError("Cannot resume another migration plan.")
        result = payload_snapshot(state["workbook"])
        completed = state["completed"]
    else:
        result, completed = deepcopy(source), []
    # Metadata is written first and never made ready by structural migration.
    order = [METADATA_SHEET, *[name for name in plan.output.sheets if name != METADATA_SHEET]]
    for name in order:
        if name in completed:
            continue
        result.sheets[name] = deepcopy(plan.output.sheets[name])
        completed.append(name)
        _private_json(state_path, {"plan_digest": plan.plan_digest,
            "completed": completed, "workbook": snapshot_payload(result)})
        checkpoint(name)
    verify_migrated_copy(source, result)
    _private_json(directory / "verified-copy.json", snapshot_payload(result))
    checkpoint("verified")
    return result


def restore_copy(directory: Path, *, confirmed: bool) -> tuple[WorkbookSnapshot, str]:
    if confirmed is not True:
        raise MigrationError("Restoration requires confirmation.")
    payload = json.loads((directory / "backup.json").read_text())
    source = payload_snapshot(payload["workbook"])
    if fingerprint(source) != payload["source_digest"]:
        raise MigrationError("Backup checksum does not match.")
    _private_json(directory / "restored-copy.json", snapshot_payload(source))
    script_path = directory / "restored-script.gs"
    if script_path.exists() and script_path.read_text(encoding="utf-8") != payload["script"]:
        raise MigrationError("A different restored script already exists.")
    if not script_path.exists():
        descriptor = os.open(script_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload["script"])
    return source, payload["script"]
