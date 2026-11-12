"""Confirmed, minimal edits to an existing native dashboard (never bootstrap)."""
from copy import deepcopy
import hashlib
import json

from finance_bot.category_projections import LABEL_SHEET, sheet_rows
from finance_bot.migration_apply import MigrationError


def digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def manifest(target):
    metadata = target.fetch_sheet_metadata(params={
        'ranges': "'Аналитика'", 'includeGridData': True,
        'fields': 'sheets(properties(sheetId,title,gridProperties),charts,data(startRow,startColumn,rowMetadata(pixelSize),columnMetadata(pixelSize)))'})
    sheet = next(item for item in metadata['sheets'] if item['properties']['title'] == 'Аналитика')
    result = deepcopy(sheet)
    result['charts'] = sorted(result.get('charts', []), key=lambda item: item['chartId'])
    return result


def _count(book):
    return sum(row[0] == 'Аналитика' and row[2] == 'category'
               for row in sheet_rows(book.sheets[LABEL_SHEET])[1:])


def _sources(chart, field):
    return chart.get('spec', {}).get('pieChart', {}).get(field, {}).get('sourceRange', {}).get('sources', [])


def prepare_presentation(target, source, output):
    before = manifest(target)
    sheet_id = before['properties']['sheetId']
    count, new_count = _count(source), _count(output)
    def main(chart):
        for field, col in (('domain', 0), ('series', 1)):
            sources = _sources(chart, field)
            if len(sources) != 1:
                return False
            area = sources[0]
            if any(area.get(key, 0) != value for key, value in {
                'sheetId': sheet_id, 'startRowIndex': 8, 'endRowIndex': 8 + count,
                'startColumnIndex': col, 'endColumnIndex': col + 1}.items()):
                return False
        return True
    candidates = [chart for chart in before['charts'] if main(chart)]
    if len(candidates) != 1:
        raise MigrationError('Общая диаграмма отсутствует или неоднозначна. Таблица не изменена.')
    after = deepcopy(before)
    requests = []
    if new_count > count:
        grid = before['properties'].get('gridProperties', {})
        if grid.get('rowCount', 0) < 8 + new_count or grid.get('columnCount', 0) < 4:
            raise MigrationError('Нет свободного места для общей сводки. Таблица не изменена.')
        rows, cols = {}, {}
        for block in before.get('data', []):
            rows.update({block.get('startRow', 0) + i: item.get('pixelSize', 21)
                         for i, item in enumerate(block.get('rowMetadata', []))})
            cols.update({block.get('startColumn', 0) + i: item.get('pixelSize', 100)
                         for i, item in enumerate(block.get('columnMetadata', []))})
        def offset(sizes, end, default):
            return sum(sizes.get(i, default) for i in range(end))
        top = offset(rows, 8 + count, 21)
        bottom = offset(rows, 8 + new_count, 21)
        right = offset(cols, 4, 100)
        for chart in before['charts']:
            overlay = chart.get('position', {}).get('overlayPosition')
            if not overlay:
                raise MigrationError('Неизвестное размещение диаграммы. Таблица не изменена.')
            anchor = overlay.get('anchorCell', {})
            x = offset(cols, anchor.get('columnIndex', 0), 100) + overlay.get('offsetXPixels', 0)
            y = offset(rows, anchor.get('rowIndex', 0), 21) + overlay.get('offsetYPixels', 0)
            if x < right and x + overlay.get('widthPixels', 600) > 0 and y < bottom and y + overlay.get('heightPixels', 371) > top:
                raise MigrationError('Новая строка пересекается с диаграммой. Таблица не изменена.')
        chart = next(item for item in after['charts'] if item['chartId'] == candidates[0]['chartId'])
        for field in ('domain', 'series'):
            _sources(chart, field)[0]['endRowIndex'] = 8 + new_count
        requests.append({'copyPaste': {
            'source': {'sheetId': sheet_id, 'startRowIndex': 7 + count, 'endRowIndex': 8 + count, 'startColumnIndex': 0, 'endColumnIndex': 4},
            'destination': {'sheetId': sheet_id, 'startRowIndex': 8 + count, 'endRowIndex': 8 + new_count, 'startColumnIndex': 0, 'endColumnIndex': 4},
            'pasteType': 'PASTE_FORMAT'}})
        requests.append({'updateChartSpec': {'chartId': chart['chartId'], 'spec': chart['spec']}})
    return {'version': 1, 'before': before, 'after': after, 'requests': requests}


def validate_presentation(target, plan, expected_digest, *, resume=False):
    if not plan or plan.get('version') != 1 or digest(plan) != expected_digest:
        raise MigrationError('Старый или повреждённый план диаграмм. Нужен новый предпросмотр; незавершённый запрос требует восстановления оператором.')
    actual = manifest(target)
    if actual != plan['before'] and not (resume and actual == plan['after']):
        raise MigrationError('Диаграммы или размещение изменились после предпросмотра. Таблица не изменена.')


def apply_presentation(target, plan, expected_digest):
    validate_presentation(target, plan, expected_digest, resume=True)
    if manifest(target) != plan['after'] and plan['requests']:
        target.batch_update({'requests': plan['requests']})
    if manifest(target) != plan['after']:
        raise MigrationError('Не удалось подтвердить сохранение диаграмм. Продолжите сохранённый запрос.')
