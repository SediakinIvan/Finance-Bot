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
from finance_bot.category_projections import LABEL_SHEET,ARCHIVE_COLUMNS_SHEET,sheet_rows,table_sheet
from finance_bot.taxonomy import new_id


class GridCapacityTests(unittest.TestCase):
    def setUp(self):
        source,registry=fixture()
        for index in range(3):
            source=prepare_change(source,spreadsheet_id='copy-id',expected_revision=index,request_id=new_id(),action='create',name='Доп '+str(index),subcategories=('Одна',)).output
        categories=sheet_rows(source.sheets['_financebot_categories'])[1:]
        expense=[row for row in categories if row[1]=='Расход']
        self.assertEqual(len(expense),4)
        source.sheets[ARCHIVE_COLUMNS_SHEET]=table_sheet(ARCHIVE_COLUMNS_SHEET,[['category_id','column'],*[[row[0],i+5] for i,row in enumerate(expense)]])
        for i,parent in enumerate(expense):source.sheets['Архив'].cells[1,i+5]=parent[2];source.sheets['Архив'].cells[2,i+5]=100
        labels=[row for row in sheet_rows(source.sheets[LABEL_SHEET]) if row[0]!='Архив']
        while len(labels)<437:labels.append(['Архив (нет '+str(len(labels))+')','A9','category',expense[0][0],''])
        source.sheets[LABEL_SHEET]=table_sheet(LABEL_SHEET,labels)
        self.source=source;self.target=NativeTarget(source)
        self.target.grids[LABEL_SHEET]={'rowCount':437,'columnCount':11}
        for key in ('domain','series'):
            self.target.metadata['sheets'][0]['charts'][0]['spec']['pieChart'][key]['sourceRange']['sources'][0]['endRowIndex']=12
        self.change=attach(prepare_change(source,spreadsheet_id='copy-id',expected_revision=3,request_id=new_id(),action='rename',entity_id=expense[0][0],name='Новое имя'),source,self.target)
        self.assertEqual(self.change.output.sheets[LABEL_SHEET].last_row,441)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def apply(self):
        with self.target.read_patch():apply_change_to_google_copy(self.source,self.change,self.target,self.root,confirmed=True,maintenance_confirmed=True)
    def test_real_437_to_441_boundary_grows_before_read_or_write(self):
        self.apply()
        self.assertEqual(self.target.grids[LABEL_SHEET]['rowCount'],441)
        self.assertEqual(self.target.book.sheets['Архив'].cells[1,5],'Новое имя')
        growth=[r for r in self.target.requests if 'appendDimension' in r]
        self.assertEqual(len(growth),1);self.assertEqual(growth[0]['appendDimension']['length'],4)
        writes=len(self.target.writes);self.apply();self.assertEqual(len(self.target.writes),writes)
    def test_lost_growth_response_does_not_append_twice(self):
        fired=[]
        def fail(body):
            if not fired and any('appendDimension' in r for r in body['requests']):fired.append(True);raise Timeout('lost')
        self.target.after_native=fail
        with patch('finance_bot.category_changes_native.time.sleep'):self.apply()
        self.assertEqual(sum('appendDimension' in r for r in self.target.requests),1)
    def test_rollback_never_deletes_expanded_rows_or_owner_values(self):
        def fail(body):
            if any('setDataValidation' in r for r in body['requests']):raise RuntimeError('controlled stop')
        self.target.before_native=fail
        with self.assertRaises(RuntimeError):self.apply()
        self.target.before_native=None
        # An unrelated owner cell must survive compensation.
        self.target.book.sheets[LABEL_SHEET].cells[441,10]='owner note'
        with self.target.read_patch():rollback_change_to_google_copy(self.source,self.change,self.target,self.root,confirmed=True)
        self.assertEqual(self.target.grids[LABEL_SHEET]['rowCount'],441)
        self.assertEqual(self.target.book.sheets[LABEL_SHEET].cells[441,10],'owner note')
