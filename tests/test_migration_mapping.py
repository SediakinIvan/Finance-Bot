from dataclasses import replace
import unittest

from finance_bot.category_migration import SheetSnapshot, WorkbookSnapshot
from finance_bot.migration_mapping import (
    LabelDecision, MappingError, ReviewedMapping, compile_label_index, seed_legacy_taxonomy,
)


class MigrationMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = WorkbookSnapshot("source-digest", {
            "Категории": SheetSnapshot("Категории", {
                (1, 1): "Тип", (1, 2): "Категория", (1, 3): "Подкатегория",
                (2, 1): "Расход", (2, 2): "Транспорт", (2, 3): "Whoosh",
                (3, 1): "Расход", (3, 2): "Транспорт", (3, 3): "Самокаты",
                (4, 1): "Расход", (4, 2): "Спорт", (4, 3): "Самокаты",
            }),
            "Траты": SheetSnapshot("Траты"),
            "Аналитика": SheetSnapshot("Аналитика"),
            "Архив (Октябрь 2026)": SheetSnapshot("Архив (Октябрь 2026)", {
                (1, 1): "Подкатегории: Транспорт", (3, 1): "Самокаты", (3, 2): 12345,
            }),
        })
        self.registry = seed_legacy_taxonomy(self.book, "sheet-a")
        self.reviewed = ReviewedMapping("source-digest", "sheet-a", True)

    def test_seeding_is_stable_and_scoped_not_a_rename_operation(self) -> None:
        self.assertEqual(seed_legacy_taxonomy(self.book, "sheet-a"), self.registry)
        other = seed_legacy_taxonomy(self.book, "sheet-b")
        self.assertNotEqual(other.categories[0].category_id, self.registry.categories[0].category_id)
        self.assertEqual(len({item.subcategory_id for item in self.registry.subcategories}), 3)

    def test_identical_labels_are_resolved_with_parent_context(self) -> None:
        index = compile_label_index(self.book, self.registry, self.reviewed)
        child = next(item for item in index if item.kind == "subcategory")
        self.assertEqual(child.subcategory_id, self.registry.subcategories[1].subcategory_id)
        self.assertNotEqual(child.subcategory_id, self.registry.subcategories[2].subcategory_id)
        self.assertEqual(self.book.sheets[child.sheet].cells[3, 2], 12345)

    def test_formula_mismatch_never_silently_maps_to_label_or_criterion(self) -> None:
        sheet = self.book.sheets["Архив (Октябрь 2026)"]
        sheet.formulas[3, 2] = '=SUMIFS(Траты!D:D,Траты!C:C,"Whoosh")'
        with self.assertRaises(MappingError):
            compile_label_index(self.book, self.registry, self.reviewed)
        whoosh = self.registry.subcategories[0]
        reviewed = replace(self.reviewed, decisions=(LabelDecision(sheet.title, "A3", whoosh.current_category_id, whoosh.subcategory_id),))
        index = compile_label_index(self.book, self.registry, reviewed)
        self.assertEqual(index[-1].subcategory_id, whoosh.subcategory_id)
        self.assertEqual(len(self.registry.subcategories), 3)

    def test_unknown_archived_names_require_decision_not_fabricated_transactions(self) -> None:
        self.book.sheets["Архив (Октябрь 2026)"].cells[3, 1] = "Старое имя"
        with self.assertRaises(MappingError):
            compile_label_index(self.book, self.registry, self.reviewed)
        self.assertEqual(self.book.sheets["Траты"].cells, {})

    def test_stale_unconfirmed_wrong_scope_and_unknown_decisions_fail(self) -> None:
        for reviewed in (replace(self.reviewed, confirmed=False), replace(self.reviewed, source_fingerprint="old"), replace(self.reviewed, spreadsheet_id="sheet-b"), replace(self.reviewed, decisions=(LabelDecision("missing", "A1", self.registry.categories[0].category_id),))):
            with self.subTest(reviewed=reviewed), self.assertRaises(MappingError):
                compile_label_index(self.book, self.registry, reviewed)


if __name__ == "__main__":
    unittest.main()
