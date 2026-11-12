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
        self.validation_rules = {}
        self.formats = {}
    def fetch_sheet_metadata(self, params=None):
        params=params or {}
        if 'userEnteredFormat' in params.get('fields',''):
            from finance_bot.category_migration import _coordinate
            area=params['ranges'].rsplit('!',1)[1];start,end=area.split(':');r,c=_coordinate(start);rr,cc=_coordinate(end)
            return {'sheets':[{'properties':{'title':'Аналитика'},'data':[{'startRow':r-1,'startColumn':c-1,'rowData':[
                {'values':[{'userEnteredFormat':deepcopy(self.formats.get((row,col),{}))} for col in range(c-1,cc)]} for row in range(r-1,rr)]}]}]}
        if 'Траты' in str(params.get('ranges','')) or 'tables' in params.get('fields',''):
            rows=[]
            for row in range(1,100):
                rows.append({'values':[{'dataValidation':self.validation_rules[row,col]} if (row,col) in self.validation_rules else {} for col in (1,2)]})
            return {'sheets':[{'properties':{'title':'Траты','sheetId':43,'gridProperties':{'rowCount':100,'columnCount':26}},
                               'data':[{'startRow':1,'startColumn':1,'rowData':rows}]}]}
        return deepcopy(self.metadata)
    def batch_update(self, body):
        self.requests.extend(deepcopy(body['requests']))
        for request in body['requests']:
            if 'copyPaste' in request:
                copy=request['copyPaste'];src,dst=copy['source'],copy['destination']
                for r in range(dst['startRowIndex'],dst['endRowIndex']):
                    for c in range(dst['startColumnIndex'],dst['endColumnIndex']):
                        self.formats[r,c]=deepcopy(self.formats.get((src['startRowIndex'],src['startColumnIndex']+c-dst['startColumnIndex']),{}))
            if 'updateCells' in request:
                entry=request['updateCells'];area=entry['range']
                for r,row in enumerate(entry['rows']):
                    for c,cell in enumerate(row['values']):self.formats[area['startRowIndex']+r,area['startColumnIndex']+c]=deepcopy(cell.get('userEnteredFormat',{}))
            if 'setDataValidation' in request:
                entry=request['setDataValidation'];area=entry['range']
                for row in range(area['startRowIndex'],area['endRowIndex']):
                    key=(row,area['startColumnIndex'])
                    if 'rule' in entry:self.validation_rules[key]=deepcopy(entry['rule'])
                    else:self.validation_rules.pop(key,None)
            if 'updateChartSpec' in request:

                edit=request['updateChartSpec']
                next(chart for chart in self.metadata['sheets'][0]['charts'] if chart['chartId']==edit['chartId'])['spec']=deepcopy(edit['spec'])


def attach(change, source, target):
    plan=prepare_presentation(target,source,change.output)
    return replace(change,presentation=plan,presentation_digest=digest(plan))
