from copy import deepcopy
from unittest.mock import patch
from dashboard_fixtures import DashboardTarget,attach
from finance_bot.category_migration import _coordinate


class NativeTarget(DashboardTarget):
    def __init__(self,source):
        super().__init__();self.book=deepcopy(source);self.writes=[];self.before_write=None;self.after_write=None;self.before_native=None;self.after_native=None
        self.grids={name:{"rowCount":max(100,sheet.last_row),"columnCount":26} for name,sheet in source.sheets.items()}
        self.sheet_ids={name:i+50 for i,name in enumerate(source.sheets)}
    def fetch_sheet_metadata(self,params=None):
        if (params or {}).get('fields')=='sheets(properties(sheetId,title,gridProperties))':
            return {'sheets':[{'properties':{'title':name,'sheetId':self.sheet_ids[name],'gridProperties':deepcopy(grid)}} for name,grid in self.grids.items()]}
        return super().fetch_sheet_metadata(params)
    def values_batch_update(self,body):
        if self.before_write:self.before_write(body)
        self.writes.append(deepcopy(body))
        for item in body['data']:
            name,area=item['range'].rsplit('!',1);name=name[1:-1].replace("''", "'")
            row,col=_coordinate(area.split(':')[0]);sheet=self.book.sheets[name]
            assert row+len(item['values'])-1<=self.grids[name]['rowCount'], 'write exceeds grid limits'
            assert col+max(map(len,item['values']),default=0)-1<=self.grids[name]['columnCount'], 'write exceeds grid limits'
            for i,values in enumerate(item['values']):
                for j,value in enumerate(values):
                    key=row+i,col+j
                    sheet.formulas.pop(key,None)
                    if body['valueInputOption']=='USER_ENTERED' and isinstance(value,str) and value.startswith('='):
                        sheet.formulas[key]=value
                    elif value in ('',None):sheet.cells.pop(key,None)
                    else:sheet.cells[key]=value
        if self.after_write:self.after_write(body)
    def values_batch_get(self,ranges,params=None):
        result=[]
        for area in ranges:
            name,area=area.rsplit('!',1);sheet=self.book.sheets[name[1:-1].replace("''", "'")]
            start,end=area.split(':');r,c=_coordinate(start);rr,cc=_coordinate(end)
            assert rr<=self.grids[sheet.title]['rowCount'] and cc<=self.grids[sheet.title]['columnCount'], 'read exceeds grid limits'
            rows=[]
            for row in range(r,rr+1):
                rows.append([sheet.formulas.get((row,col),sheet.cells.get((row,col),'')) for col in range(c,cc+1)])
            result.append({'values':rows})
        return {'valueRanges':result}
    def read_patch(self):
        return patch('finance_bot.category_changes_native.read_google_spreadsheet',side_effect=lambda _:deepcopy(self.book))

    def batch_update(self,body):
        if self.before_native:self.before_native(body)
        for request in body['requests']:
            if 'appendDimension' in request:
                entry=request['appendDimension'];name=next(n for n,i in self.sheet_ids.items() if i==entry['sheetId'])
                key='rowCount' if entry['dimension']=='ROWS' else 'columnCount'
                self.grids[name][key]+=entry['length']
        super().batch_update(body)
        if self.after_native:self.after_native(body)
