from copy import deepcopy
import unittest
from test_dashboard_changes import fixture
from finance_bot.category_changes import prepare_change
from finance_bot.category_schema import METADATA_SHEET,REGISTRY_HEADERS,WorkbookMetadata,parse_registry
from finance_bot.category_projections import sheet_rows
from finance_bot.ledger import resolve_new_pair
from finance_bot.taxonomy import new_id,Period


class ReactivationTests(unittest.TestCase):
    def setUp(self):self.book,self.registry=fixture()
    def plan(self,**params):
        meta=WorkbookMetadata.from_rows(sheet_rows(self.book.sheets[METADATA_SHEET]))
        return prepare_change(self.book,spreadsheet_id='copy-id',expected_revision=meta.taxonomy_revision,request_id=new_id(),**params)
    def registry_of(self,book):
        meta=WorkbookMetadata.from_rows(sheet_rows(book.sheets[METADATA_SHEET]))
        return parse_registry(meta,{name:sheet_rows(book.sheets[name]) for name in REGISTRY_HEADERS})
    def test_category_restores_all_children_and_all_ids_without_touching_archives(self):
        parent=self.registry.categories[0];child=self.registry.subcategories[0]
        self.book=self.plan(action='create',parent_id=parent.category_id,name='Отдельная').output
        registry=self.registry_of(self.book);separate=registry.subcategories[-1]
        self.book=self.plan(action='deactivate',entity_id=separate.subcategory_id).output
        self.book=self.plan(action='deactivate',entity_id=parent.category_id,period=Period(2026,11)).output
        archived=deepcopy(self.book.sheets['Архив']);before=deepcopy(self.book.sheets['Аналитика'])
        change=self.plan(action='reactivate',entity_id=parent.category_id);result=self.registry_of(change.output)
        self.assertTrue(result.category(parent.category_id).active);self.assertIsNone(result.category(parent.category_id).deactivated_period)
        self.assertTrue(result.subcategory(child.subcategory_id).active);self.assertTrue(result.subcategory(separate.subcategory_id).active)
        self.assertEqual(result.historical_pairs,self.registry_of(self.book).historical_pairs)
        self.assertEqual(change.output.sheets['Архив'],archived);self.assertEqual(change.output.sheets['Аналитика'],before)
        self.assertEqual(resolve_new_pair(result,'Расход',parent.name,child.name),(parent.category_id,child.subcategory_id))
        self.book=change.output
        with self.assertRaisesRegex(ValueError,'already active'):self.plan(action='reactivate',entity_id=parent.category_id)
        self.book=self.plan(action='deactivate',entity_id=parent.category_id,period=Period(2027,1)).output
        self.assertEqual(self.registry_of(self.book).category(parent.category_id).deactivated_period,Period(2027,1))

    def test_moved_child_is_not_reactivated_with_former_parent(self):
        parent=self.registry.categories[0];child=self.registry.subcategories[0]
        self.book=self.plan(action='create',name='Получатель',subcategories=('Другая',)).output
        destination=self.registry_of(self.book).categories[-1]
        self.book=self.plan(action='move',entity_id=child.subcategory_id,parent_id=destination.category_id).output
        self.book=self.plan(action='deactivate',entity_id=child.subcategory_id).output
        self.book=self.plan(action='deactivate',entity_id=parent.category_id,period=Period(2026,11)).output
        result=self.registry_of(self.plan(action='reactivate',entity_id=parent.category_id).output)
        self.assertFalse(result.subcategory(child.subcategory_id).active)
        self.assertEqual(result.subcategory(child.subcategory_id).current_category_id,destination.category_id)

    def test_separate_child_requires_active_parent_and_does_not_restore_other_children(self):
        parent=self.registry.categories[0];child=self.registry.subcategories[0]
        self.book=self.plan(action='create',parent_id=parent.category_id,name='Другая').output
        other=self.registry_of(self.book).subcategories[-1]
        self.book=self.plan(action='deactivate',entity_id=parent.category_id,period=Period(2026,11)).output
        with self.assertRaisesRegex(ValueError,'parent category first'):self.plan(action='reactivate',entity_id=child.subcategory_id)
        self.book=self.plan(action='reactivate',entity_id=parent.category_id).output
        for cid in (child.subcategory_id,other.subcategory_id):self.book=self.plan(action='deactivate',entity_id=cid).output
        result=self.registry_of(self.plan(action='reactivate',entity_id=child.subcategory_id).output)
        self.assertTrue(result.subcategory(child.subcategory_id).active);self.assertFalse(result.subcategory(other.subcategory_id).active)

    def test_reactivation_rejects_corrupt_duplicate_names_before_any_output(self):
        parent=self.registry.categories[0]
        self.book=self.plan(action='deactivate',entity_id=parent.category_id,period=Period(2026,11)).output
        from finance_bot.category_projections import table_sheet
        rows=sheet_rows(self.book.sheets['_financebot_categories'])
        rows.append([new_id(),'Расход',parent.name,True,''])
        self.book.sheets['_financebot_categories']=table_sheet('_financebot_categories',rows)
        with self.assertRaises(ValueError):self.plan(action='reactivate',entity_id=parent.category_id)
