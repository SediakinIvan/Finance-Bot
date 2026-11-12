import unittest
from finance_bot.report_presentation import report_presentation_requests, apply_report_presentation
from finance_bot.category_projections import table_sheet, LABEL_HEADERS


class ReportPresentationTests(unittest.TestCase):
    def test_native_metadata_ids_do_not_trigger_one_extra_read_per_tab(self):
        class NativeMetadataBook:
            def fetch_sheet_metadata(self, params):
                return {'sheets': [{'properties': {'sheetId': i, 'title': title}}
                                  for i, title in enumerate(('Аналитика','Траты','_financebot_detail'))]}
            def worksheet(self, name):
                raise AssertionError('Native metadata already supplies every sheet ID')
            def batch_update(self, body):
                self.body = body
        book = NativeMetadataBook()
        apply_report_presentation(book, table_sheet('Аналитика',[['Title']]), [LABEL_HEADERS])
        self.assertTrue(book.body['requests'])

    def test_formats_and_chart_ranges_preserve_controls_and_do_not_overlay_data(self):
        labels = [
            ['Аналитика','A9','category','parent',''],
            ['Аналитика','G1','heading','parent','','Продукты: подкатегории'],
            ['Аналитика','G3','subcategory','parent','one'],
            ['Аналитика','G4','subcategory','parent','two'],
        ]
        requests = report_presentation_requests(42, labels, 1, (999,))
        self.assertEqual(requests[0], {'deleteEmbeddedObject': {'objectId': 999}})
        self.assertFalse(any('updateCells' in r for r in requests))
        charts = [r['addChart']['chart'] for r in requests if 'addChart' in r]
        self.assertEqual(len(charts), 2)
        for chart in charts:
            pie = chart['spec']['pieChart']
            for key in ('domain','series'):
                self.assertEqual(pie[key]['sourceRange']['sources'][0]['sheetId'], 42)
        self.assertGreater(charts[1]['position']['overlayPosition']['anchorCell']['rowIndex'], 3)
        masked = [r['repeatCell'] for r in requests if r.get('repeatCell',{}).get('cell',{}).get('userEnteredFormat',{}).get('numberFormat',{}).get('pattern') == ';;;']
        self.assertEqual(masked[0]['range']['startRowIndex'], 7)
        percentage = [r['repeatCell'] for r in requests if r.get('repeatCell',{}).get('cell',{}).get('userEnteredFormat',{}).get('numberFormat',{}).get('type') == 'PERCENT']
        self.assertEqual(percentage[0]['range']['startColumnIndex'], 2)
