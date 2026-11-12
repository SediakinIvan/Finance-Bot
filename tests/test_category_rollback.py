from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from requests.exceptions import Timeout
from category_native_fixture import NativeTarget,attach
from test_dashboard_changes import fixture
from finance_bot.category_changes import prepare_change
from finance_bot.category_changes_native import apply_change_to_google_copy,rollback_change_to_google_copy,prepare_rollback
from finance_bot.category_journal import CategoryConflict,cell_steps,rollback_projection
from finance_bot.category_projections import table_sheet,sheet_rows
from finance_bot.migration_apply import _private_json
from finance_bot.taxonomy import new_id,Period


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.source,self.registry=fixture();self.target=NativeTarget(self.source)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def plan(self,action='rename',**params):
        if action=='rename':params=dict(entity_id=self.registry.categories[0].category_id,name='Покупки',**params)
        return attach(prepare_change(self.source,spreadsheet_id='copy-id',expected_revision=0,request_id=new_id(),action=action,**params),self.source,self.target)
    def apply(self,change):
        with self.target.read_patch():apply_change_to_google_copy(self.source,change,self.target,self.root,confirmed=True,maintenance_confirmed=True)
    def rollback(self,change):
        with self.target.read_patch():rollback_change_to_google_copy(self.source,change,self.target,self.root,confirmed=True)
    def state(self,change):return json.loads((self.root/(change.request_id+'-native.json')).read_text())
    def stop_after_cells(self,change):
        def fail(body):raise RuntimeError('stop before presentation/validations')
        self.target.before_native=fail
        # Rename's unchanged chart step needs no write; dropdowns do.
        with self.assertRaises(RuntimeError):self.apply(change)
        self.target.before_native=None

    def test_pure_inverse_preserves_unrelated_month_and_new_archive(self):
        for action,params in (('rename',{}),('create',dict(name='Новая',subcategories=('Первая',))),('deactivate',dict(entity_id=self.registry.categories[0].category_id,period=Period(2026,11)))):
            self.setUp();change=self.plan(action,**params)
            current=deepcopy(change.output);current.sheets['Аналитика'].cells[1,4]=12
            current.sheets['Архив (другой новый)']=table_sheet('Архив (другой новый)',[['unchanged',300]])
            restored=rollback_projection(self.source,change.output,current,cell_steps(self.source,change.output,self.target),self.target)
            self.assertEqual(restored.sheets['Аналитика'].cells[1,4],12)
            self.assertEqual(restored.sheets['Архив (другой новый)'],current.sheets['Архив (другой новый)'])
            self.assertEqual(restored.sheets['_financebot_categories'].cells[2,3],self.source.sheets['_financebot_categories'].cells[2,3])
            self.assertEqual(restored.sheets['Траты'].cells[2,4],100)

    def test_rollback_partial_rename_keeps_month_and_money_and_has_monotonic_revision(self):
        change=self.plan();self.stop_after_cells(change)
        self.target.book.sheets['Аналитика'].cells[1,4]=12
        self.rollback(change)
        self.assertEqual(self.target.book.sheets['Траты'].cells[2,2],self.source.sheets['Траты'].cells[2,2])
        self.assertEqual(self.target.book.sheets['Аналитика'].cells[1,4],12)
        self.assertEqual(self.target.book.sheets['Архив'].cells[2,5],900)
        state=self.state(change);self.assertEqual(state['phase'],'rolled_back')
        from finance_bot.category_schema import WorkbookMetadata,METADATA_SHEET
        WorkbookMetadata.from_rows(sheet_rows(self.target.book.sheets[METADATA_SHEET])).require_ready('copy-id',expected_revision=2)
        writes=len(self.target.writes);self.rollback(change);self.assertEqual(len(self.target.writes),writes)
        with self.assertRaises(CategoryConflict):self.apply(change)

    def test_conflicting_label_missing_sheet_or_new_dependency_refuses_before_compensation(self):
        for mode in ('label','sheet','new_dependency'):
            self.setUp();change=self.plan('create',name='Новая',subcategories=('Первая',)) if mode=='new_dependency' else self.plan()
            self.stop_after_cells(change)
            if mode=='label':self.target.book.sheets['Архив'].cells[1,5]='Owner edit'
            if mode=='sheet':self.target.book.sheets.pop('Архив')
            if mode=='new_dependency':
                category_id=sheet_rows(change.output.sheets['_financebot_categories'])[-1][0]
                self.target.book.sheets['Траты'].cells[3,7]=category_id
            before=len(self.target.writes);native=len(self.target.requests)
            with self.assertRaises(CategoryConflict):self.rollback(change)
            self.assertEqual(len(self.target.writes),before);self.assertEqual(len(self.target.requests),native)

    def test_rollback_restart_at_each_written_step(self):
        change=self.plan();self.stop_after_cells(change)
        names=[step['id'] for step in self.state(change)['steps']]+['rollback_publish']
        for name in names:
            self.setUp();change=self.plan();self.stop_after_cells(change);fired=[]
            def save(path,state):
                all_steps=state['steps']+[state.get('rollback_publish',{})]
                if not fired and any(s.get('id')==name and s.get('phase') == ('verified' if name=='rollback_publish' else 'reverted') for s in all_steps) and state['phase'] in {'rolling_back','rollback_finalizing'}:
                    fired.append(True);raise RuntimeError('crash rollback')
                _private_json(path,state)
            with patch('finance_bot.category_changes_native._private_json',side_effect=save),self.assertRaises(RuntimeError):self.rollback(change)
            self.rollback(change)
            self.assertEqual(self.state(change)['phase'],'rolled_back')

    def test_lost_rollback_response_and_forward_race_are_safe(self):
        change=self.plan();self.stop_after_cells(change);fired=[]
        def crash(body):
            if not fired:fired.append(True);raise Timeout('lost')
        self.target.after_write=crash
        with patch('finance_bot.category_changes_native.time.sleep'):self.rollback(change)
        self.assertEqual(self.state(change)['phase'],'rolled_back')

    def test_creation_rollback_restores_main_chart_range_without_deleting_chart(self):
        change=self.plan('create',name='Новая',subcategories=('Первая','Вторая'))
        # Fail at validations, after chart enlargement.
        def fail(body):
            if any('setDataValidation' in r for r in body['requests']):raise RuntimeError('stop')
        self.target.before_native=fail
        with self.assertRaises(RuntimeError):self.apply(change)
        self.target.before_native=None;self.rollback(change)
        self.assertEqual(self.target.metadata['sheets'][0]['charts'],change.presentation['before']['charts'])
        self.assertFalse(any('deleteEmbeddedObject' in r or 'addChart' in r for r in self.target.requests))

    def test_moved_pair_rollback_retains_historical_parent(self):
        from finance_bot.category_schema import WorkbookMetadata,METADATA_SHEET,REGISTRY_HEADERS,parse_registry
        added=self.plan('create',name='Получатель',subcategories=('Другая',))
        self.source=deepcopy(added.output);self.target=NativeTarget(self.source)
        meta=WorkbookMetadata.from_rows(sheet_rows(self.source.sheets[METADATA_SHEET]))
        registry=parse_registry(meta,{n:sheet_rows(self.source.sheets[n]) for n in REGISTRY_HEADERS})
        change=prepare_change(self.source,spreadsheet_id='copy-id',expected_revision=1,request_id=new_id(),action='move',entity_id=self.registry.subcategories[0].subcategory_id,parent_id=registry.categories[-1].category_id)
        projected=rollback_projection(self.source,change.output,change.output,cell_steps(self.source,change.output,self.target),self.target)
        self.assertEqual(projected.sheets['Траты'].cells[2,7],self.source.sheets['Траты'].cells[2,7])
        self.assertEqual(projected.sheets['_financebot_subcategories'].cells[2,2],self.registry.categories[0].category_id)

    def test_old_journal_cannot_be_rolled_back_by_guessing(self):
        change=self.plan();_private_json(self.root/(change.request_id+'-native.json'),{'version':1})
        with self.assertRaises(CategoryConflict):self.rollback(change)
        self.assertEqual(self.target.writes,[])

    def test_rollback_restores_new_summary_row_format(self):
        self.target.formats[8,1]={'numberFormat':{'type':'NUMBER','pattern':'0.00'}}
        self.target.formats[9,1]={'backgroundColor':{'red':.25}}
        original=deepcopy(self.target.formats[9,1])
        change=self.plan('create',name='Новая',subcategories=('Первая',))
        def fail(body):
            if any('setDataValidation' in r for r in body['requests']):raise RuntimeError('stop')
        self.target.before_native=fail
        with self.assertRaises(RuntimeError):self.apply(change)
        self.assertNotEqual(self.target.formats[9,1],original)
        self.target.before_native=None;self.rollback(change)
        self.assertEqual(self.target.formats[9,1],original)
