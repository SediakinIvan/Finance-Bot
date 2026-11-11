from dataclasses import FrozenInstanceError, replace
import unittest

from finance_bot.taxonomy import (
    Category, HistoricalPair, Period, Subcategory, Taxonomy, TaxonomyError, new_id,
)


class TaxonomyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.food = Category(new_id(), "Расход", "Продукты")
        self.cafe = Category(new_id(), "Расход", "Кафе")
        self.delivery = Subcategory(new_id(), self.food.category_id, "Доставка")
        self.taxonomy = Taxonomy("sheet-a", 0, (self.food, self.cafe), (self.delivery,))

    def test_rename_does_not_change_identity(self) -> None:
        renamed = replace(self.food, name="Покупки продуктов")
        registry = replace(self.taxonomy, categories=(renamed, self.cafe), revision=1)
        self.assertEqual(registry.category(self.food.category_id).name, renamed.name)
        self.assertEqual(self.delivery.current_category_id, renamed.category_id)
        with self.assertRaises(FrozenInstanceError):
            renamed.name = "Изменение"  # type: ignore[misc]

    def test_move_preserves_historical_pair_but_changes_new_choices(self) -> None:
        previous = HistoricalPair(self.food.category_id, self.delivery.subcategory_id)
        moved = replace(self.delivery, current_category_id=self.cafe.category_id)
        registry = replace(self.taxonomy, subcategories=(moved,), historical_pairs=(previous,))
        self.assertEqual(registry.validate_pair(previous.category_id, previous.subcategory_id, new_operation=False), previous)
        with self.assertRaises(TaxonomyError):
            registry.validate_pair(previous.category_id, previous.subcategory_id, new_operation=True)
        self.assertEqual(registry.active_pairs(), (HistoricalPair(self.cafe.category_id, moved.subcategory_id),))

    def test_deactivation_preserves_history_and_excludes_children(self) -> None:
        inactive = replace(self.food, active=False, deactivated_period=Period(2026, 11))
        registry = replace(self.taxonomy, categories=(inactive, self.cafe))
        self.assertEqual(registry.active_pairs(), ())
        registry.validate_pair(inactive.category_id, self.delivery.subcategory_id, new_operation=False)
        with self.assertRaises(TaxonomyError):
            registry.validate_pair(inactive.category_id, self.delivery.subcategory_id, new_operation=True)

    def test_subcategory_deactivation_also_preserves_history(self) -> None:
        registry = replace(self.taxonomy, subcategories=(replace(self.delivery, active=False),))
        self.assertEqual(registry.active_pairs(), ())
        registry.validate_pair(self.food.category_id, self.delivery.subcategory_id, new_operation=False)

    def test_names_are_unique_in_context_including_inactive_entities(self) -> None:
        duplicate = Category(new_id(), "Расход", "  ПРОДУКТЫ  ")
        with self.assertRaises(TaxonomyError):
            replace(self.taxonomy, categories=(*self.taxonomy.categories, duplicate))
        with self.assertRaises(TaxonomyError):
            replace(self.taxonomy, subcategories=(self.delivery, Subcategory(new_id(), self.food.category_id, " доставка ")))
        same_name_elsewhere = Subcategory(new_id(), self.cafe.category_id, "Доставка")
        registry = replace(self.taxonomy, subcategories=(self.delivery, same_name_elsewhere))
        self.assertEqual(len(registry.active_pairs()), 2)
        self.assertNotEqual(registry.active_pairs()[0].subcategory_id, registry.active_pairs()[1].subcategory_id)

    def test_rejects_dangling_duplicate_and_cross_type_relationships(self) -> None:
        with self.assertRaises(TaxonomyError):
            replace(self.taxonomy, categories=(self.food, self.food))
        with self.assertRaises(TaxonomyError):
            replace(self.taxonomy, subcategories=(replace(self.delivery, current_category_id=new_id()),))
        with self.assertRaises(TaxonomyError):
            replace(self.taxonomy, historical_pairs=(HistoricalPair(new_id(), self.delivery.subcategory_id),))
        income = Category(new_id(), "Доход", "Доход")
        with self.assertRaises(TaxonomyError):
            replace(self.taxonomy, categories=(*self.taxonomy.categories, income), historical_pairs=(HistoricalPair(income.category_id, self.delivery.subcategory_id),))

    def test_scope_and_revision_are_checked(self) -> None:
        self.taxonomy.assert_scope("sheet-a", 0)
        for scope, revision in (("sheet-b", 0), ("sheet-a", 1)):
            with self.assertRaises(TaxonomyError):
                self.taxonomy.assert_scope(scope, revision)

    def test_activity_period_and_ids_are_validated(self) -> None:
        for period in ("2026-00", "2026-13", "0000-01", "2026-1"):
            with self.assertRaises(TaxonomyError):
                Period.parse(period)
        self.assertEqual(str(Period.parse("2026-11")), "2026-11")
        with self.assertRaises(TaxonomyError):
            replace(self.food, active=False)
        with self.assertRaises(TaxonomyError):
            replace(self.food, category_id="not-an-id")
        with self.assertRaises(TaxonomyError):
            replace(self.delivery, active="FALSE")


if __name__ == "__main__":
    unittest.main()
