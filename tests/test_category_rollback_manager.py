from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from category_native_fixture import NativeTarget
from test_dashboard_changes import fixture
from finance_bot.category_management import CategoryManager,CategoryManagementError
from finance_bot.category_projections import sheet_rows
from finance_bot.category_changes_native import apply_change_to_google_copy
from finance_bot.migration_apply import _private_json


class RollbackManagerTests(unittest.TestCase):
    def setUp(self):
        source,self.registry=fixture()
        class Store(NativeTarget):
            spreadsheet_id='copy-id'
            def _get_spreadsheet(self):return self
            def _rows(self,title):return sheet_rows(self.book.sheets[title])
            def apply_category_change(self,source,change,directory,**kwargs):
                return apply_change_to_google_copy(source,change,self,directory,**kwargs)
        self.store=Store(source);self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.manager=CategoryManager(self.store,Path(self.tmp.name),123)
        self.patches=[patch('finance_bot.category_management.read_google_spreadsheet',side_effect=lambda _:deepcopy(self.store.book)),self.store.read_patch()]
        for item in self.patches:item.start();self.addCleanup(item.stop)
    def pending(self):
        preview=self.manager.preview(action='rename',entity_id=self.registry.categories[0].category_id,name='Покупки')
        self.store.before_native=lambda _:(_ for _ in ()).throw(RuntimeError('private secret'))
        with self.assertRaises(CategoryManagementError):self.manager.apply(preview.request_id)
        self.store.before_native=None
        return preview

    def test_confirmation_owner_scope_and_successful_rollback(self):
        preview=self.pending()
        with self.assertRaises(CategoryManagementError):self.manager.rollback(preview.request_id,confirmed=True)
        other=CategoryManager(self.store,Path(self.tmp.name),999)
        with self.assertRaises(CategoryManagementError):other.preview_rollback(preview.request_id)
        self.manager.preview_rollback(preview.request_id)
        with self.assertRaises(CategoryManagementError):self.manager.rollback(preview.request_id)
        result=self.manager.rollback(preview.request_id,confirmed=True)
        self.assertEqual(result.status,'rolled_back');self.assertIsNone(self.manager.pending())
        self.assertEqual(self.manager.rollback(preview.request_id,confirmed=True).status,'rolled_back')

    def test_rollback_restart_status_and_release_compatibility(self):
        preview=self.pending();self.assertTrue(self.manager.recovery_ready())
        self.manager.preview_rollback(preview.request_id)
        self.store.before_write=lambda _:(_ for _ in ()).throw(RuntimeError('stop'))
        with self.assertRaises(CategoryManagementError):self.manager.rollback(preview.request_id,confirmed=True)
        manager=CategoryManager(self.store,Path(self.tmp.name),123)
        self.assertEqual(manager.pending().status,'rolling_back')
        with self.assertRaises(CategoryManagementError):manager.apply(preview.request_id,resume=True)
        self.assertTrue(manager.recovery_ready())
        self.store.before_write=None
        self.assertEqual(manager.rollback(preview.request_id,confirmed=True,resume=True).status,'rolled_back')

    def test_legacy_pending_blocks_release_and_rollback(self):
        preview=self.pending();path=self.manager._directory(preview.request_id)/(preview.request_id+'-native.json')
        _private_json(path,{'version':1,'finished':False})
        self.assertEqual(self.manager.pending().status,'operator_required')
        with self.assertRaises(CategoryManagementError):self.manager.recovery_ready()
        with self.assertRaises(CategoryManagementError):self.manager.preview_rollback(preview.request_id)

    def test_conflict_status_is_safe_and_does_not_suggest_repeating_forward(self):
        preview=self.pending();self.store.book.sheets['Аналитика'].cells[1,4]=12
        with self.assertRaises(CategoryManagementError):self.manager.apply(preview.request_id,resume=True)
        status=self.manager.pending()
        self.assertEqual(status.status,'conflict')
        self.assertIn('period_conflict',status.text)
        self.assertIn(preview.request_id,status.text)
        self.assertNotIn('private secret',status.text)
        self.manager.preview_rollback(preview.request_id)
        self.manager.rollback(preview.request_id,confirmed=True)
        self.assertEqual(self.store.book.sheets['Аналитика'].cells[1,4],12)
