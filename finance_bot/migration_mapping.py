"""Explicit, fingerprint-bound mappings for legacy category labels."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from finance_bot.category_migration import WorkbookSnapshot, inspect_workbook
from finance_bot.taxonomy import (
    Category, Subcategory, Taxonomy, TaxonomyError, normalized_name,
)


class MappingError(ValueError):
    """Owner-reviewed correspondences are incomplete or no longer match the source."""


def _legacy_id(spreadsheet_id: str, *parts: str) -> str:
    # Initial migration IDs are stable for retries. Later renames keep these IDs;
    # they must never call this initial seeding routine on an existing v2 book.
    return str(uuid5(NAMESPACE_URL, "financebot:v2:" + "\0".join((spreadsheet_id, *parts))))


def seed_legacy_taxonomy(workbook: WorkbookSnapshot, spreadsheet_id: str) -> Taxonomy:
    report = inspect_workbook(workbook)
    if any(item["code"] in {"missing_sheet", "invalid_taxonomy_header", "invalid_taxonomy_row", "duplicate_taxonomy_pair"} for item in report["findings"]):
        raise MappingError("Fix the legacy registry findings before seeding IDs.")
    categories: dict[tuple[str, str], Category] = {}
    children = []
    for pair in report["taxonomy_pairs"]:
        kind = normalized_name(pair["type"])
        category_key = kind, normalized_name(pair["category"])
        if category_key not in categories:
            categories[category_key] = Category(
                _legacy_id(spreadsheet_id, "category", *category_key),
                "Доход" if kind == "доход" else "Расход", pair["category"],
            )
        parent = categories[category_key]
        children.append(Subcategory(
            _legacy_id(spreadsheet_id, "subcategory", *category_key, normalized_name(pair["subcategory"])),
            parent.category_id, pair["subcategory"],
        ))
    return Taxonomy(spreadsheet_id, 0, tuple(categories.values()), tuple(children))


@dataclass(frozen=True)
class LabelDecision:
    """A decision applies to exactly one cell in one reviewed source snapshot."""

    sheet: str
    cell: str
    category_id: str
    subcategory_id: str = ""


@dataclass(frozen=True)
class ReviewedMapping:
    source_fingerprint: str
    spreadsheet_id: str
    confirmed: bool
    decisions: tuple[LabelDecision, ...] = ()


@dataclass(frozen=True)
class ArchiveLabel:
    sheet: str
    cell: str
    kind: str
    category_id: str
    subcategory_id: str = ""


def compile_label_index(
    workbook: WorkbookSnapshot,
    taxonomy: Taxonomy,
    reviewed: ReviewedMapping,
) -> tuple[ArchiveLabel, ...]:
    """Build an identity index without changing labels or any financial values."""
    if (
        reviewed.confirmed is not True
        or reviewed.source_fingerprint != workbook.source_fingerprint
        or reviewed.spreadsheet_id != taxonomy.spreadsheet_id
    ):
        raise MappingError("Mapping is unconfirmed, stale or belongs to another spreadsheet.")
    report = inspect_workbook(workbook)
    decisions = {(item.sheet, item.cell): item for item in reviewed.decisions}
    if len(decisions) != len(reviewed.decisions):
        raise MappingError("Duplicate mapping decisions for one cell.")
    forced = {
        (item["sheet"], item["cell"])
        for item in report["findings"]
        if item["code"].startswith("unmapped_report_")
        or item["code"] == "unmapped_archive_category"
        or item["code"] == "report_label_differs_from_criterion"
    }
    labels = [*report["report_labels"], *[
        {**item, "sheet": "Архив", "kind": "category"}
        for item in report["archive_columns"]
    ]]
    result = []
    used = set()
    unresolved = []
    for label in labels:
        address = label["sheet"], label["cell"]
        decision = decisions.get(address)
        if decision is None and address in forced:
            unresolved.append(address)
            continue
        try:
            if decision:
                category = taxonomy.category(decision.category_id)
                if label["kind"] == "subcategory":
                    taxonomy.validate_pair(category.category_id, decision.subcategory_id, new_operation=False)
                elif decision.subcategory_id:
                    raise MappingError("A category label cannot point to a subcategory.")
                child_id = decision.subcategory_id
                used.add(address)
            else:
                kind = "Доход" if normalized_name(label["category"]) == "доход" else "Расход"
                candidates = [
                    item for item in taxonomy.categories
                    if item.transaction_type == kind
                    and normalized_name(item.name) == normalized_name(label["category"])
                ]
                if len(candidates) != 1:
                    unresolved.append(address)
                    continue
                category = candidates[0]
                child_id = ""
                if label["kind"] == "subcategory":
                    children = [
                        item for item in taxonomy.subcategories
                        if item.current_category_id == category.category_id
                        and normalized_name(item.name) == normalized_name(label["subcategory"])
                    ]
                    if len(children) != 1:
                        unresolved.append(address)
                        continue
                    child_id = children[0].subcategory_id
            result.append(ArchiveLabel(*address, label["kind"], category.category_id, child_id))
        except TaxonomyError as exc:
            raise MappingError("Mapping points to an unknown or incompatible entity.") from exc
    if unresolved:
        coordinates = ", ".join(f"{sheet}!{cell}" for sheet, cell in unresolved)
        raise MappingError("Owner mapping required for: " + coordinates)
    if decisions.keys() - used:
        raise MappingError("Mapping includes cells absent from the reviewed label inventory.")
    # Two archive headers mapped to one category would silently merge history.
    archive_ids = [item.category_id for item in result if item.sheet == "Архив"]
    if len(archive_ids) != len(set(archive_ids)):
        raise MappingError("Multiple archive columns map to one category; explicit merge is out of scope.")
    return tuple(result)
