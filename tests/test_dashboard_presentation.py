from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dashboard_fixtures import DashboardTarget, attach
from test_dashboard_changes import fixture
from finance_bot.category_changes import prepare_change
from finance_bot.dashboard_changes import prepare_presentation, digest, validate_presentation, apply_presentation
from finance_bot.migration_apply import MigrationError
from finance_bot.taxonomy import new_id


class DashboardPresentationTests(unittest.TestCase):
    def setUp(self):
        self.source,self.registry=fixture()
        self.target=DashboardTarget()
        self.change=prepare_change(self.source,spreadsheet_id='copy-id',expected_revision=0,request_id=new_id(),action='create',name='Новая',subcategories=('Раз','Два','Три'))
    def plan(self):return prepare_presentation(self.target,self.source,self.change.output)
    def test_only_main_range_changes_and_retry_keeps_ids_positions_styles(self):
        before=deepcopy(self.target.metadata)
        plan=self.plan();apply_presentation(self.target,plan,digest(plan))
        actual=self.target.metadata
        expected=deepcopy(before)
        for field in ('domain','series'):
            expected['sheets'][0]['charts'][0]['spec']['pieChart'][field]['sourceRange']['sources'][0]['endRowIndex']=10
        self.assertEqual(actual,expected)
        self.assertEqual([list(req)[0] for req in self.target.requests],['copyPaste','updateChartSpec'])
        requests=deepcopy(self.target.requests)
        apply_presentation(self.target,plan,digest(plan))
        self.assertEqual(self.target.requests,requests)
    def test_child_and_income_have_no_native_presentation_requests(self):
        for params in (dict(parent_id=self.registry.categories[0].category_id,name='Добавка'),dict(name='Доходы',transaction_type='Доход',subcategories=('Первая',))):
            change=prepare_change(self.source,spreadsheet_id='copy-id',expected_revision=0,request_id=new_id(),action='create',**params)
            plan=prepare_presentation(self.target,self.source,change.output)
            apply_presentation(self.target,plan,digest(plan))
            self.assertEqual(self.target.requests,[])
    def test_missing_ambiguous_malformed_chart_and_visual_overlap_refuse(self):
        for mode in ('missing','duplicate','malformed','overlap','grid'):
            with self.subTest(mode=mode):
                target=DashboardTarget();sheet=target.metadata['sheets'][0]
                if mode=='missing':sheet['charts'].pop(0)
                if mode=='duplicate':
                    chart=deepcopy(sheet['charts'][0]);chart['chartId']=83;sheet['charts'].append(chart)
                if mode=='malformed':sheet['charts'][0]['spec']['pieChart']['series']['sourceRange']['sources'][0]['endRowIndex']=999
                if mode=='overlap':
                    sheet['charts'][0]['position']['overlayPosition']['anchorCell']['rowIndex']=9
                if mode=='grid':sheet['properties']['gridProperties']['rowCount']=9
                with self.assertRaises(MigrationError):prepare_presentation(target,self.source,self.change.output)
                self.assertEqual(target.requests,[])
    def test_changed_style_position_size_or_extra_chart_invalidate_preview(self):
        plan=self.plan()
        for field in ('title','position','size','extra','rows'):
            with self.subTest(field=field):
                target=DashboardTarget();sheet=target.metadata['sheets'][0];chart=sheet['charts'][0]
                if field=='title':chart['spec']['title']='Edited'
                if field=='position':chart['position']['overlayPosition']['anchorCell']['rowIndex']=40
                if field=='size':chart['position']['overlayPosition']['widthPixels']=600
                if field=='extra':sheet['charts'].append(dict(chartId=999))
                if field=='rows':sheet['data'][0]['rowMetadata'][0]={'pixelSize':40}
                with self.assertRaises(MigrationError):validate_presentation(target,plan,digest(plan),resume=True)
                self.assertEqual(target.requests,[])
    def test_missing_or_tampered_plan_refuses_before_any_native_write(self):
        from finance_bot.category_changes_native import apply_change_to_google_copy
        for change in (self.change,replace(attach(self.change,self.source,self.target),presentation_digest='bad')):
            with tempfile.TemporaryDirectory() as directory, patch('finance_bot.category_changes_native.read_google_spreadsheet',return_value=self.source),patch('finance_bot.category_changes_native._write_sheet') as write:
                with self.assertRaises(MigrationError):apply_change_to_google_copy(self.source,change,self.target,Path(directory),confirmed=True,maintenance_confirmed=True)
                write.assert_not_called();self.assertEqual(list(Path(directory).iterdir()),[])
    def test_bad_native_readback_keeps_failure_visible(self):
        plan=self.plan()
        with patch.object(self.target,'batch_update'):
            with self.assertRaisesRegex(MigrationError,'подтвердить'):apply_presentation(self.target,plan,digest(plan))
