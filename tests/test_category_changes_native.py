from dashboard_fixtures import DashboardTarget, attach
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from finance_bot.category_changes import prepare_change
from finance_bot.category_changes_native import apply_change_to_google_copy
from finance_bot.category_migration import _coordinate
from finance_bot.category_projections import sheet_rows, table_sheet
from finance_bot.category_schema import AccessMode, METADATA_SHEET, MigrationState, WorkbookMetadata
from finance_bot.migration_apply import MigrationError, prepare_migration
from finance_bot.taxonomy import new_id
from test_migration_apply import migration_fixture


class NativeCategoryChangeTests(unittest.TestCase):
    def test_partial_rename_resumes_and_preserves_archive_amounts(self):
        source, registry, reviewed = migration_fixture()
        source = prepare_migration(source, registry, reviewed).output
        source.sheets[METADATA_SHEET] = table_sheet(METADATA_SHEET, WorkbookMetadata("copy-id", 2, 0, MigrationState.READY, AccessMode.OPEN).to_rows())
        from finance_bot.category_projections import LABEL_SHEET
        labels=sheet_rows(source.sheets[LABEL_SHEET])
        labels.append(['Архив (пропавший снимок)','A9','category',registry.categories[0].category_id,''])
        source.sheets[LABEL_SHEET]=table_sheet(LABEL_SHEET,labels)
        request_id = new_id()
        change = prepare_change(source, spreadsheet_id="copy-id", expected_revision=0, request_id=request_id, action="rename", entity_id=registry.categories[0].category_id, name="Покупки")
        current = deepcopy(source)
        class Target(DashboardTarget):
            id = "copy-id"
            fail = True
            def values_batch_update(self, body):
                for item in body["data"]:
                    name, cell = item["range"].rsplit("!", 1)
                    title = name[1:-1].replace("''", "'")
                    if title == "Архив" and self.fail:
                        raise RuntimeError("simulated failure on archive rename")
                    coordinate = _coordinate(cell)
                    value = item["values"][0][0]
                    if body['valueInputOption'] == 'USER_ENTERED':current.sheets[title].formulas[coordinate]=value
                    else:current.sheets[title].cells[coordinate]=value
        target = Target()
        change = attach(change, source, target)
        def write_sheet(_target, sheet):
            current.sheets[sheet.title] = deepcopy(sheet)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("finance_bot.category_changes_native.read_google_spreadsheet", side_effect=lambda _: deepcopy(current)), patch("finance_bot.category_changes_native._write_sheet", side_effect=write_sheet), patch("finance_bot.report_presentation.apply_report_presentation"), patch("finance_bot.category_dropdowns.apply_category_dropdowns"):
                with self.assertRaises(RuntimeError):
                    apply_change_to_google_copy(source, change, target, root, confirmed=True, maintenance_confirmed=True)
                meta = WorkbookMetadata.from_rows(sheet_rows(current.sheets[METADATA_SHEET]))
                self.assertEqual(meta.pending_request_id, request_id)
                with self.assertRaises(ValueError):
                    meta.require_ready("copy-id")
                target.fail = False
                current.sheets["Траты"].cells[2, 4] = 101
                with self.assertRaises(MigrationError):
                    apply_change_to_google_copy(source, change, target, root, confirmed=True, maintenance_confirmed=True)
                self.assertEqual(current.sheets["Траты"].cells[2, 4], 101)
                current.sheets["Траты"].cells[2, 4] = 100
                apply_change_to_google_copy(source, change, target, root, confirmed=True, maintenance_confirmed=True)
                self.assertEqual(current.sheets["Архив"].cells[1, 5], "Покупки")
                self.assertEqual(current.sheets["Архив"].cells[2, 5], 900)
                self.assertIn(labels[-1],sheet_rows(current.sheets[LABEL_SHEET]))
                self.assertNotIn('Архив (пропавший снимок)',current.sheets)
                self.assertEqual(target.requests, [])
                meta = WorkbookMetadata.from_rows(sheet_rows(current.sheets[METADATA_SHEET]))
                meta.require_ready("copy-id", expected_revision=1)
                apply_change_to_google_copy(source, change, target, root, confirmed=True, maintenance_confirmed=True)


if __name__ == "__main__":
    unittest.main()

class NativeCategoryCreateTests(unittest.TestCase):
    def test_failed_dropdowns_keep_pending_then_resume_without_duplicate_create(self):
        from collections import Counter
        source,registry,reviewed=migration_fixture()
        source=prepare_migration(source,registry,reviewed).output
        source.sheets[METADATA_SHEET]=table_sheet(METADATA_SHEET,WorkbookMetadata('copy-id',2,0,MigrationState.READY,AccessMode.OPEN).to_rows())
        from finance_bot.category_projections import LABEL_SHEET
        labels=sheet_rows(source.sheets[LABEL_SHEET])
        labels.append(['Архив (удалённый снимок)','A9','category',registry.categories[0].category_id,''])
        source.sheets[LABEL_SHEET]=table_sheet(LABEL_SHEET,labels)
        change=prepare_change(source,spreadsheet_id='copy-id',expected_revision=0,request_id=new_id(),action='create',name='Новая',subcategories=('Первая',))
        current=deepcopy(source);writes=Counter()
        class Target(DashboardTarget):
            id='copy-id'
            def values_batch_update(self,body):
                for item in body['data']:
                    name,cell=item['range'].rsplit('!',1)
                    title=name[1:-1].replace("''", "'")
                    coordinate=_coordinate(cell);value=item['values'][0][0]
                    if body['valueInputOption']=='USER_ENTERED':current.sheets[title].formulas[coordinate]=value
                    else:current.sheets[title].cells[coordinate]=value
        target=Target()
        change=attach(change,source,target)
        def write_sheet(target,sheet):
            writes[sheet.title]+=1;current.sheets[sheet.title]=deepcopy(sheet)
        def fail_dropdowns(target,output):
            meta=WorkbookMetadata.from_rows(sheet_rows(current.sheets[METADATA_SHEET]))
            self.assertEqual(meta.pending_request_id,change.request_id)
            raise RuntimeError('dropdown service unavailable')
        with tempfile.TemporaryDirectory() as directory:
            with patch('finance_bot.category_changes_native.read_google_spreadsheet',side_effect=lambda _:deepcopy(current)), patch('finance_bot.category_changes_native._write_sheet',side_effect=write_sheet), patch('finance_bot.report_presentation.apply_report_presentation'), patch('finance_bot.category_dropdowns.apply_category_dropdowns',side_effect=fail_dropdowns) as dropdown:
                with self.assertRaises(RuntimeError):apply_change_to_google_copy(source,change,target,Path(directory),confirmed=True,maintenance_confirmed=True)
                counts=writes.copy()
                dropdown.side_effect=None
                apply_change_to_google_copy(source,change,target,Path(directory),confirmed=True,maintenance_confirmed=True)
                for title,count in counts.items():
                    if title != METADATA_SHEET:self.assertEqual(writes[title],count)
                meta=WorkbookMetadata.from_rows(sheet_rows(current.sheets[METADATA_SHEET]))
                meta.require_ready('copy-id',expected_revision=1)
                self.assertEqual(current.sheets['Траты'].cells[2,4],100)
                self.assertIn(labels[-1],sheet_rows(current.sheets[LABEL_SHEET]))
                self.assertNotIn('Архив (удалённый снимок)',current.sheets)
                self.assertNotIn('Аналитика', writes)
                self.assertEqual([list(item)[0] for item in target.requests], ['copyPaste', 'updateChartSpec'])
                self.assertEqual([chart['chartId'] for chart in target.metadata['sheets'][0]['charts']], [81, 82])
