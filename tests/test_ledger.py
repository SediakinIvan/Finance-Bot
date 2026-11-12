from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
import unittest

from finance_bot.category_schema import TRANSACTION_HEADERS
from finance_bot.ledger import LedgerError, normalize_date, reconcile_rows, require_valid, sheet_date
from finance_bot.taxonomy import Category, HistoricalPair, Period, Subcategory, Taxonomy, new_id


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.a = Category(new_id(), "Расход", "Продукты")
        self.b = Category(new_id(), "Расход", "Кафе")
        self.child = Subcategory(new_id(), self.a.category_id, "Доставка")
        self.registry = Taxonomy("test", 0, (self.a, self.b), (self.child,))
        self.row = ["11.11.2026", "Продукты", "Доставка", 100, "Расход", ""]

    def reconcile(self, row=None, registry=None, snapshots=None):
        return reconcile_rows([list(TRANSACTION_HEADERS), row or self.row], registry or self.registry, snapshots or {})[0]

    def test_date_formats_and_month_boundaries(self):
        for value in (date(2026, 11, 1), datetime(2026, 11, 1, 14), "01.11.2026", "2026-11-01", 46327):
            self.assertEqual(normalize_date(value), date(2026, 11, 1))
        self.assertEqual(normalize_date(sheet_date("31.12.2026")), date(2026, 12, 31))
        for value in ("", None, True, "31.11.2026", float("nan"), -1, "46327"):
            with self.subTest(value=value), self.assertRaises(LedgerError):
                normalize_date(value)

    def test_new_row_gets_ids_and_keeps_visible_fields(self):
        row = self.reconcile()
        self.assertEqual(row.state, "valid")
        self.assertEqual(row.values, tuple(self.row))
        self.assertEqual(row.category_id, self.a.category_id)
        self.assertEqual(row.report_date, sheet_date(self.row[0]))

    def test_other_fields_do_not_reparent_moved_inactive_history(self):
        original = self.reconcile()
        old_pair = HistoricalPair(self.a.category_id, self.child.subcategory_id)
        registry = replace(self.registry, categories=(replace(self.a, active=False, deactivated_period=Period(2026, 11)), self.b), subcategories=(replace(self.child, current_category_id=self.b.category_id),), historical_pairs=(old_pair,))
        edited = [*self.row, *original.service_values()]
        edited[3] = 200
        edited[5] = "updated"
        row = self.reconcile(edited, registry, {original.transaction_id: original.identity()})
        self.assertEqual(row.state, "valid")
        self.assertEqual(row.category_id, self.a.category_id)
        # No snapshot means the inactive historical pair cannot be forged.
        self.assertEqual(self.reconcile(edited, registry).state, "invalid_category_pair")

    def test_explicit_reclassification_updates_both_ids(self):
        original = self.reconcile()
        registry = replace(self.registry, subcategories=(replace(self.child, current_category_id=self.b.category_id),), historical_pairs=(HistoricalPair(self.a.category_id, self.child.subcategory_id),))
        edited = [*self.row, *original.service_values()]
        edited[1] = "Кафе"
        row = self.reconcile(edited, registry, {original.transaction_id: original.identity()})
        self.assertEqual(row.category_id, self.b.category_id)
        self.assertEqual(row.transaction_id, original.transaction_id)

    def test_invalid_manual_edit_can_revert_to_inactive_historical_pair(self):
        original = self.reconcile()
        registry = replace(self.registry, categories=(replace(self.a, active=False, deactivated_period=Period(2026, 11)), self.b))
        source = [*self.row, *original.service_values()]
        source[2] = "Опечатка"
        snapshots = {original.transaction_id: original.identity()}
        invalid = self.reconcile(source, registry, snapshots)
        self.assertEqual(invalid.state, "invalid_category_pair")
        self.assertEqual(invalid.category_id, self.a.category_id)
        source = [*self.row, *invalid.service_values()]
        corrected = self.reconcile(source, registry, snapshots)
        self.assertEqual(corrected.state, "valid")
        self.assertEqual(corrected.category_id, self.a.category_id)

    def test_paste_duplicates_incomplete_and_cleared_rows(self):
        original = self.reconcile()
        source = [*self.row, *original.service_values()]
        rows = reconcile_rows([list(TRANSACTION_HEADERS), source, source, ["", "Продукты"], [""] * 11], self.registry, {})
        self.assertEqual(rows[0].state, "duplicate_or_invalid_transaction_id")
        self.assertEqual(rows[1].state, "duplicate_or_invalid_transaction_id")
        self.assertNotEqual(rows[2].state, "valid")
        self.assertEqual(rows[3].service_values(), ["", "", "", "", ""])
        with self.assertRaises(LedgerError):
            require_valid(rows)

    def test_invalid_date_amount_and_tampered_id_are_visible(self):
        for index, value, expected in ((0, "bad", "invalid_date"), (3, 0, "invalid_amount"), (3, Decimal("NaN"), "invalid_amount")):
            source = self.row.copy()
            source[index] = value
            self.assertEqual(self.reconcile(source).state, expected)
        original = self.reconcile()
        source = [*self.row, *original.service_values()]
        source[6] = self.b.category_id
        row = self.reconcile(source, snapshots={original.transaction_id: original.identity()})
        self.assertEqual(row.state, "identity_tampered")


if __name__ == "__main__":
    unittest.main()
