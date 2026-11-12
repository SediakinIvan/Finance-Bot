from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from finance_bot.category_changes import apply_change_to_copy, prepare_change
from finance_bot.category_projections import LABEL_SHEET, SNAPSHOT_SHEET, sheet_rows, table_sheet
from finance_bot.category_schema import AccessMode, METADATA_SHEET, MigrationState, REGISTRY_HEADERS, WorkbookMetadata, parse_registry
from finance_bot.migration_apply import fingerprint, prepare_migration
from finance_bot.taxonomy import Period, new_id
from test_migration_apply import migration_fixture


class CategoryChangesTests(unittest.TestCase):
    def setUp(self):
        source, registry, reviewed = migration_fixture()
        self.book = prepare_migration(source, registry, reviewed).output
        self.book.sheets[METADATA_SHEET] = table_sheet(METADATA_SHEET, WorkbookMetadata("copy-id", 2, 0, MigrationState.READY, AccessMode.OPEN).to_rows())
        self.parent = registry.categories[0]
        self.child = registry.subcategories[0]

    def plan(self, action, **kwargs):
        return prepare_change(self.book, spreadsheet_id="copy-id", expected_revision=0, request_id=new_id(), action=action, **kwargs)

    def test_rename_updates_labels_and_operations_without_changing_amounts(self):
        change = self.plan("rename", entity_id=self.parent.category_id, name="Покупки")
        self.assertEqual(change.output.sheets["Траты"].cells[2, 2], "Покупки")
        self.assertEqual(change.output.sheets["Траты"].cells[2, 4], 100)
        self.assertEqual(change.output.sheets["Архив"].cells[1, 5], "Покупки")
        self.assertEqual(change.output.sheets["Архив"].cells[2, 5], 900)
        self.assertEqual(change.output.sheets["Траты"].cells[2, 7], self.parent.category_id)
        self.assertEqual(self.book.sheets["Траты"].cells[2, 2], "Продукты")

    def test_create_does_not_reuse_ids_and_rejects_ambiguous_names(self):
        change = self.plan("create", name="Новая", subcategories=("Первая", "Вторая"))
        meta = WorkbookMetadata.from_rows(sheet_rows(change.output.sheets[METADATA_SHEET]))
        registry = parse_registry(meta, {key: sheet_rows(change.output.sheets[key]) for key in REGISTRY_HEADERS})
        self.assertEqual(len(registry.categories), 3)
        self.assertEqual(len(registry.subcategories), 4)
        with self.assertRaises(ValueError):
            self.plan("create", name=" продукты ", subcategories=("Первая",))
        request_id = new_id()
        first = prepare_change(self.book, spreadsheet_id="copy-id", expected_revision=0, request_id=request_id, action="create", name="Повтор", subcategories=("Одна",))
        second = prepare_change(self.book, spreadsheet_id="copy-id", expected_revision=0, request_id=request_id, action="create", name="Повтор", subcategories=("Одна",))
        self.assertEqual(first.target_digest, second.target_digest)

    def test_move_preserves_record_parent_and_income_type_is_not_crossed(self):
        change = self.plan("create", name="Кафе", subcategories=("Чай",))
        self.book = change.output
        meta = WorkbookMetadata.from_rows(sheet_rows(self.book.sheets[METADATA_SHEET]))
        registry = parse_registry(meta, {key: sheet_rows(self.book.sheets[key]) for key in REGISTRY_HEADERS})
        target = registry.categories[-1]
        moved = prepare_change(self.book, spreadsheet_id="copy-id", expected_revision=1, request_id=new_id(), action="move", entity_id=self.child.subcategory_id, parent_id=target.category_id)
        self.assertEqual(moved.output.sheets["Траты"].cells[2, 7], self.parent.category_id)
        self.assertEqual(moved.output.sheets["Архив"].cells[2, 5], 900)
        with self.assertRaises(ValueError):
            prepare_change(self.book, spreadsheet_id="copy-id", expected_revision=1, request_id=new_id(), action="move", entity_id=self.child.subcategory_id, parent_id=registry.categories[1].category_id)

    def test_deactivate_keeps_archive_and_stored_pair(self):
        change = self.plan("deactivate", entity_id=self.parent.category_id, period=Period(2026, 11))
        self.assertEqual(change.output.sheets["Архив"].cells[2, 5], 900)
        self.assertEqual(change.output.sheets["Траты"].cells[2, 7], self.parent.category_id)
        meta = WorkbookMetadata.from_rows(sheet_rows(change.output.sheets[METADATA_SHEET]))
        registry = parse_registry(meta, {key: sheet_rows(change.output.sheets[key]) for key in REGISTRY_HEADERS})
        self.assertFalse(registry.category(self.parent.category_id).active)
        self.assertFalse(registry.subcategory(self.child.subcategory_id).active)

    def test_partial_rename_stays_pending_and_resumes_same_request(self):
        change = self.plan("rename", entity_id=self.parent.category_id, name="Покупки")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def fail(name):
                if name == "Архив":
                    raise RuntimeError("simulated archive failure")
            with self.assertRaises(RuntimeError):
                apply_change_to_copy(self.book, change, root, confirmed=True, checkpoint=fail)
            state = json.loads((root / (change.request_id + ".json")).read_text())
            self.assertFalse(state["finished"])
            from finance_bot.migration_apply import payload_snapshot
            partial = payload_snapshot(state["workbook"])
            meta = WorkbookMetadata.from_rows(sheet_rows(partial.sheets[METADATA_SHEET]))
            self.assertEqual(meta.pending_request_id, change.request_id)
            with self.assertRaises(ValueError):
                meta.require_ready("copy-id")
            complete = apply_change_to_copy(self.book, change, root, confirmed=True)
            self.assertEqual(fingerprint(complete), change.target_digest)
            again = apply_change_to_copy(self.book, change, root, confirmed=True)
            self.assertEqual(fingerprint(again), fingerprint(complete))


if __name__ == "__main__":
    unittest.main()

class MissingArchiveCategoryChangeTests(unittest.TestCase):
    setUp = CategoryChangesTests.setUp
    plan = CategoryChangesTests.plan
    def add_missing_archive_reference(self):
        from finance_bot.category_projections import LABEL_HEADERS
        rows=sheet_rows(self.book.sheets[LABEL_SHEET])
        rows.append(['Архив (удалённый снимок)','A9','category',self.parent.category_id,''])
        self.book.sheets[LABEL_SHEET]=table_sheet(LABEL_SHEET,rows)
        return rows[-1]

    def test_nonrename_actions_preserve_dangling_index_and_existing_archive(self):
        missing=self.add_missing_archive_reference()
        for action,params in (
            ('create',dict(name='Одежда',subcategories=('Белье','Верхняя одежда','Одежда','Аксессуары'))),
            ('create',dict(parent_id=self.parent.category_id,name='Добавленная')),
            ('deactivate',dict(entity_id=self.child.subcategory_id)),
            ('deactivate',dict(entity_id=self.parent.category_id,period=Period(2026,11))),
        ):
            with self.subTest(action=action,params=params):
                change=self.plan(action,**params)
                self.assertIn(missing,sheet_rows(change.output.sheets[LABEL_SHEET]))
                self.assertNotIn(missing[0],change.output.sheets)
                self.assertEqual(change.output.sheets['Архив'],self.book.sheets['Архив'])
                self.assertEqual(change.output.sheets['Траты'].cells[2,4],100)

    def test_move_ignores_unrelated_missing_archive(self):
        self.book=self.plan('create',name='Получатель',subcategories=('Другая',)).output
        self.add_missing_archive_reference()
        from finance_bot.category_schema import parse_registry,REGISTRY_HEADERS
        meta=WorkbookMetadata.from_rows(sheet_rows(self.book.sheets[METADATA_SHEET]))
        registry=parse_registry(meta,{key:sheet_rows(self.book.sheets[key]) for key in REGISTRY_HEADERS})
        target=registry.categories[-1]
        change=prepare_change(self.book,spreadsheet_id='copy-id',expected_revision=1,request_id=new_id(),action='move',entity_id=self.child.subcategory_id,parent_id=target.category_id)
        self.assertEqual(change.output.sheets['Траты'].cells[2,7],self.parent.category_id)
        self.assertEqual(change.output.sheets['Архив'],self.book.sheets['Архив'])

    def test_rename_still_refuses_missing_archive_before_mutation(self):
        self.add_missing_archive_reference()
        before=fingerprint(self.book)
        with self.assertRaisesRegex(ValueError,'indexed archive sheet is missing'):
            self.plan('rename',entity_id=self.parent.category_id,name='Покупки')
        self.assertEqual(fingerprint(self.book),before)
