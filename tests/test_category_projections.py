from dataclasses import replace
from decimal import Decimal
import unittest

from finance_bot.category_projections import analytics_sheets, calculate_totals, report_label_rows, report_blocks
from finance_bot.category_schema import TRANSACTION_HEADERS
from finance_bot.ledger import LedgerError, reconcile_rows
from finance_bot.taxonomy import Category, HistoricalPair, Period, Subcategory, Taxonomy, new_id


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.a = Category(new_id(), "Расход", "Продукты")
        self.b = Category(new_id(), "Расход", "Кафе")
        self.income = Category(new_id(), "Доход", "Доход")
        self.x = Subcategory(new_id(), self.a.category_id, "Доставка")
        self.y = Subcategory(new_id(), self.b.category_id, "Доставка")
        self.z = Subcategory(new_id(), self.income.category_id, "Зарплата")
        self.registry = Taxonomy("copy", 0, (self.a, self.b, self.income), (self.x, self.y, self.z))
        self.rows = reconcile_rows([list(TRANSACTION_HEADERS),
            ["01.11.2026", "Продукты", "Доставка", 100, "Расход", ""],
            ["30.11.2026", "Кафе", "Доставка", 200, "Расход", ""],
            ["30.11.2026", "Доход", "Зарплата", 1000, "Доход", ""],
            ["01.12.2026", "Кафе", "Доставка", 999, "Расход", ""],
        ], self.registry, {})

    def test_reference_totals_separate_identical_subcategories_and_periods(self):
        totals = calculate_totals(self.rows, self.registry, Period(2026, 11))
        self.assertEqual((totals.income, totals.expenses, totals.balance), (1000, 300, 700))
        self.assertEqual(totals.subcategories[HistoricalPair(self.a.category_id, self.x.subcategory_id)], 100)
        self.assertEqual(totals.subcategories[HistoricalPair(self.b.category_id, self.y.subcategory_id)], 200)

    def test_generated_formulas_use_ids_and_normalized_dates(self):
        summary, detail = analytics_sheets(self.registry, Period(2026, 11))
        self.assertIn(self.a.category_id, summary.formulas[9, 2])
        self.assertIn("K2:K", summary.formulas[9, 2])
        self.assertIn("G2:G", summary.formulas[9, 2])
        self.assertIn("H2:H", detail.formulas[2, 5])
        self.assertIn("NA()", detail.formulas[2, 5])
        self.assertEqual(summary.formulas[9, 3], "=IF($B$3=0,0,B9/$B$3)")

    def test_visible_blocks_preserve_reference_order_and_link_to_id_detail(self):
        preferred = (self.income.category_id, self.b.category_id, self.a.category_id)
        view, detail = analytics_sheets(self.registry, Period(2026, 11), preferred_categories=preferred)
        self.assertEqual([view.cell(1, col) for col in (7, 10, 13)],
                         ["Подкатегории: Доход", "Подкатегории: Кафе", "Подкатегории: Продукты"])
        self.assertEqual(view.formulas[3, 8], "='_financebot_detail'!E4")
        self.assertEqual(view.formulas[3, 11], "='_financebot_detail'!E3")
        self.assertEqual(view.formulas[3, 14], "='_financebot_detail'!E2")
        labels = report_label_rows(self.registry, preferred)
        self.assertIn(['Аналитика','G3','subcategory',self.income.category_id,self.z.subcategory_id], labels)
        # The next band must follow both the longest detail list and its chart.
        many = tuple(Subcategory(new_id(), self.a.category_id, f"Дополнение {i}") for i in range(20))
        new_parent = Category(new_id(), "Расход", "Новая")
        extra = tuple(Subcategory(new_id(), new_parent.category_id, str(i)) for i in range(2))
        extended = replace(self.registry, categories=self.registry.categories + (new_parent,), subcategories=self.registry.subcategories + many + extra)
        blocks = report_blocks(extended, preferred)
        self.assertEqual(len(blocks), 4)
        self.assertEqual(blocks[3][1], 34)

    def test_move_does_not_recalculate_old_parent_and_zero_month_is_valid(self):
        moved = replace(self.registry, subcategories=(replace(self.x, current_category_id=self.b.category_id, name="Новая доставка"), self.y, self.z), historical_pairs=(HistoricalPair(self.a.category_id, self.x.subcategory_id),))
        totals = calculate_totals(self.rows, moved, Period(2026, 11))
        self.assertEqual(totals.categories[self.a.category_id], 100)
        totals = calculate_totals(self.rows, moved, Period(2026, 10))
        self.assertEqual(totals.expenses, Decimal(0))

    def test_deactivation_preserves_month_total_but_rejects_later_operations(self):
        inactive = replace(self.registry, categories=(self.a, replace(self.b, active=False, deactivated_period=Period(2026, 11)), self.income))
        self.assertEqual(calculate_totals(self.rows, inactive, Period(2026, 11)).categories[self.b.category_id], 200)
        with self.assertRaises(LedgerError):
            calculate_totals(self.rows, inactive, Period(2026, 12))


if __name__ == "__main__":
    unittest.main()
