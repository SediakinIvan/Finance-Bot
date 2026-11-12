from copy import deepcopy
import unittest
import test_category_changes_native as native_tests
from finance_bot.category_changes import prepare_change
from finance_bot.category_projections import sheet_rows
from finance_bot.category_schema import METADATA_SHEET,WorkbookMetadata
from finance_bot.taxonomy import Period,new_id
from category_native_fixture import NativeTarget,attach


class ReactivationNativeTests(unittest.TestCase):
    setUp=native_tests.NativeCategoryChangeTests.setUp
    apply=native_tests.NativeCategoryChangeTests.apply
    rollback=native_tests.NativeCategoryChangeTests.rollback
    state=native_tests.NativeCategoryChangeTests.state

    def test_reactivation_resume_and_rollback_keep_archive_chart_and_original_ids(self):
        parent=self.registry.categories[0]
        self.source=prepare_change(self.source,spreadsheet_id='copy-id',expected_revision=0,request_id=new_id(),action='deactivate',entity_id=parent.category_id,period=Period(2026,11)).output
        self.target=NativeTarget(self.source)
        change=attach(prepare_change(self.source,spreadsheet_id='copy-id',expected_revision=1,request_id=new_id(),action='reactivate',entity_id=parent.category_id),self.source,self.target)
        before_charts=deepcopy(self.target.metadata['sheets'][0]['charts'])
        def fail(body):
            if any('setDataValidation' in r for r in body['requests']):raise RuntimeError('dropdown failed')
        self.target.before_native=fail
        with self.assertRaises(RuntimeError):self.apply(change)
        self.target.before_native=None
        self.rollback(change)
        self.assertFalse(self.target.book.sheets['_financebot_categories'].cells[2,4])
        self.assertEqual(self.target.book.sheets['_financebot_categories'].cells[2,5],'2026-11')
        self.assertEqual(self.target.metadata['sheets'][0]['charts'],before_charts)
        self.assertEqual(self.target.book.sheets['Архив'].cells[2,5],900)
        # New confirmed activation against the rolled-back revision.
        self.source=deepcopy(self.target.book)
        meta=WorkbookMetadata.from_rows(sheet_rows(self.source.sheets[METADATA_SHEET]))
        change=attach(prepare_change(self.source,spreadsheet_id='copy-id',expected_revision=meta.taxonomy_revision,request_id=new_id(),action='reactivate',entity_id=parent.category_id),self.source,self.target)
        self.target.before_native=fail
        with self.assertRaises(RuntimeError):self.apply(change)
        self.target.before_native=None;self.apply(change)
        self.assertTrue(self.target.book.sheets['_financebot_categories'].cells[2,4])
        self.assertEqual(self.target.metadata['sheets'][0]['charts'],before_charts)
        self.assertEqual(self.target.book.sheets['Траты'].cells[2,7],parent.category_id)
        self.assertEqual(self.target.book.sheets['Архив'].cells[2,5],900)
