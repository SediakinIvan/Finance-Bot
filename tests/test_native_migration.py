from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from finance_bot.category_schema import METADATA_SHEET
from finance_bot.migration_apply import MigrationError, prepare_migration
from finance_bot.native_migration import install_copy, verify_and_finalize_copy, native_formula, ledger_dropdown_table_updates
from test_migration_apply import migration_fixture
from test_v2_sheets import FakeBook


class NativeMigrationTests(unittest.TestCase):
    def test_typed_table_keeps_structure_and_other_types_for_dependent_dropdowns(self):
        columns = [
            {"columnName": "Дата", "columnType": "DATE"},
            {"columnIndex": 1, "columnName": "Категория", "columnType": "DROPDOWN", "dataValidationRule": {"condition": {"type": "ONE_OF_LIST"}}},
            {"columnIndex": 2, "columnName": "Подкатегория", "columnType": "DROPDOWN", "dataValidationRule": {"condition": {"type": "ONE_OF_LIST"}}},
            {"columnIndex": 3, "columnName": "Сумма", "columnType": "DOUBLE"},
        ]
        table = {"tableId": "existing", "name": "Таблица1", "range": {"endColumnIndex": 6}, "columnProperties": columns}
        metadata = {"sheets": [{"properties": {"title": "Траты"}, "tables": [table]}]}
        before = deepcopy(metadata)
        requests = ledger_dropdown_table_updates(metadata)
        self.assertEqual(metadata, before)
        updated = requests[0]["updateTable"]
        self.assertEqual(updated["fields"], "columnProperties")
        result = updated["table"]["columnProperties"]
        self.assertEqual(result[0], columns[0])
        self.assertEqual(result[3], columns[3])
        for column in result[1:3]:
            self.assertEqual(column["columnType"], "COLUMN_TYPE_UNSPECIFIED")
            self.assertNotIn("dataValidationRule", column)
        table["columnProperties"] = result
        self.assertEqual(ledger_dropdown_table_updates(metadata), [])
        metadata["sheets"][0]["properties"]["title"] = "Другое"
        self.assertEqual(ledger_dropdown_table_updates(metadata), [])

    def test_native_formula_respects_russian_locale_and_quoted_commas(self):
        formula = '=IF(\'Sheet, O\'\'Brien\'!A1="a,""b",SUM(1,2),0)'
        expected = '=IF(\'Sheet, O\'\'Brien\'!A1="a,""b";SUM(1;2);0)'
        self.assertEqual(native_formula(formula, SimpleNamespace(locale="ru_RU")), expected)
        self.assertEqual(native_formula(formula, SimpleNamespace(locale="en_US")), formula)
        with self.assertRaises(MigrationError):
            native_formula(formula, SimpleNamespace(locale="unverified"))

    def setUp(self):
        self.source, registry, reviewed = migration_fixture()
        self.plan = prepare_migration(self.source, registry, reviewed)
        self.target = FakeBook(self.source)
        self.backup = FakeBook(self.source)
        self.backup.id = "backup-copy"

    def test_install_requires_separate_native_backup_and_keeps_applying(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "original.gs"
            script.write_text("function original() {}")
            with patch("finance_bot.native_migration.read_google_spreadsheet", return_value=deepcopy(self.source)):
                install_copy(self.source, self.plan, self.target, self.backup, Path(directory) / "state", script, confirmed=True, maintenance_confirmed=True)
            meta = dict(self.target.worksheet(METADATA_SHEET).rows[1:])
            self.assertEqual(meta["migration_state"], "applying")
            self.assertEqual(meta["access_mode"], "maintenance")
            self.assertEqual(self.target.worksheet("Траты").rows[1][:6], ["11.11.2026", "Продукты", "Магазин", 100, "Расход", "private note"])
            self.assertEqual(self.target.worksheet("Траты").rows[1][8], self.plan.output.sheets["Траты"].cells[2, 9])
            with self.assertRaises(MigrationError):
                install_copy(self.source, self.plan, self.target, self.target, Path(directory) / "state", script, confirmed=True, maintenance_confirmed=True)

    def native_result(self):
        current = deepcopy(self.plan.output)
        view = current.sheets["Аналитика"]
        for cell, value in {(1, 6): "ready", (3, 2): 100, (4, 2): 0, (5, 2): -100, (9, 2): 100}.items():
            view.cells[cell] = value
        current.sheets["_financebot_detail"].cells[2, 5] = 100
        current.sheets["_financebot_detail"].cells[3, 5] = 0
        return current

    def test_ready_is_published_only_after_native_values_and_proof_match(self):
        current = self.native_result()
        self.target = FakeBook(current)
        proof = {"spreadsheet_id": "copy-id", "schema_version": 2, "taxonomy_revision": 0, "verified": True}
        with patch("finance_bot.native_migration.read_google_spreadsheet", side_effect=[deepcopy(current), deepcopy(current)]):
            verify_and_finalize_copy(self.source, self.plan, self.target, proof, confirmed=True)
        meta = dict(self.target.worksheet(METADATA_SHEET).rows[1:])
        self.assertEqual(meta["migration_state"], "ready")
        self.assertEqual(meta["access_mode"], "open")
        self.assertEqual(len(self.target.writes), 1)

    def test_wrong_native_sum_or_missing_script_proof_never_publishes_ready(self):
        proof = {"spreadsheet_id": "copy-id", "schema_version": 2, "taxonomy_revision": 0, "verified": True}
        current = self.native_result()
        current.sheets["Аналитика"].cells[3, 2] = 999
        self.target = FakeBook(current)
        with patch("finance_bot.native_migration.read_google_spreadsheet", return_value=current):
            with self.assertRaises(MigrationError):
                verify_and_finalize_copy(self.source, self.plan, self.target, proof, confirmed=True)
        with self.assertRaises(MigrationError):
            verify_and_finalize_copy(self.source, self.plan, self.target, {}, confirmed=True)
        self.assertEqual(self.target.writes, [])


if __name__ == "__main__":
    unittest.main()
