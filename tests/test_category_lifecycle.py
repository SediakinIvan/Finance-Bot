from copy import deepcopy
import unittest
import test_category_changes as fixtures
from finance_bot.category_projections import ARCHIVE_COLUMNS_SHEET,LABEL_SHEET,sheet_rows,table_sheet
from finance_bot.taxonomy import new_id


class ArchiveHeaderTests(unittest.TestCase):
    setUp = fixtures.CategoryChangesTests.setUp
    plan = fixtures.CategoryChangesTests.plan
    def test_missing_label_is_repaired_by_id_and_archived_numbers_unchanged(self):
        labels=sheet_rows(self.book.sheets[LABEL_SHEET])
        labels=[row for row in labels if row[0]!='Архив']
        self.book.sheets[LABEL_SHEET]=table_sheet(LABEL_SHEET,labels)
        before=deepcopy(self.book.sheets['Архив'])
        result=self.plan('rename',entity_id=self.parent.category_id,name='Новое').output
        self.assertEqual(result.sheets['Архив'].cell(1,5),'Новое')
        before.cells[1,5]='Новое';self.assertEqual(result.sheets['Архив'],before)
        self.assertIn(['Архив','E1','category',self.parent.category_id,''],sheet_rows(result.sheets[LABEL_SHEET]))

    def test_invalid_mapping_refuses_before_write(self):
        for entries in (((self.parent.category_id,4),),((self.parent.category_id,5),(self.parent.category_id,6)),((self.parent.category_id,5),(new_id(),5))):
            with self.subTest(entries=entries):
                self.book.sheets[ARCHIVE_COLUMNS_SHEET]=table_sheet(ARCHIVE_COLUMNS_SHEET,[['category_id','column'],*entries])
                with self.assertRaises(ValueError):self.plan('rename',entity_id=self.parent.category_id,name='Новое')

    def test_header_formula_or_conflicting_label_refuses(self):
        self.book.sheets['Архив'].formulas[1,5]='=123'
        with self.assertRaises(ValueError):self.plan('rename',entity_id=self.parent.category_id,name='Новое')

    def test_income_does_not_create_an_expense_column(self):
        before=deepcopy(self.book.sheets['Архив'])
        income_id=next(row[0] for row in sheet_rows(self.book.sheets['_financebot_categories'])[1:] if row[1]=='Доход')
        output=self.plan('rename',entity_id=income_id,name='Новый доход').output
        self.assertEqual(output.sheets['Архив'],before)
