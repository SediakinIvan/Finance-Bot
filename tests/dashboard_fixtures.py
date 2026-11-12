from copy import deepcopy
from dataclasses import replace
from finance_bot.dashboard_changes import prepare_presentation, digest


def metadata():
    def source(col):
        return {'sourceRange': {'sources': [{'sheetId': 42, 'startRowIndex': 8, 'endRowIndex': 9, 'startColumnIndex': col, 'endColumnIndex': col+1}]}}
    return {'sheets': [{'properties': {'sheetId': 42, 'title': 'Аналитика', 'gridProperties': {'rowCount': 1000, 'columnCount': 26}},
        'charts': [{'chartId': 81, 'spec': {'title': 'Мои категории', 'fontName': 'Arial', 'pieChart': {'domain': source(0), 'series': source(1), 'legendPosition': 'RIGHT_LEGEND'}},
            'position': {'overlayPosition': {'anchorCell': {'sheetId': 42, 'rowIndex': 28, 'columnIndex': 0}, 'widthPixels': 510, 'heightPixels': 190}}},
            {'chartId': 82, 'spec': {'title': 'Existing detail', 'basicChart': {'chartType': 'COLUMN'}},
             'position': {'overlayPosition': {'anchorCell': {'sheetId': 42, 'rowIndex': 9, 'columnIndex': 6}, 'widthPixels': 370, 'heightPixels': 190}}}],
        'data': [{'rowMetadata': [{'pixelSize': 21}]*50, 'columnMetadata': [{'pixelSize': 100}]*26}]}]}


class DashboardTarget:
    id = 'copy-id'
    def __init__(self):
        self.metadata = metadata()
        self.requests = []
    def fetch_sheet_metadata(self, params=None):return deepcopy(self.metadata)
    def batch_update(self, body):
        self.requests.extend(deepcopy(body['requests']))
        for request in body['requests']:
            if 'updateChartSpec' in request:
                edit=request['updateChartSpec']
                next(chart for chart in self.metadata['sheets'][0]['charts'] if chart['chartId']==edit['chartId'])['spec']=deepcopy(edit['spec'])


def attach(change, source, target):
    plan=prepare_presentation(target,source,change.output)
    return replace(change,presentation=plan,presentation_digest=digest(plan))
