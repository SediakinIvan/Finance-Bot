"""Native chart/format and validation before-images for reversible category edits."""
from copy import deepcopy
from finance_bot.category_schema import METADATA_SHEET, REGISTRY_HEADERS, WorkbookMetadata, parse_registry
from finance_bot.category_projections import sheet_rows
from finance_bot.category_dropdowns import dropdown_requests
from finance_bot.native_migration import ledger_dropdown_table_updates
from finance_bot.dashboard_changes import manifest


def _validation_manifest(metadata):
    sheet = next(item for item in metadata['sheets'] if item['properties']['title'] == 'Траты')
    props = sheet['properties']
    rows = props['gridProperties']['rowCount']
    rules = [[None, None] for _ in range(rows - 1)]
    for block in sheet.get('data', []):
        for i, row in enumerate(block.get('rowData', [])):
            r = block.get('startRow', 0) + i
            for j, cell in enumerate(row.get('values', [])):
                c = block.get('startColumn', 0) + j
                if r >= 1 and r < rows and c in (1, 2):
                    rules[r - 1][c - 1] = cell.get('dataValidation')
    tables = deepcopy(sheet.get('tables', []))
    for table in tables:
        for column in table.get('columnProperties', []):
            if column.get('columnType') == 'COLUMN_TYPE_UNSPECIFIED': column.pop('columnType')
    return {'sheet_id': props['sheetId'], 'row_count': rows, 'rules': rules, 'tables': tables}


def read_validations(target):
    metadata = target.fetch_sheet_metadata(params={
        'ranges': "'Траты'!B:C", 'includeGridData': True,
        'fields': 'sheets(properties(sheetId,title,gridProperties),tables,data(startRow,startColumn,rowData(values(dataValidation))))'})
    return _validation_manifest(metadata)


def validation_steps(target, output):
    before = read_validations(target)
    meta = WorkbookMetadata.from_rows(sheet_rows(output.sheets[METADATA_SHEET]))
    taxonomy = parse_registry(meta, {name: sheet_rows(output.sheets[name]) for name in REGISTRY_HEADERS})
    requests = ledger_dropdown_table_updates({'sheets': [{'properties': {'title': 'Траты'}, 'tables': before['tables']}]})
    requests += dropdown_requests(before['sheet_id'], before['row_count'], sheet_rows(output.sheets['Траты']), taxonomy)
    after = deepcopy(before)
    for request in requests:
        if 'updateTable' in request:
            change = request['updateTable']['table']
            table = next(t for t in after['tables'] if t['tableId'] == change['tableId'])
            table['columnProperties'] = deepcopy(change['columnProperties'])
            for col in table['columnProperties']:
                if col.get('columnType') == 'COLUMN_TYPE_UNSPECIFIED': col.pop('columnType')
        elif 'setDataValidation' in request:
            entry = request['setDataValidation']; area = entry['range']
            for row in range(area['startRowIndex'], area['endRowIndex']):
                after['rules'][row - 1][area['startColumnIndex'] - 1] = entry.get('rule')
    # A validation step per cell-group permits mixed before/after readback after
    # a lost response. Google batchUpdate applies each complete batch atomically.
    inverse = []
    for table in before['tables']:
        inverse.append({'updateTable': {'table': {'tableId': table['tableId'], 'columnProperties': table.get('columnProperties', [])}, 'fields': 'columnProperties'}})
    for col in (1, 2):
        segments = []
        for row, rules in enumerate(before['rules'], 1):
            rule = rules[col - 1]
            if segments and segments[-1][2] == rule: segments[-1][1] = row + 1
            else: segments.append([row, row + 1, rule])
        for start, end, rule in segments:
            entry = {'range': {'sheetId': before['sheet_id'], 'startRowIndex': start, 'endRowIndex': end, 'startColumnIndex': col, 'endColumnIndex': col+1}}
            if rule is not None: entry['rule'] = rule
            inverse.append({'setDataValidation': entry})
    return {'id': 'dropdowns', 'kind': 'validations', 'before': before, 'after': after,
            'forward': requests, 'inverse': inverse, 'phase': 'prepared'}


def read_formats(target, area):
    # A1-free ranges are accepted as sheetId GridRange only in batchUpdate;
    # fetch metadata uses an explicit A1 rectangle on the known analytics sheet.
    from finance_bot.category_migration import cell_address
    a1 = "'Аналитика'!" + cell_address(area['startRowIndex']+1,area['startColumnIndex']+1) + ':' + cell_address(area['endRowIndex'],area['endColumnIndex'])
    metadata = target.fetch_sheet_metadata(params={'ranges': a1, 'includeGridData': True,
        'fields': 'sheets(properties(title),data(startRow,startColumn,rowData(values(userEnteredFormat))))'})
    h, w = area['endRowIndex']-area['startRowIndex'], area['endColumnIndex']-area['startColumnIndex']
    values = [[{} for _ in range(w)] for _ in range(h)]
    sheet = next(s for s in metadata['sheets'] if s['properties']['title']=='Аналитика')
    for block in sheet.get('data', []):
        for i,row in enumerate(block.get('rowData', [])):
            r=block.get('startRow',0)+i-area['startRowIndex']
            for j,cell in enumerate(row.get('values', [])):
                c=block.get('startColumn',0)+j-area['startColumnIndex']
                if 0<=r<h and 0<=c<w:values[r][c]=cell.get('userEnteredFormat',{})
    return values


def presentation_step(target, plan):
    formats = []
    for request in plan['requests']:
        if 'copyPaste' in request:
            copy = request['copyPaste']; area = copy['destination']
            before = read_formats(target, area); source = read_formats(target, copy['source'])
            after = [deepcopy(source[i % len(source)]) for i in range(len(before))]
            formats.append({'area': area, 'before': before, 'after': after})
    inverse = []
    for chart in plan['before']['charts']:
        changed = next(c for c in plan['after']['charts'] if c['chartId']==chart['chartId'])
        if changed != chart:inverse.append({'updateChartSpec': {'chartId':chart['chartId'],'spec':chart['spec']}})
    for item in formats:
        inverse.append({'updateCells': {'range': item['area'], 'rows': [
            {'values': [{'userEnteredFormat': value} for value in row]} for row in item['before']], 'fields': 'userEnteredFormat'}})
    return {'id': 'presentation', 'kind':'presentation', 'before':plan['before'], 'after':plan['after'],
            'formats':formats, 'forward':plan['requests'], 'inverse':inverse, 'phase':'prepared'}


def view_state(target, step):
    if step['kind']=='validations':return read_validations(target)
    return {'charts': manifest(target), 'formats': [read_formats(target,item['area']) for item in step['formats']]}


def view_expected(step, side):
    if step['kind']=='validations':return step[side]
    return {'charts':step[side], 'formats':[item[side] for item in step['formats']]}


def capacity_steps(target, steps):
    """Grow managed sheets before reading/writing ranges beyond their grid."""
    metadata=target.fetch_sheet_metadata(params={'fields':'sheets(properties(sheetId,title,gridProperties))'})
    sheets={s['properties']['title']:s['properties'] for s in metadata['sheets']}
    result=[]
    for step in steps:
        props=sheets[step['sheet']];grid=props['gridProperties']
        before={'rows':grid['rowCount'],'columns':grid['columnCount']}
        after={'rows':max(before['rows'],max(e['key'][0] for e in step['edits'])),
               'columns':max(before['columns'],max(e['key'][1] for e in step['edits']))}
        if after!=before:
            result.append({'id':'capacity:'+step['sheet'],'kind':'capacity','sheet':step['sheet'],
                           'sheet_id':props['sheetId'],'before':before,'after':after,'phase':'prepared'})
    return result


def read_capacity(target, step):
    metadata=target.fetch_sheet_metadata(params={'fields':'sheets(properties(sheetId,title,gridProperties))'})
    props=next(s['properties'] for s in metadata['sheets'] if s['properties']['sheetId']==step['sheet_id'])
    grid=props['gridProperties'];return {'rows':grid['rowCount'],'columns':grid['columnCount']}


def capacity_matches(actual, expected):
    # Extra blank rows must never be removed during compensation: the user may
    # already have added unrelated data there. Only restore the request's cells.
    return all(actual[key]>=expected[key] for key in ('rows','columns'))
