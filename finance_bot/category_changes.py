"""Prepare and resume category mutations without a Telegram management UI."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from uuid import UUID, uuid5

from finance_bot.category_migration import WorkbookSnapshot, _coordinate, cell_address
from finance_bot.category_projections import (
    ARCHIVE_COLUMNS_SHEET, LABEL_HEADERS, LABEL_SHEET, SNAPSHOT_HEADERS, SNAPSHOT_SHEET,
    sheet_rows, table_sheet, taxonomy_view,
    evolve_analytics,
)
from finance_bot.category_schema import (
    AccessMode, METADATA_SHEET, MigrationState, REGISTRY_HEADERS,
    WorkbookMetadata, parse_registry, read_table,
)
from finance_bot.migration_apply import (
    MigrationError, _private_json, fingerprint, payload_snapshot,
    registry_sheets, snapshot_payload,
)
from finance_bot.taxonomy import Category, HistoricalPair, Period, Subcategory, Taxonomy, new_id


@dataclass(frozen=True)
class CategoryChange:
    request_id: str
    spreadsheet_id: str
    expected_revision: int
    source_digest: str
    target_digest: str
    output: WorkbookSnapshot
    presentation: dict | None = None
    presentation_digest: str = ""
    missing_archives: tuple[str, ...] = ()


def prepare_change(
    source: WorkbookSnapshot, *, spreadsheet_id: str, expected_revision: int,
    request_id: str, action: str, entity_id: str = "", name: str = "",
    parent_id: str = "", subcategories: tuple[str, ...] = (),
    transaction_type: str = "Расход", period: Period | None = None,
) -> CategoryChange:
    if str(UUID(request_id)) != request_id:
        raise ValueError("A mutation requires a UUID request ID.")
    meta = WorkbookMetadata.from_rows(sheet_rows(source.sheets[METADATA_SHEET]))
    meta.require_ready(spreadsheet_id, expected_revision=expected_revision)
    original = parse_registry(meta, {key: sheet_rows(source.sheets[key]) for key in REGISTRY_HEADERS})
    parents, children, history = list(original.categories), list(original.subcategories), list(original.historical_pairs)
    if action == "create":
        if parent_id:
            parent = original.category(parent_id)
            if not parent.active:
                raise ValueError("Cannot add a subcategory to an inactive parent.")
            children.append(Subcategory(str(uuid5(UUID(request_id), "subcategory")), parent_id, name))
        else:
            if not subcategories:
                raise ValueError("A new category must include subcategories.")
            parent = Category(str(uuid5(UUID(request_id), "category")), transaction_type, name)
            parents.append(parent)
            children.extend(Subcategory(str(uuid5(UUID(request_id), f"subcategory:{index}")), parent.category_id, label)
                            for index, label in enumerate(subcategories))
    elif action == "rename":
        if any(item.category_id == entity_id for item in parents):
            parents = [replace(item, name=name) if item.category_id == entity_id else item for item in parents]
        else:
            original.subcategory(entity_id)
            children = [replace(item, name=name) if item.subcategory_id == entity_id else item for item in children]
    elif action == "reactivate":
        if any(item.category_id == entity_id for item in parents):
            parent = original.category(entity_id)
            if parent.active:
                raise ValueError("Entity is already active.")
            parents = [replace(item, active=True, deactivated_period=None) if item.category_id == entity_id else item for item in parents]
            children = [replace(item, active=True) if item.current_category_id == entity_id else item for item in children]
        else:
            child = original.subcategory(entity_id)
            if child.active:
                raise ValueError("Entity is already active.")
            if not original.category(child.current_category_id).active:
                raise ValueError("Reactivate the parent category first.")
            children = [replace(item, active=True) if item.subcategory_id == entity_id else item for item in children]
    elif action == "move":
        child, parent = original.subcategory(entity_id), original.category(parent_id)
        old_parent = original.category(child.current_category_id)
        if not child.active or not parent.active or old_parent.transaction_type != parent.transaction_type:
            raise ValueError("Move requires active entities of the same transaction type.")
        pair = HistoricalPair(child.current_category_id, child.subcategory_id)
        if pair not in history:
            history.append(pair)
        children = [replace(item, current_category_id=parent_id) if item.subcategory_id == entity_id else item for item in children]
    elif action == "deactivate":
        affected = [child for child in children if child.subcategory_id == entity_id or child.current_category_id == entity_id]
        for child in affected:
            pair = HistoricalPair(child.current_category_id, child.subcategory_id)
            if pair not in history:
                history.append(pair)
        if any(item.category_id == entity_id for item in parents):
            if period is None:
                raise ValueError("A category deactivation requires a reporting period.")
            original.category(entity_id)
            parents = [replace(item, active=False, deactivated_period=period) if item.category_id == entity_id else item for item in parents]
            children = [replace(item, active=False) if item.current_category_id == entity_id else item for item in children]
        else:
            original.subcategory(entity_id)
            children = [replace(item, active=False) if item.subcategory_id == entity_id else item for item in children]
    else:
        raise ValueError("Unsupported category operation.")
    registry = Taxonomy(spreadsheet_id, expected_revision + 1, tuple(parents), tuple(children), tuple(history))
    output = deepcopy(source)
    output.sheets.update(registry_sheets(registry))
    output.sheets["Категории"] = taxonomy_view(registry)
    # Updating the current label never reassigns the operation's stored parent.
    ledger = output.sheets["Траты"]
    for number in range(2, ledger.last_row + 1):
        category_id, child_id = ledger.cell(number, 7), ledger.cell(number, 8)
        if category_id and child_id:
            registry.validate_pair(category_id, child_id, new_operation=False)
            ledger.cells[number, 2] = registry.category(category_id).name
            ledger.cells[number, 3] = registry.subcategory(child_id).name
    labels = read_table(sheet_rows(output.sheets[LABEL_SHEET]), LABEL_HEADERS)
    # Only rename changes historical labels. Other actions must not depend
    # on old snapshots that may no longer exist in the workbook.
    missing_archives = set()
    if action == "rename":
        for required in ("Аналитика", "Архив", *REGISTRY_HEADERS):
            if required not in output.sheets:
                raise ValueError("A required finance sheet is missing.")
        columns = read_table(sheet_rows(output.sheets[ARCHIVE_COLUMNS_SHEET]), ("category_id", "column"))
        ids, positions = set(), set()
        for entry in columns:
            key = str(entry["category_id"])
            number = str(entry["column"])
            if not number.isdecimal() or int(number) <= 4:
                raise ValueError("Invalid archive column mapping.")
            column = int(number)
            if key in ids or column in positions or column > max((col for _, col in output.sheets["Архив"].cells), default=0):
                raise ValueError("Ambiguous archive column mapping.")
            parent = registry.category(key)
            if parent.transaction_type != "Расход":
                raise ValueError("Invalid archive column mapping.")
            ids.add(key); positions.add(column)
            address = cell_address(1, column)
            matching = [item for item in labels if item["sheet"] == "Архив" and item["cell"] == address]
            if any(item["kind"] != "category" or item["category_id"] != key for item in matching):
                raise ValueError("Conflicting archive label mapping.")
            if not matching:
                labels.append(dict(zip(LABEL_HEADERS, ("Архив", address, "category", key, ""))))
        output.sheets[LABEL_SHEET] = table_sheet(LABEL_SHEET, [LABEL_HEADERS, *[
            [item[key] for key in LABEL_HEADERS] for item in labels]])
        for item in labels:
            sheet = output.sheets.get(str(item["sheet"]))
            if sheet is None:
                title = str(item["sheet"])
                if title.startswith("Архив (") and title.endswith(")") and title[7:-1].strip():
                    missing_archives.add(title)
                    continue
                raise ValueError("An indexed archive sheet is missing.")
            key = _coordinate(str(item["cell"]))
            parent = registry.category(str(item["category_id"]))
            if item["kind"] == "subcategory":
                child = registry.subcategory(str(item["subcategory_id"]))
                value = child.name
            elif item["kind"] == "heading":
                value = "Подкатегории: " + parent.name
            elif item["kind"] == "category":
                value = parent.name
            else:
                raise ValueError("Unknown indexed label kind.")
            if key in sheet.formulas:
                raise ValueError("An archive label unexpectedly contains a formula.")
            sheet.cells[key] = value
    # Keep trusted row snapshots in sync with a global rename.
    snapshots = read_table(sheet_rows(output.sheets[SNAPSHOT_SHEET]), SNAPSHOT_HEADERS)
    for item in snapshots:
        item["category_label"] = registry.category(str(item["category_id"])).name
        item["subcategory_label"] = registry.subcategory(str(item["subcategory_id"])).name
    output.sheets[SNAPSHOT_SHEET] = table_sheet(SNAPSHOT_SHEET, [
        SNAPSHOT_HEADERS, *[[item[key] for key in SNAPSHOT_HEADERS] for item in snapshots],
    ])
    analytics = source.sheets["Аналитика"]
    current_period = Period(int(float(analytics.cell(2, 4))), int(float(analytics.cell(1, 4))))
    for projection in evolve_analytics(output, registry, current_period):
        output.sheets[projection.title] = projection
    final_meta = replace(meta, taxonomy_revision=registry.revision)
    output.sheets[METADATA_SHEET] = table_sheet(METADATA_SHEET, final_meta.to_rows())
    return CategoryChange(request_id, spreadsheet_id, expected_revision,
                          fingerprint(source), fingerprint(output), output, missing_archives=tuple(sorted(missing_archives)))


def apply_change_to_copy(
    source: WorkbookSnapshot, change: CategoryChange, directory: Path, *,
    confirmed: bool, checkpoint: Callable[[str], None] = lambda _: None,
) -> WorkbookSnapshot:
    """Durable local execution, including pending-operation state and recovery.

    A server writer must use this same plan in the maintenance window. This
    function does not connect to Google or install an Apps Script project.
    """
    if confirmed is not True or fingerprint(source) != change.source_digest:
        raise MigrationError("Confirm the exact category plan against its source.")
    if fingerprint(change.output) != change.target_digest:
        raise MigrationError("The category plan was modified.")
    meta = WorkbookMetadata.from_rows(sheet_rows(source.sheets[METADATA_SHEET]))
    meta.require_ready(change.spreadsheet_id, expected_revision=change.expected_revision)
    path = directory / (change.request_id + ".json")
    if path.exists():
        import json
        state = json.loads(path.read_text())
        if state["target_digest"] != change.target_digest or state["source_digest"] != change.source_digest:
            raise MigrationError("A request ID cannot be reused for a different plan.")
        result = payload_snapshot(state["workbook"])
        done = state["completed"]
        if state["finished"]:
            return result
    else:
        result, done = deepcopy(source), []
        pending = replace(meta, access_mode=AccessMode.MAINTENANCE,
                          pending_request_id=change.request_id)
        result.sheets[METADATA_SHEET] = table_sheet(METADATA_SHEET, pending.to_rows())
    def save(finished=False):
        _private_json(path, {"source_digest": change.source_digest,
            "target_digest": change.target_digest, "completed": done,
            "finished": finished, "workbook": snapshot_payload(result)})
    save()
    checkpoint("pending")
    for name, sheet in change.output.sheets.items():
        if name == METADATA_SHEET or name in done:
            continue
        result.sheets[name] = deepcopy(sheet)
        done.append(name)
        save()
        checkpoint(name)
    # Publish the new revision and ready/open state only after all projections.
    result.sheets[METADATA_SHEET] = deepcopy(change.output.sheets[METADATA_SHEET])
    if fingerprint(result) != change.target_digest:
        raise MigrationError("Category projections failed verification.")
    save(finished=True)
    checkpoint("complete")
    return result
