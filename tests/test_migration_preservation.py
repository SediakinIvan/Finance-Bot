from copy import deepcopy
import unittest

from finance_bot.migration_apply import prepare_migration, verify_migrated_copy, MigrationError
from finance_bot.migration_preservation import preserve_view
from finance_bot.category_migration import SheetSnapshot
from finance_bot.native_migration import _write_sheet
from finance_bot.report_presentation import preserve_chart_requests
from test_migration_apply import migration_fixture
from test_v2_sheets import FakeBook


class MigrationPreservationTests(unittest.TestCase):
    def test_custom_cells_and_formulas_survive_planning_and_native_view_write(self):
        source, taxonomy, reviewed = migration_fixture()
        source.sheets["Аналитика"].cells[60, 20] = "owner note"
        source.sheets["Аналитика"].formulas[61, 20] = "=SUM(1,2)"
        source.sheets["Категории"].cells[2, 8] = "custom registry note"
        plan = prepare_migration(source, taxonomy, reviewed)
        verify_migrated_copy(source, plan.output)
        book = FakeBook(source)
        for name in ("Аналитика", "Категории"):
            _write_sheet(book, plan.output.sheets[name])
        self.assertEqual(book.worksheet("Аналитика").rows[59][19], "owner note")
        self.assertEqual(book.worksheet("Категории").rows[1][7], "custom registry note")
        self.assertEqual(book.worksheet("Аналитика").rows[60][19], "=SUM(1,2)")
        broken = deepcopy(plan.output)
        broken.sheets["Аналитика"].cells[60, 20] = "lost"
        with self.assertRaises(MigrationError):
            verify_migrated_copy(source, broken)
        wrong_period = deepcopy(plan.output)
        wrong_period.sheets["Аналитика"].cells[1, 4] = 12
        with self.assertRaises(MigrationError):
            verify_migrated_copy(source, wrong_period)

    def test_new_generated_cell_cannot_overwrite_unknown_source_content(self):
        source, _, _ = migration_fixture()
        source.sheets["Аналитика"].cells[40, 13] = "owner"
        with self.assertRaises(ValueError):
            preserve_view(source, SheetSnapshot("Аналитика", {(40, 13): "new block"}))

    def test_known_legacy_block_tail_is_cleared_without_clearing_worksheet(self):
        source, _, _ = migration_fixture()
        source.sheets["Аналитика"].cells.update({(1, 7): "Подкатегории: Продукты", (3, 7): "Магазин", (4, 7): "old child"})
        view = preserve_view(source, SheetSnapshot("Аналитика", {(1, 7): "Подкатегории: Продукты", (3, 7): "Магазин"}))
        self.assertEqual(view.cells[4, 7], "")
        book = FakeBook(source)
        book.worksheet("Аналитика").clear = lambda: self.fail("must not clear live dashboard")
        _write_sheet(book, view)
        self.assertEqual(book.worksheet("Аналитика").rows[3][6], "")

    def test_existing_chart_identity_and_unrelated_chart_are_preserved(self):
        def spec(row, col):
            return {"pieChart": {"domain": {"sourceRange": {"sources": [{"sheetId": 7, "startRowIndex": row, "startColumnIndex": col}]}}}}
        existing = [{"chartId": 11, "spec": spec(0, 6)}, {"chartId": 12, "spec": spec(70, 20)}]
        chart = {"spec": spec(2, 6), "position": {"overlayPosition": {"anchorCell": {"sheetId": 7}}}}
        requests = preserve_chart_requests([{"addChart": {"chart": chart}}], existing)
        self.assertEqual(requests[0]["updateChartSpec"]["chartId"], 11)
        self.assertFalse(any("deleteEmbeddedObject" in r or "addChart" in r for r in requests))
        self.assertEqual(preserve_chart_requests([{"addChart": {"chart": chart}}], []), [{"addChart": {"chart": chart}}])
        with self.assertRaises(ValueError):
            preserve_chart_requests([{"addChart": {"chart": chart}}], existing + [{"chartId": 13, "spec": spec(0, 6)}])
