from copy import deepcopy
from dataclasses import replace
import re
import unittest

from finance_bot.category_projections import sheet_rows
from finance_bot.category_schema import AccessMode, METADATA_SHEET, MigrationState, WorkbookMetadata
from finance_bot.migration_apply import prepare_migration
from finance_bot.sheets import SheetsError, StaleTransactionError
from finance_bot.taxonomy import new_id
from finance_bot.v2_sheets import TaxonomyChangedError, V2GoogleSheetsStore
from test_migration_apply import migration_fixture


class FakeSheet:
    def __init__(self, title, rows, book):
        self.title, self.rows, self.book = title, deepcopy(rows), book
        self.row_count = max(1000, len(rows))
        self.col_count = max((len(row) for row in rows), default=1)
        self.id = len(book.sheets) + 1 if hasattr(book, "sheets") else 1

    def get_all_values(self, **kwargs):
        return deepcopy(self.rows)

    def add_rows(self, count):
        self.row_count += count

    def resize(self, *, rows, cols):
        self.row_count, self.col_count = rows, cols

    def clear(self):
        self.rows = []

    def update(self, *, values, range_name, value_input_option):
        self.book.values_batch_update({"valueInputOption": value_input_option, "data": [{"range": "'" + self.title + "'!" + range_name, "values": values}]})

    def delete_rows(self, index):
        self.book.deleted.append(index)
        self.rows.pop(index - 1)


class FakeBook:
    def __init__(self, snapshot):
        self.id = "copy-id"
        self.writes, self.deleted = [], []
        self.sheets = {name: FakeSheet(name, sheet_rows(sheet), self) for name, sheet in snapshot.sheets.items()}

    def worksheet(self, title):
        return self.sheets[title]

    def worksheets(self):
        return list(self.sheets.values())

    def fetch_sheet_metadata(self, params=None):
        return {"sheets": [{"properties": {"title": name}, "tables": []} for name in self.sheets]}

    def add_worksheet(self, *, title, rows, cols):
        sheet = FakeSheet(title, [], self)
        sheet.row_count, sheet.col_count = rows, cols
        self.sheets[title] = sheet
        return sheet

    def batch_update(self, body):
        self.writes.append(deepcopy(body))

    def values_batch_update(self, body):
        self.writes.append(deepcopy(body))
        for item in body["data"]:
            match = re.fullmatch(r"'(.+)'!([A-Z]+)([0-9]+)(?::([A-Z]+)([0-9]+))?", item["range"])
            if not match:
                raise AssertionError(item["range"])
            title = match[1].replace("''", "'")
            offset = 0
            for char in match[2]:
                offset = offset * 26 + ord(char) - 64
            offset -= 1
            rows = self.sheets[title].rows
            for index, values in enumerate(item["values"], int(match[3]) - 1):
                while len(rows) <= index:
                    rows.append([])
                rows[index] += [""] * max(0, offset + len(values) - len(rows[index]))
                rows[index][offset:offset + len(values)] = values


class V2StoreTests(unittest.TestCase):
    def setUp(self):
        source, self.taxonomy, reviewed = migration_fixture()
        output = prepare_migration(source, self.taxonomy, reviewed).output
        metadata = WorkbookMetadata("copy-id", 2, 0, MigrationState.READY, AccessMode.OPEN)
        from finance_bot.category_projections import table_sheet
        output.sheets[METADATA_SHEET] = table_sheet(METADATA_SHEET, metadata.to_rows())
        self.book = FakeBook(output)
        self.store = V2GoogleSheetsStore("unused", "copy-id")
        self.store._spreadsheet = self.book

    def append(self, **overrides):
        option = self.store.load_taxonomy()[0]
        kwargs = dict(category_id=option.category_id, subcategory_id=option.subcategory_id,
                      expected_revision=option.revision, transaction_id=new_id())
        kwargs.update(overrides)
        self.store.append_transaction(["12.11.2026", option.category, option.subcategory, 150, "Расход", ""], **kwargs)
        return kwargs

    def test_appends_user_fields_and_ids_in_same_batch(self):
        kwargs = self.append()
        operation = self.book.writes[-1]["data"][0]
        self.assertEqual(operation["range"], "'Траты'!A3:K3")
        values = operation["values"][0]
        self.assertEqual(values[6:9], [kwargs["category_id"], kwargs["subcategory_id"], kwargs["transaction_id"]])
        self.assertEqual(values[9], "valid")
        self.assertEqual(len(self.book.writes[-1]["data"]), 2)

    def test_stale_revision_is_rejected_before_writing(self):
        with self.assertRaises(TaxonomyChangedError):
            self.append(expected_revision=1)
        self.assertEqual(self.book.writes, [])

    def test_snapshot_capacity_grows_with_new_operations(self):
        identity_sheet = self.book.sheets["_financebot_row_snapshots"]
        identity_sheet.row_count = 2
        self.append()
        self.assertEqual(identity_sheet.row_count, 3)
        self.assertEqual(len(identity_sheet.rows), 3)

    def test_legacy_or_maintenance_workbook_is_rejected(self):
        rows = self.book.sheets[METADATA_SHEET].rows
        for row in rows:
            if row[0] == "access_mode":
                row[1] = "maintenance"
        with self.assertRaises(SheetsError):
            self.store.load_taxonomy()
        self.assertEqual(self.book.writes, [])
        del self.book.sheets[METADATA_SHEET]
        with self.assertRaises(SheetsError):
            self.store.get_recent_transactions()

    def test_manual_change_reconciles_ids_and_bad_pair_blocks_report(self):
        self.book.sheets["Траты"].rows[1][3] = 250
        records = self.store.get_recent_transactions()
        self.assertEqual(records[0].values[3], "250")
        self.assertTrue(records[0].transaction_id)
        self.book.sheets["Траты"].rows[1][2] = "Несуществующая"
        with self.assertRaises(SheetsError):
            self.store.get_recent_transactions()
        self.assertEqual(self.book.sheets["Траты"].rows[1][9], "invalid_category_pair")

    def test_sort_before_delete_cannot_delete_different_operation(self):
        self.append()
        selected = self.store.get_recent_transactions()[0]
        rows = self.book.sheets["Траты"].rows
        rows[1], rows[2] = rows[2], rows[1]
        with self.assertRaises(StaleTransactionError):
            self.store.delete_transaction(selected)
        self.assertEqual(self.book.deleted, [])

    def test_unchanged_selected_transaction_can_be_deleted(self):
        selected = self.store.get_recent_transactions()[0]
        self.store.delete_transaction(selected)
        self.assertEqual(self.book.deleted, [2])

    def test_uncertain_write_is_not_retried(self):
        original = self.book.values_batch_update
        calls = []
        def uncertain(body):
            calls.append(body)
            if any("!A3:K3" in item["range"] for item in body["data"]):
                original(body)
                raise TimeoutError("uncertain result")
            original(body)
        self.book.values_batch_update = uncertain
        with self.assertRaises(SheetsError):
            self.append()
        self.assertEqual(sum(any("!A3:K3" in item["range"] for item in body["data"]) for body in calls), 1)
        self.assertEqual(len(self.book.sheets["Траты"].rows), 3)

    def test_first_append_after_reconciling_manual_row_succeeds_without_retry(self):
        row=self.book.sheets['Траты'].rows[1]
        row[6:11]=['']*5
        kwargs=self.append()
        self.assertEqual(self.book.sheets['Траты'].rows[1][9],'valid')
        self.assertEqual(self.book.sheets['Траты'].rows[2][8],kwargs['transaction_id'])
        self.assertEqual(sum(any('!A3:K3' in item['range'] for item in body.get('data',[])) for body in self.book.writes),1)

    def test_unchanged_ledger_reads_do_not_write_service_fields_again(self):
        self.store.get_recent_transactions()
        first=len(self.book.writes)
        self.store.get_recent_transactions()
        self.assertEqual(len(self.book.writes),first)
        self.assertEqual(first,0)

    def test_prewrite_quota_failure_is_reported_as_not_written_without_retry(self):
        from types import SimpleNamespace
        from finance_bot.sheets import TransactionNotWrittenError
        class Quota(Exception):response=SimpleNamespace(status_code=429)
        original=self.store._rows;calls=[]
        def fail(title):
            calls.append(title)
            if title=='Траты':raise Quota('secret API payload')
            return original(title)
        self.store._rows=fail
        with self.assertRaises(TransactionNotWrittenError) as error:self.append()
        self.assertIn('429',str(error.exception));self.assertNotIn('secret',str(error.exception))
        self.assertEqual(self.book.writes,[])


    def test_append_reads_registry_once_and_keeps_concurrent_input_guard(self):
        from unittest.mock import Mock
        option=self.store.load_taxonomy()[0]
        original=self.store._registry
        self.store._registry=Mock(wraps=original)
        self.store.append_transaction(["12.11.2026",option.category,option.subcategory,150,"Расход",""],
            category_id=option.category_id,subcategory_id=option.subcategory_id,expected_revision=option.revision,transaction_id=new_id())
        self.assertEqual(self.store._registry.call_count,1)

    def test_own_reconciliation_does_not_mask_a_concurrent_financial_edit(self):
        from finance_bot.sheets import TransactionNotWrittenError
        row=self.book.sheets['Траты'].rows[1];row[6:11]=['']*5
        original=self.book.values_batch_update
        def write_and_edit(body):
            original(body)
            if any('!G2:K2' in item['range'] for item in body['data']):
                self.book.sheets['Траты'].rows[1][3]=999
        self.book.values_batch_update=write_and_edit
        with self.assertRaises(TransactionNotWrittenError):self.append()
        self.assertEqual(self.book.sheets['Траты'].rows[1][3],999)
        self.assertEqual(len(self.book.sheets['Траты'].rows),2)


    def test_api_sheet_only_ranges_are_valid_without_trailing_separator(self):
        original=self.book
        class ApiBook(FakeBook):
            def values_get(self,area,params=None):
                assert not area.endswith('!'),area
                assert area.startswith("'") and area.endswith("'"),area
                title=area[1:-1].replace("''", "'")
                return {'values':deepcopy(self.sheets[title].rows)}
            def values_batch_get(self,areas,params=None):
                return {'valueRanges':[self.values_get(area,params) for area in areas]}
        native=ApiBook.__new__(ApiBook);native.__dict__=original.__dict__
        self.store._spreadsheet=native
        self.assertTrue(self.store.load_taxonomy())
        self.assertEqual(self.store._rows_many((METADATA_SHEET,'Траты'))[1],self.store._rows('Траты'))



if __name__ == "__main__":
    unittest.main()
