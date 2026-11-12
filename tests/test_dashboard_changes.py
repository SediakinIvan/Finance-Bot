from copy import deepcopy
from dataclasses import replace
import unittest
from finance_bot.category_changes import prepare_change
from finance_bot.category_projections import DETAIL_SHEET,LABEL_SHEET,sheet_rows,table_sheet
from finance_bot.category_schema import WorkbookMetadata,METADATA_SHEET,MigrationState,AccessMode,REGISTRY_HEADERS,parse_registry
from finance_bot.migration_apply import prepare_migration
from finance_bot.taxonomy import new_id
from test_migration_apply import migration_fixture


def fixture():
    source,registry,reviewed=migration_fixture()
    book=prepare_migration(source,registry,reviewed).output
    book.sheets[METADATA_SHEET]=table_sheet(METADATA_SHEET,WorkbookMetadata('copy-id',2,0,MigrationState.READY,AccessMode.OPEN).to_rows())
    return book,registry


class DashboardProjectionTests(unittest.TestCase):
    def setUp(self):self.book,self.registry=fixture()
    def plan(self,**params):
        return prepare_change(self.book,spreadsheet_id='copy-id',expected_revision=0,request_id=new_id(),**params)
    def test_create_keeps_visible_blocks_custom_formulas_and_period(self):
        source=self.book.sheets['Аналитика']
        source.cells[2,10]='My custom block';source.formulas[3,10]='=123+456'
        source.cells[1,4]=12
        for children in (('Одежда',),('Белье','Одежда','Аксессуары')):
            with self.subTest(children=children):
                result=self.plan(action='create',name='Одежда',subcategories=children).output
                actual=result.sheets['Аналитика']
                for key,value in source.cells.items():self.assertEqual(actual.cells.get(key),value)
                for key,value in source.formulas.items():self.assertEqual(actual.formulas.get(key),value)
                added={key for key in (*actual.cells,*actual.formulas) if key not in source.cells and key not in source.formulas}
                self.assertEqual(added,{(10,1),(10,2),(10,3),(10,4)})
                self.assertEqual(actual.cell(10,1),'Одежда')
                labels=sheet_rows(result.sheets[LABEL_SHEET])[1:]
                self.assertFalse(any(row[2]=='heading' and row[3]!=self.registry.categories[0].category_id for row in labels))
    def test_add_second_child_and_income_do_not_change_visible_sheet(self):
        for params in (dict(parent_id=self.registry.categories[0].category_id,name='Добавка'),dict(name='Новый доход',transaction_type='Доход',subcategories=('Первая','Вторая'))):
            result=self.plan(action='create',**params).output
            self.assertEqual(result.sheets['Аналитика'],self.book.sheets['Аналитика'])
            self.assertGreater(result.sheets[DETAIL_SHEET].last_row,self.book.sheets[DETAIL_SHEET].last_row)
    def test_occupied_summary_row_refuses_without_touching_source(self):
        self.book.sheets['Аналитика'].cells[10,1]='Owner data'
        before=deepcopy(self.book)
        with self.assertRaisesRegex(ValueError,'free space'):
            self.plan(action='create',name='Новая',subcategories=('Одна',))
        self.assertEqual(self.book,before)
    def test_detail_rows_do_not_shift_after_move_then_new_child(self):
        first=self.plan(action='create',name='Получатель',subcategories=('Другая',))
        self.book=first.output
        meta=WorkbookMetadata.from_rows(sheet_rows(self.book.sheets[METADATA_SHEET]))
        registry=parse_registry(meta,{name:sheet_rows(self.book.sheets[name]) for name in REGISTRY_HEADERS})
        moved=prepare_change(self.book,spreadsheet_id='copy-id',expected_revision=1,request_id=new_id(),action='move',entity_id=self.registry.subcategories[0].subcategory_id,parent_id=registry.categories[-1].category_id)
        before=sheet_rows(moved.output.sheets[DETAIL_SHEET])
        added=prepare_change(moved.output,spreadsheet_id='copy-id',expected_revision=2,request_id=new_id(),action='create',parent_id=registry.categories[-1].category_id,name='Добавка')
        after=sheet_rows(added.output.sheets[DETAIL_SHEET])
        self.assertEqual([row[:2] for row in before],[row[:2] for row in after[:len(before)]])
