from copy import deepcopy
from unittest.mock import patch
from dashboard_fixtures import DashboardTarget,attach
from finance_bot.category_migration import _coordinate


class NativeTarget(DashboardTarget):
    def __init__(self,source):
        super().__init__();self.book=deepcopy(source);self.writes=[];self.before_write=None;self.after_write=None;self.before_native=None;self.after_native=None
    def values_batch_update(self,body):
        if self.before_write:self.before_write(body)
        self.writes.append(deepcopy(body))
        for item in body['data']:
            name,area=item['range'].rsplit('!',1);name=name[1:-1].replace("''", "'")
            row,col=_coordinate(area.split(':')[0]);sheet=self.book.sheets[name]
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
            rows=[]
            for row in range(r,rr+1):
                rows.append([sheet.formulas.get((row,col),sheet.cells.get((row,col),'')) for col in range(c,cc+1)])
            result.append({'values':rows})
        return {'valueRanges':result}
    def read_patch(self):
        return patch('finance_bot.category_changes_native.read_google_spreadsheet',side_effect=lambda _:deepcopy(self.book))

    def batch_update(self,body):
        if self.before_native:self.before_native(body)
        super().batch_update(body)
        if self.after_native:self.after_native(body)
