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
from finance_bot.category_changes_native import apply_change_to_google_copy,rollback_change_to_google_copy
from finance_bot.category_journal import CategoryConflict
from finance_bot.category_schema import METADATA_SHEET,WorkbookMetadata
from finance_bot.category_projections import sheet_rows,table_sheet,LABEL_SHEET
from finance_bot.migration_apply import _private_json
from finance_bot.taxonomy import new_id


class NativeCategoryChangeTests(unittest.TestCase):
    def setUp(self):
        self.source,self.registry=fixture();self.target=NativeTarget(self.source)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def plan(self,**params):
        return attach(prepare_change(self.source,spreadsheet_id='copy-id',expected_revision=0,request_id=new_id(),**params),self.source,self.target)
    def apply(self,change):
        with self.target.read_patch():apply_change_to_google_copy(self.source,change,self.target,self.root,confirmed=True,maintenance_confirmed=True)
    def rollback(self,change):
        with self.target.read_patch():rollback_change_to_google_copy(self.source,change,self.target,self.root,confirmed=True)
    def state(self,change):return json.loads((self.root/(change.request_id+'-native.json')).read_text())
    def rename(self):return self.plan(action='rename',entity_id=self.registry.categories[0].category_id,name='Покупки')

    def test_rename_with_missing_snapshot_is_resumable_and_preserves_finances(self):
        labels=sheet_rows(self.source.sheets[LABEL_SHEET]);labels.append(['Архив (missing)','A9','category',self.registry.categories[0].category_id,''])
        self.source.sheets[LABEL_SHEET]=table_sheet(LABEL_SHEET,labels);self.target.book=deepcopy(self.source)
        change=self.rename()
        def fail(body):
            if any(item['range'].startswith("'Архив'!") for item in body['data']):raise RuntimeError('fail')
        self.target.before_write=fail
        with self.assertRaises(RuntimeError):self.apply(change)
        self.target.before_write=None;self.apply(change)
        self.assertEqual(self.target.book.sheets['Архив'].cell(1,5),'Покупки')
        self.assertEqual(self.target.book.sheets['Архив'].cells[2,5],900)
        self.assertNotIn('Архив (missing)',self.target.book.sheets)
        self.assertIn(labels[-1],sheet_rows(self.target.book.sheets[LABEL_SHEET]))
        self.assertEqual(self.target.book.sheets['Траты'].cells[2,4],100)
        before=len(self.target.writes);self.apply(change);self.assertEqual(len(self.target.writes),before)
        self.assertEqual(self.state(change)['version'],2)
        self.assertEqual((self.root/(change.request_id+'-native.json')).stat().st_mode&0o777,0o600)

    def test_lost_transport_response_reads_back_before_repeat(self):
        change=self.rename();failed=[]
        def fail(body):
            if not failed:failed.append(True);raise Timeout('private secret')
        self.target.after_write=fail
        with patch('finance_bot.category_changes_native.time.sleep'):self.apply(change)
        maintenance=[b for b in self.target.writes if any(i['range'].startswith("'_financebot_meta'!") and i['values']==[['maintenance']] for i in b['data'])]
        self.assertEqual(len(maintenance),1)
        self.assertTrue(self.state(change)['finished'])

    def test_external_period_date_amount_or_removed_operation_conflict(self):
        for name,key,value in (('Аналитика',(1,4),12),('Траты',(2,1),'12.11.2026'),('Траты',(2,4),101),('Траты',(2,9),'')):
            with self.subTest(name=name,key=key):
                self.setUp();change=self.rename()
                self.target.before_write=lambda body: (_ for _ in ()).throw(RuntimeError('stop'))
                with self.assertRaises(RuntimeError):self.apply(change)
                self.target.before_write=None;self.target.book.sheets[name].cells[key]=value
                before=len(self.target.writes)
                with self.assertRaises(CategoryConflict):self.apply(change)
                self.assertEqual(len(self.target.writes),before)
                error=self.state(change)['last_error'];self.assertEqual(error['sheet'],name)
                self.assertNotIn(str(value),json.dumps(error)) if value==101 else None

    def test_legacy_pending_is_not_replanned_and_finished_legacy_is_cached(self):
        change=self.rename();path=self.root/(change.request_id+'-native.json')
        state={'source_digest':change.source_digest,'target_digest':change.target_digest,'finished':False}
        _private_json(path,state)
        with self.assertRaises(CategoryConflict):self.apply(change)
        self.assertEqual(self.target.writes,[])
        state['finished']=True;_private_json(path,state);self.target.book=deepcopy(change.output)
        self.apply(change);self.assertEqual(self.target.writes,[])

    def test_exhausted_transient_attempts_and_access_error_have_safe_diagnostics(self):
        change=self.rename();calls=[]
        def fail(body):calls.append(body);raise Timeout('financial secret')
        self.target.before_write=fail
        with patch('finance_bot.category_changes_native.time.sleep'),self.assertRaises(Timeout):self.apply(change)
        self.assertEqual(len(calls),3)
        self.assertNotIn('secret',json.dumps(self.state(change)))

    def test_crash_after_each_written_step_then_restart(self):
        base=self.rename()
        with self.target.read_patch():
            from finance_bot.category_changes_native import _load_or_create
            path,state=_load_or_create(self.source,base,self.target,self.root)
        for step_id in ['maintenance',*[s['id'] for s in state['steps']],'publish']:
            with self.subTest(step=step_id):
                self.setUp();change=self.rename();fired=[]
                def save(path,value):
                    # Crash after native success, before its verified checkpoint.
                    steps=[*value.get('steps',[]),value.get('pending',{}),value.get('publish',{})]
                    if not fired and any(s.get('id')==step_id and s.get('phase')=='verified' for s in steps):
                        fired.append(True);raise RuntimeError('process crash')
                    _private_json(path,value)
                with patch('finance_bot.category_changes_native._private_json',side_effect=save),self.assertRaises(RuntimeError):self.apply(change)
                self.apply(change)
                self.assertTrue(self.state(change)['finished'])
                self.assertEqual(self.target.book.sheets['Архив'].cells[2,5],900)

    def test_create_lost_chart_and_dropdown_responses_preserve_ids_and_repeat(self):
        for kind in ('updateChartSpec','setDataValidation'):
            self.setUp();change=self.plan(action='create',name='Новая',subcategories=('Раз','Два'))
            fired=[]
            def crash(body):
                if not fired and any(kind in r for r in body['requests']):fired.append(True);raise Timeout('lost native response')
            self.target.after_native=crash
            with patch('finance_bot.category_changes_native.time.sleep'):self.apply(change)
            self.assertEqual([c['chartId'] for c in self.target.metadata['sheets'][0]['charts']],[81,82])
            self.assertFalse(any('addChart' in r or 'deleteEmbeddedObject' in r for r in self.target.requests))
            before=len(self.target.requests);self.apply(change);self.assertEqual(len(self.target.requests),before)
            self.assertTrue(self.state(change)['finished'])

    def test_access_error_is_not_retried_and_is_redacted(self):
        from types import SimpleNamespace
        change=self.rename();calls=[]
        class Forbidden(Exception):response=SimpleNamespace(status_code=403)
        def fail(body):calls.append(body);raise Forbidden('token-private-financial-payload')
        self.target.before_write=fail
        with self.assertRaises(Forbidden):self.apply(change)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.state(change)['last_error']['code'],'google_access')
        self.assertNotIn('token-private',json.dumps(self.state(change)))

    def test_http_code_type_and_error_history_survive_rollback(self):
        from types import SimpleNamespace
        class InvalidRange(Exception):response=SimpleNamespace(status_code=400)
        change=self.rename()
        self.target.before_native=lambda _:(_ for _ in ()).throw(InvalidRange('private payload'))
        with self.assertRaises(InvalidRange):self.apply(change)
        state=self.state(change)
        self.assertEqual(state['last_error']['http_status'],400)
        self.assertEqual(state['last_error']['exception_type'],'InvalidRange')
        self.target.before_native=None
        self.rollback(change)
        state=self.state(change)
        self.assertEqual(state['phase'],'rolled_back')
        self.assertEqual(state['errors'][0]['http_status'],400)
        self.assertNotIn('private payload',json.dumps(state))
