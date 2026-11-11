from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from finance_bot.category_schema import (
    AccessMode, CATEGORY_SHEET, HISTORY_SHEET, METADATA_SHEET, MigrationState,
    REGISTRY_HEADERS, RegistryReader, SchemaError, SUBCATEGORY_SHEET,
    WorkbookMetadata, parse_registry, read_table,
)
from finance_bot.taxonomy import new_id


class CategorySchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.meta = WorkbookMetadata("sheet-a", 2, 1, MigrationState.READY, AccessMode.OPEN)
        self.category_id, self.child_id = new_id(), new_id()
        self.tables = {
            CATEGORY_SHEET: [list(REGISTRY_HEADERS[CATEGORY_SHEET]), [self.category_id, "Расход", "Продукты", "TRUE", ""]],
            SUBCATEGORY_SHEET: [list(REGISTRY_HEADERS[SUBCATEGORY_SHEET]), [self.child_id, self.category_id, "Магазин", "TRUE"]],
            HISTORY_SHEET: [list(REGISTRY_HEADERS[HISTORY_SHEET])],
        }

    def spreadsheet(self, scope: str):
        worksheets = {name: SimpleNamespace(get_all_values=Mock(return_value=rows)) for name, rows in self.tables.items()}
        worksheets[METADATA_SHEET] = SimpleNamespace(get_all_values=Mock(return_value=replace(self.meta, spreadsheet_id=scope).to_rows()))
        return SimpleNamespace(id=scope, worksheet=lambda name: worksheets[name]), worksheets

    def test_metadata_roundtrip_and_gate(self) -> None:
        self.assertEqual(WorkbookMetadata.from_rows(self.meta.to_rows()), self.meta)
        self.meta.require_ready("sheet-a", expected_revision=1)
        for state in MigrationState:
            if state is MigrationState.READY:
                continue
            with self.subTest(state=state), self.assertRaises(SchemaError):
                replace(self.meta, migration_state=state).require_ready("sheet-a")
        for meta in (replace(self.meta, access_mode=AccessMode.MAINTENANCE), replace(self.meta, pending_request_id="unfinished")):
            with self.assertRaises(SchemaError):
                meta.require_ready("sheet-a")
        for scope, revision in (("sheet-b", 1), ("sheet-a", 0), ("sheet-a", True)):
            with self.assertRaises(SchemaError):
                self.meta.require_ready(scope, expected_revision=revision)

    def test_corrupt_or_missing_metadata_does_not_default_to_ready(self) -> None:
        rows = self.meta.to_rows()
        invalid = [[], rows[:-1], rows + [rows[1]], [["key", "value"], ["schema_version", True]]]
        for field, value in (("schema_version", "1"), ("migration_state", "unknown"), ("access_mode", ""), ("taxonomy_revision", "-1"), ("taxonomy_revision", "1.5")):
            invalid.append([[row[0], value if row[0] == field else row[1]] for row in rows])
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(SchemaError):
                WorkbookMetadata.from_rows(data)

    def test_reordered_headers_and_false_are_read_correctly(self) -> None:
        tables = dict(self.tables)
        tables[SUBCATEGORY_SHEET] = [
            ["active", "name", "current_category_id", "subcategory_id"],
            ["FALSE", "Магазин", self.category_id, self.child_id],
        ]
        registry = parse_registry(self.meta, tables)
        self.assertEqual(registry.active_pairs(), ())
        self.assertEqual(registry.subcategories[0].subcategory_id, self.child_id)
        for rows in ([['a', 'a'], [1, 2]], [['a'], [1, 2]], [['a', '']]):
            with self.assertRaises(SchemaError):
                read_table(rows, ["a"])

    def test_dangling_ids_and_invalid_activity_are_rejected(self) -> None:
        for child in ([self.child_id, new_id(), "Магазин", "TRUE"], [self.child_id, self.category_id, "Магазин", "yes"]):
            with self.subTest(child=child), self.assertRaises(SchemaError):
                parse_registry(self.meta, {**self.tables, SUBCATEGORY_SHEET: [list(REGISTRY_HEADERS[SUBCATEGORY_SHEET]), child]})

    def test_readers_do_not_share_spreadsheet_state(self) -> None:
        a, _ = self.spreadsheet("sheet-a")
        b, _ = self.spreadsheet("sheet-b")
        self.assertEqual(RegistryReader(a, "sheet-a").load().spreadsheet_id, "sheet-a")
        self.assertEqual(RegistryReader(b, "sheet-b").load().spreadsheet_id, "sheet-b")
        with self.assertRaises(SchemaError):
            RegistryReader(a, "sheet-b").load()

    def test_revision_changed_during_load_is_rejected(self) -> None:
        book, worksheets = self.spreadsheet("sheet-a")
        worksheets[METADATA_SHEET].get_all_values.side_effect = [self.meta.to_rows(), replace(self.meta, taxonomy_revision=2).to_rows()]
        with self.assertRaises(SchemaError):
            RegistryReader(book, "sheet-a").load()

    def test_incomplete_workbook_never_reads_registry(self) -> None:
        book, worksheets = self.spreadsheet("sheet-a")
        worksheets[METADATA_SHEET].get_all_values.return_value = replace(self.meta, migration_state=MigrationState.APPLYING).to_rows()
        with self.assertRaises(SchemaError):
            RegistryReader(book, "sheet-a").load()
        worksheets[CATEGORY_SHEET].get_all_values.assert_not_called()

    def test_native_batch_preserves_revision_gate_and_uses_three_reads(self):
        meta, tables = self.meta, self.tables
        class BatchBook:
            id = 'sheet-a'
            def __init__(self):
                self.reads, self.changed, self.incomplete = [], False, False
            def values_get(self, name, params):
                self.reads.append(name)
                value = replace(meta, migration_state=MigrationState.APPLYING) if self.incomplete else meta
                if self.changed and len(self.reads) == 3:
                    value = replace(meta, taxonomy_revision=2)
                return {'values': value.to_rows()}
            def values_batch_get(self, ranges, params):
                self.reads.append(ranges)
                return {'valueRanges': [{'values': tables[name]} for name in REGISTRY_HEADERS]}
        book = BatchBook()
        self.assertEqual(RegistryReader(book, 'sheet-a').load().revision, 1)
        self.assertEqual(len(book.reads), 3)
        book = BatchBook();book.changed = True
        with self.assertRaises(SchemaError):
            RegistryReader(book, 'sheet-a').load()
        book = BatchBook();book.incomplete = True
        with self.assertRaises(SchemaError):
            RegistryReader(book, 'sheet-a').load()
        self.assertEqual(len(book.reads), 1)


if __name__ == "__main__":
    unittest.main()
