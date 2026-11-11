import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from zipfile import ZipFile

from finance_bot.category_migration import (
    SheetSnapshot, WorkbookSnapshot, inspect_workbook, read_google_spreadsheet, read_xlsx,
)


class MigrationPreflightTests(unittest.TestCase):
    def workbook(self):
        return WorkbookSnapshot("test-fingerprint", {
            "Категории": SheetSnapshot("Категории", {
                (1, 1): "Тип", (1, 2): "Категория", (1, 3): "Подкатегория",
                (2, 1): "Расход", (2, 2): "Транспорт", (2, 3): "Whoosh",
                (3, 1): "Расход", (3, 2): "Транспорт", (3, 3): "Самокаты",
            }),
            "Траты": SheetSnapshot("Траты", {
                **{(1, i): name for i, name in enumerate(("Дата", "Категория", "Подкатегория", "Сумма", "Доход/Расход", "Примечание"), 1)},
                (2, 1): "11.11.2026", (2, 2): "Транспорт", (2, 3): "Whoosh",
                (2, 4): 987654, (2, 5): "Расход", (2, 6): "secret personal note",
            }),
            "Аналитика": SheetSnapshot("Аналитика", {
                (1, 1): "Подкатегории: Транспорт", (3, 1): "Самокаты", (3, 2): 987654,
            }, {(3, 2): '=SUMIFS(Траты!D:D,Траты!C:C,"Whoosh")'}),
            "Архив": SheetSnapshot("Архив", {(1, 5): "Транспорт", (2, 5): 987654}),
            "Архив (Октябрь 2026)": SheetSnapshot("Архив (Октябрь 2026)", {
                (1, 1): "Подкатегории: Транспорт", (3, 1): "Штрафы", (3, 2): 987654,
            }),
        })

    def test_report_exposes_alias_conflict_without_guessing_merge(self) -> None:
        report = inspect_workbook(self.workbook())
        self.assertEqual(len(report["taxonomy_pairs"]), 2)
        self.assertEqual(report["transaction_count"], 1)
        findings = report["findings"]
        self.assertTrue(any(item["code"] == "report_label_differs_from_criterion" and item["criterion"] == "Whoosh" for item in findings))
        self.assertTrue(any(item["code"] == "unmapped_report_subcategory" and item["subcategory"] == "Штрафы" for item in findings))
        self.assertFalse(report["can_apply"])

    def test_report_never_contains_amounts_notes_or_operation_dates(self) -> None:
        serialized = json.dumps(inspect_workbook(self.workbook()), ensure_ascii=False)
        for private_value in ("987654", "secret personal note", "11.11.2026", "SUMIFS"):
            self.assertNotIn(private_value, serialized)

    def test_google_reader_only_uses_read_operations(self) -> None:
        class ReadOnlySheet:
            title = "Траты"

            def get_all_values(self, *, value_render_option):
                return [["Дата", "Категория"], ["11.11.2026", "Транспорт"]]

            def __getattr__(self, name):
                raise AssertionError(f"Unexpected worksheet operation: {name}")

        class ReadOnlyWorkbook:
            def worksheets(self):
                return [ReadOnlySheet()]

            def fetch_sheet_metadata(self, *, params):
                return {"sheets": [{"properties": {"title": "Траты"}, "data": [{"startRow": 1, "startColumn": 1, "rowData": [{"values": [{"dataValidation": {"condition": {"type": "ONE_OF_RANGE"}}}]}]}]}]}

            def __getattr__(self, name):
                raise AssertionError(f"Unexpected workbook operation: {name}")

        snapshot = read_google_spreadsheet(ReadOnlyWorkbook())
        self.assertEqual(snapshot.sheets["Траты"].validations, [{"range": "B2", "type": "ONE_OF_RANGE"}])
        self.assertEqual(len(snapshot.source_fingerprint), 64)

    def test_missing_and_unknown_pairs_are_not_silently_ignored(self) -> None:
        book = self.workbook()
        book.sheets["Траты"].cells[2, 3] = "Неизвестное"
        del book.sheets["Аналитика"]
        report = inspect_workbook(book)
        self.assertTrue(any(item["code"] == "missing_sheet" for item in report["findings"]))
        self.assertTrue(any(item["code"] == "unmapped_transaction_pair" for item in report["findings"]))

    def test_batch_reader_preserves_padding_and_matches_sequential_snapshot(self):
        class Sheet:
            title, row_count, col_count = "Траты", 10, 3
            def get_all_values(self, *, value_render_option):
                return [["Дата", "Категория"], ["11.11.2026", ""]]
        class Sequential:
            def worksheets(self):
                return [Sheet()]
            def fetch_sheet_metadata(self, *, params):
                return {"sheets": [{"properties": {"title": "Траты"}}]}
        class Batched(Sequential):
            def __init__(self):
                self.calls = []
            def values_batch_get(self, ranges, params):
                self.calls.append((ranges, params))
                return {"valueRanges": [{"values": [["Дата", "Категория"], ["11.11.2026"]]}]}
        batch = Batched()
        self.assertEqual(read_google_spreadsheet(batch), read_google_spreadsheet(Sequential()))
        self.assertEqual(len(batch.calls), 2)
        self.assertEqual(batch.calls[0][0], ["'Траты'!A1:C10"])

    def test_xlsx_extraction_is_read_only_and_counts_shared_formulas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.xlsx"
            with ZipFile(source, "w") as archive:
                archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Аналитика" sheetId="1" r:id="rId1"/></sheets></workbook>')
                archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
                archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Метка</t></is></c><c r="B1"><f t="shared" si="0" ref="B1:B2">1+1</f><v>2</v></c></row><row r="2"><c r="B2"><f t="shared" si="0"/><v>2</v></c></row></sheetData><dataValidations><dataValidation type="list" sqref="A2:A155"/></dataValidations></worksheet>')
            original = source.read_bytes()
            snapshot = read_xlsx(source)
            self.assertEqual(source.read_bytes(), original)
            sheet = snapshot.sheets["Аналитика"]
            self.assertEqual(sheet.cell(1, 1), "Метка")
            self.assertEqual(len(sheet.formulas), 2)
            self.assertEqual(sheet.validations, [{"range": "A2:A155", "type": "list"}])


if __name__ == "__main__":
    unittest.main()
