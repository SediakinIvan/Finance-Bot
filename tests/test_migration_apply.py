from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from finance_bot.category_migration import SheetSnapshot, WorkbookSnapshot
from finance_bot.category_schema import METADATA_SHEET, MigrationState, WorkbookMetadata
from finance_bot.category_projections import sheet_rows
from finance_bot.migration_apply import (
    MigrationError, apply_to_copy, fingerprint, prepare_migration, restore_copy,
)
from finance_bot.migration_mapping import ReviewedMapping, seed_legacy_taxonomy


def migration_fixture():
    book = WorkbookSnapshot("fixture", {
        "Категории": SheetSnapshot("Категории", {
            (1, 1): "Тип", (1, 2): "Категория", (1, 3): "Подкатегория",
            (2, 1): "Расход", (2, 2): "Продукты", (2, 3): "Магазин",
            (3, 1): "Доход", (3, 2): "Доход", (3, 3): "Зарплата",
        }),
        "Траты": SheetSnapshot("Траты", {
            **{(1, col): value for col, value in enumerate(("Дата", "Категория", "Подкатегория", "Сумма", "Доход/Расход", "Примечание"), 1)},
            (2, 1): "11.11.2026", (2, 2): "Продукты", (2, 3): "Магазин",
            (2, 4): 100, (2, 5): "Расход", (2, 6): "private note",
        }),
        "Аналитика": SheetSnapshot("Аналитика", {(1, 4): 11, (2, 4): 2026}),
        "Архив": SheetSnapshot("Архив", {(1, 5): "Продукты", (2, 5): 900}),
    })
    registry = seed_legacy_taxonomy(book, "copy-id")
    reviewed = ReviewedMapping("fixture", "copy-id", True)
    return book, registry, reviewed


class MigrationApplyTests(unittest.TestCase):
    def setUp(self):
        self.source, self.registry, self.reviewed = migration_fixture()
        self.plan = prepare_migration(self.source, self.registry, self.reviewed)

    def test_plan_does_not_modify_source_or_claim_native_ready(self):
        self.assertNotIn(METADATA_SHEET, self.source.sheets)
        meta = WorkbookMetadata.from_rows(sheet_rows(self.plan.output.sheets[METADATA_SHEET]))
        self.assertEqual(meta.migration_state, MigrationState.APPLYING)
        with self.assertRaises(ValueError):
            meta.require_ready("copy-id")

    def test_failure_resumes_with_same_ids_and_restores_original_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "original.gs"
            script.write_text("function original() {}")
            backup = root / "migration"
            def crash(name):
                if name == "Траты":
                    raise RuntimeError("simulated write failure")
            with self.assertRaises(RuntimeError):
                apply_to_copy(self.source, self.plan, backup, script, confirmed=True, checkpoint=crash)
            partial = json.loads((backup / "state.json").read_text())
            self.assertIn("Траты", partial["completed"])
            resumed = apply_to_copy(self.source, self.plan, backup, script, confirmed=True)
            self.assertEqual(resumed.sheets["Траты"].cells[2, 9], self.plan.output.sheets["Траты"].cells[2, 9])
            again = apply_to_copy(self.source, self.plan, backup, script, confirmed=True)
            self.assertEqual(fingerprint(again), fingerprint(resumed))
            restored, script_text = restore_copy(backup, confirmed=True)
            self.assertEqual(fingerprint(restored), fingerprint(self.source))
            self.assertEqual(script_text, "function original() {}")
            self.assertEqual(restored.sheets["Архив"].cells[2, 5], 900)
            self.assertEqual(restored.sheets["Траты"].cells[2, 6], "private note")
            self.assertEqual((backup / "backup.json").stat().st_mode & 0o777, 0o600)

    def test_modified_source_plan_and_unconfirmed_apply_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "source.gs"
            script.write_text("function source() {}")
            with self.assertRaises(MigrationError):
                apply_to_copy(self.source, self.plan, root / "out", script, confirmed=False)
            self.source.sheets["Траты"].cells[2, 4] = 200
            with self.assertRaises(MigrationError):
                apply_to_copy(self.source, self.plan, root / "out", script, confirmed=True)
            self.assertFalse((root / "out").exists())

    def test_no_overwriting_preexisting_service_or_custom_columns(self):
        self.source.sheets["Траты"].cells[1, 7] = "custom"
        with self.assertRaises(MigrationError):
            prepare_migration(self.source, self.registry, self.reviewed)


if __name__ == "__main__":
    unittest.main()
