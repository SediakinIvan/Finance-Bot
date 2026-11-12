from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from finance_bot.category_management import CategoryManager, CategoryManagementError, StaleCategoryPlan, validate_children, validate_name
from finance_bot.category_projections import sheet_rows, table_sheet
from finance_bot.category_schema import WorkbookMetadata, METADATA_SHEET, AccessMode, MigrationState
from finance_bot.migration_apply import prepare_migration, _private_json
from test_migration_apply import migration_fixture


class CategoryManagementTests(unittest.TestCase):
    def setUp(self):
        source, self.registry, reviewed = migration_fixture()
        self.current = prepare_migration(source, self.registry, reviewed).output
        self.current.sheets[METADATA_SHEET] = table_sheet(METADATA_SHEET, WorkbookMetadata('copy-id',2,0,MigrationState.READY,AccessMode.OPEN).to_rows())
        test = self
        from dashboard_fixtures import DashboardTarget
        class Store(DashboardTarget):
            spreadsheet_id = 'copy-id'
            writes = 0
            fail = False
            def _get_spreadsheet(self): return self
            def _rows(self, title): return sheet_rows(test.current.sheets[title])
            def apply_category_change(self, source, change, directory, **kwargs):
                self.writes += 1
                if self.fail:
                    meta = WorkbookMetadata.from_rows(self._rows(METADATA_SHEET))
                    test.current.sheets[METADATA_SHEET] = table_sheet(METADATA_SHEET, replace(meta,access_mode=AccessMode.MAINTENANCE,pending_request_id=change.request_id).to_rows())
                    _private_json(directory/(change.request_id+'-native.json'),{'finished':False})
                    raise RuntimeError('partial write')
                test.current = deepcopy(change.output)
        self.store = Store()
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'journal'
        self.read = patch('finance_bot.category_management.read_google_spreadsheet',side_effect=lambda _:deepcopy(self.current))
        self.read.start();self.addCleanup(self.read.stop)
        self.manager = CategoryManager(self.store,self.root,123)

    def preview(self):
        return self.manager.preview(action='rename',entity_id=self.registry.categories[0].category_id,name='Покупки')

    def test_preview_has_no_remote_writes_and_confirm_is_idempotent_after_restart(self):
        preview = self.preview()
        self.assertEqual(self.store.writes,0)
        path = self.manager._directory(preview.request_id)/'request.json'
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777,0o700)
        result = self.manager.apply(preview.request_id)
        self.assertEqual(result.status,'complete')
        again = CategoryManager(self.store,self.root,123).apply(preview.request_id)
        self.assertEqual(again.status,'complete');self.assertEqual(self.store.writes,1)

    def test_source_or_period_change_requires_new_confirmation(self):
        for coordinate,value in (((1,4),12),((2,4),2027)):
            with self.subTest(coordinate=coordinate):
                preview = self.preview()
                self.current.sheets['Аналитика'].cells[coordinate] = value
                with self.assertRaises(StaleCategoryPlan):self.manager.apply(preview.request_id)
        self.assertEqual(self.store.writes,0)

    def test_cancel_and_wrong_owner_cannot_apply(self):
        preview = self.preview()
        with self.assertRaises(CategoryManagementError):CategoryManager(self.store,self.root,999).apply(preview.request_id)
        self.manager.cancel(preview.request_id)
        with self.assertRaises(CategoryManagementError):self.manager.apply(preview.request_id)
        self.assertEqual(self.store.writes,0)

    def test_partial_request_survives_restart_and_resumes_same_plan(self):
        preview = self.preview();self.store.fail = True
        with self.assertRaises(CategoryManagementError):self.manager.apply(preview.request_id)
        manager = CategoryManager(self.store,self.root,123)
        self.assertEqual(manager.pending().request_id,preview.request_id)
        self.store.fail = False
        self.assertEqual(manager.apply(preview.request_id,resume=True).status,'complete')
        self.assertIsNone(manager.pending())
        self.assertEqual(self.current.sheets['Траты'].cells[2,4],100)

    def test_missing_or_corrupt_journal_does_not_write(self):
        preview = self.preview()
        path = self.manager._directory(preview.request_id)/'request.json'
        path.write_text('{broken')
        with self.assertRaises(CategoryManagementError):self.manager.apply(preview.request_id)
        path.unlink()
        with self.assertRaises(CategoryManagementError):self.manager.apply(preview.request_id)
        self.assertEqual(self.store.writes,0)

    def test_unwritable_journal_blocks_before_source_read(self):
        with patch.object(self.manager,'_directory',side_effect=CategoryManagementError('Журнал недоступен')):
            with self.assertRaises(CategoryManagementError):self.preview()
        self.assertEqual(self.store.writes,0)

    def test_dashboard_change_between_preview_and_confirm_refuses_before_store_write(self):
        preview = self.preview()
        self.store.metadata['sheets'][0]['charts'][0]['spec']['title'] = 'Owner edited'
        with self.assertRaisesRegex(CategoryManagementError, 'после предпросмотра'):
            self.manager.apply(preview.request_id)
        self.assertEqual(self.store.writes, 0)

    def test_old_preview_and_pending_require_explicit_new_preview_or_recovery(self):
        for status in ('preview', 'applying'):
            with self.subTest(status=status):
                preview = self.preview()
                path = self.manager._directory(preview.request_id) / 'request.json'
                saved = json.loads(path.read_text())
                saved['change'].pop('presentation')
                saved['change'].pop('presentation_digest')
                saved['status'] = status
                _private_json(path, saved)
                with self.assertRaisesRegex(CategoryManagementError, 'Старый'):
                    self.manager.apply(preview.request_id, resume=status == 'applying')
                self.assertEqual(json.loads(path.read_text())['status'], 'stale' if status == 'preview' else 'applying')
        self.assertEqual(self.store.writes, 0)

    def test_input_boundaries_and_duplicates(self):
        self.assertEqual(validate_name('  A   B '),'A B')
        self.assertEqual(len(validate_name('x'*64)),64)
        self.assertEqual(len(validate_children('\n'.join(str(i) for i in range(20)))),20)
        for value in ('','x'*65):
            with self.assertRaises(CategoryManagementError):validate_name(value)
        for value in ('one\n ONE ','a\n\nb','\n'.join(str(i) for i in range(21))):
            with self.assertRaises(CategoryManagementError):validate_children(value)

    def test_conflicts_explain_scope_and_do_not_write(self):
        parent=self.registry.categories[0]
        child=self.registry.subcategories[0]
        for params,part in ((dict(action='create',name=parent.name,subcategories=('Первая',)), 'Категория'),
                            (dict(action='create',name=child.name,parent_id=parent.category_id), 'подкатегория')):
            with self.subTest(params=params):
                with self.assertRaisesRegex(CategoryManagementError,part):self.manager.preview(**params)
        self.assertEqual(self.store.writes,0)

    def test_inactive_rename_preview_preserves_ids_and_archive_amount(self):
        parent=self.registry.categories[0]
        preview=self.manager.preview(action='deactivate',entity_id=parent.category_id)
        self.assertIn('2026-11',preview.text)
        self.manager.apply(preview.request_id)
        preview=self.manager.preview(action='rename',entity_id=parent.category_id,name='История покупок')
        self.assertIn(parent.name+' → История покупок',preview.text)
        self.manager.apply(preview.request_id)
        self.assertEqual(self.current.sheets['Траты'].cells[2,7],parent.category_id)
        self.assertEqual(self.current.sheets['Архив'].cells[2,5],900)
        self.assertEqual(self.current.sheets['Архив'].cells[1,5],'История покупок')

    def test_inactive_subcategory_can_be_renamed_without_reidentifying_operations(self):
        child=self.registry.subcategories[0]
        preview=self.manager.preview(action='deactivate',entity_id=child.subcategory_id)
        self.manager.apply(preview.request_id)
        preview=self.manager.preview(action='rename',entity_id=child.subcategory_id,name='Старые покупки')
        self.assertIn(child.name+' → Старые покупки',preview.text)
        self.manager.apply(preview.request_id)
        self.assertEqual(self.current.sheets['Траты'].cells[2,3],'Старые покупки')
        self.assertEqual(self.current.sheets['Траты'].cells[2,8],child.subcategory_id)
        self.assertEqual(self.current.sheets['Траты'].cells[2,4],100)

    def test_move_conflicting_child_is_rejected_before_apply(self):
        from finance_bot.category_schema import parse_registry,REGISTRY_HEADERS
        child=self.registry.subcategories[0]
        preview=self.manager.preview(action='create',name='Получатель',subcategories=(child.name,))
        self.manager.apply(preview.request_id)
        meta=WorkbookMetadata.from_rows(sheet_rows(self.current.sheets[METADATA_SHEET]))
        registry=parse_registry(meta,{name:sheet_rows(self.current.sheets[name]) for name in REGISTRY_HEADERS})
        before=self.store.writes
        with self.assertRaisesRegex(CategoryManagementError,'подкатегория'):
            self.manager.preview(action='move',entity_id=child.subcategory_id,parent_id=registry.categories[-1].category_id)
        self.assertEqual(self.store.writes,before)

    def test_missing_archive_rename_is_actionable_and_does_not_write(self):
        from finance_bot.category_projections import LABEL_SHEET
        rows=sheet_rows(self.current.sheets[LABEL_SHEET])
        rows.append(['Архив (удалённый снимок)','A9','category',self.registry.categories[0].category_id,''])
        self.current.sheets[LABEL_SHEET]=table_sheet(LABEL_SHEET,rows)
        with self.assertRaisesRegex(CategoryManagementError,'отсутствующий архивный лист'):
            self.preview()
        self.assertEqual(self.store.writes,0)
