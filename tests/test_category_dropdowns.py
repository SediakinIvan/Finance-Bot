from dataclasses import replace
import unittest
from finance_bot.category_dropdowns import dropdown_requests
from finance_bot.category_schema import TRANSACTION_HEADERS
from finance_bot.taxonomy import Category,Subcategory,Taxonomy,Period,new_id


class DropdownTests(unittest.TestCase):
    def test_new_choices_and_inactive_historical_row_are_preserved_without_values_writes(self):
        old=Category(new_id(),'Расход','История',False,Period(2026,12))
        current=Category(new_id(),'Расход','Новая')
        child=Subcategory(new_id(),current.category_id,'После переноса')
        taxonomy=Taxonomy('copy',1,(old,current),(child,))
        rows=[list(TRANSACTION_HEADERS),['15.12.2026','История','После переноса',100,'Расход','',old.category_id,child.subcategory_id,new_id(),'valid',1]]
        requests=dropdown_requests(42,160,rows,taxonomy)
        self.assertTrue(all(set(r)=={'setDataValidation'} for r in requests))
        first=requests[0]['setDataValidation']
        self.assertEqual([v['userEnteredValue'] for v in first['rule']['condition']['values']],['Новая','История'])
        rest=requests[1]['setDataValidation']
        self.assertEqual(rest['range']['endRowIndex'],160)
        self.assertEqual([v['userEnteredValue'] for v in rest['rule']['condition']['values']],['Новая'])
        children=[r['setDataValidation'] for r in requests if r['setDataValidation']['range']['startColumnIndex']==2]
        self.assertEqual(children[0]['rule']['condition']['values'][0]['userEnteredValue'],'После переноса')
        self.assertNotIn('rule',children[-1])

    def test_rename_and_new_child_follow_registry(self):
        parent=Category(new_id(),'Расход','Категория')
        first=Subcategory(new_id(),parent.category_id,'Новое имя')
        second=Subcategory(new_id(),parent.category_id,'Добавленная')
        taxonomy=Taxonomy('copy',2,(parent,),(first,second))
        rows=[list(TRANSACTION_HEADERS),['','Категория','','','','']]
        requests=dropdown_requests(1,3,rows,taxonomy)
        child=next(r['setDataValidation'] for r in requests if r['setDataValidation']['range']['startColumnIndex']==2)
        self.assertEqual([v['userEnteredValue'] for v in child['rule']['condition']['values']],['Новое имя','Добавленная'])

    def test_native_batch_preserves_table_structure_and_user_values(self):
        from unittest.mock import Mock
        from finance_bot.category_dropdowns import apply_category_dropdowns
        from finance_bot.category_schema import METADATA_SHEET,WorkbookMetadata,MigrationState,AccessMode
        from finance_bot.category_projections import table_sheet
        from finance_bot.migration_apply import prepare_migration
        from test_migration_apply import migration_fixture
        source,registry,reviewed=migration_fixture()
        output=prepare_migration(source,registry,reviewed).output
        output.sheets[METADATA_SHEET]=table_sheet(METADATA_SHEET,WorkbookMetadata('copy-id',2,0,MigrationState.READY,AccessMode.OPEN).to_rows())
        metadata={'sheets':[{'properties':{'title':'Траты','sheetId':42,'gridProperties':{'rowCount':200}},'tables':[{
            'tableId':'table-1','range':{'sheetId':42,'endColumnIndex':11},'columnProperties':[
                {'columnIndex':0,'columnType':'DATE'}, {'columnIndex':1,'columnType':'DROPDOWN'},
                {'columnIndex':2,'columnType':'DROPDOWN'},{'columnIndex':3,'columnType':'CURRENCY'}]}]}]}
        target=Mock();target.fetch_sheet_metadata.return_value=metadata
        apply_category_dropdowns(target,output)
        requests=target.batch_update.call_args.args[0]['requests']
        update=requests[0]['updateTable']
        self.assertEqual(update['fields'],'columnProperties')
        self.assertEqual(update['table']['tableId'],'table-1')
        self.assertEqual(update['table']['columnProperties'][3]['columnType'],'CURRENCY')
        self.assertTrue(all('setDataValidation' in r for r in requests[1:]))
        target.values_batch_update.assert_not_called()
        self.assertEqual(output.sheets['Траты'].cells[2,4],100)

    def test_category_case_and_spaces_use_the_same_resolution_as_ledger(self):
        parent=Category(new_id(),'Расход','Еда офис')
        child=Subcategory(new_id(),parent.category_id,'Столовая')
        taxonomy=Taxonomy('copy',1,(parent,),(child,))
        rows=[list(TRANSACTION_HEADERS),['',' еда  офис ','','','','']]
        requests=dropdown_requests(1,3,rows,taxonomy)
        rules=[r['setDataValidation'] for r in requests if r['setDataValidation']['range']['startColumnIndex']==2]
        self.assertEqual(rules[0]['rule']['condition']['values'],[{'userEnteredValue':'Столовая'}])
