"""Pure cell plans, conflict checks and compensations for category requests."""
from copy import deepcopy

from finance_bot.category_migration import cell_address
from finance_bot.category_projections import sheet_rows, taxonomy_view
from finance_bot.category_schema import METADATA_SHEET, REGISTRY_HEADERS, WorkbookMetadata, parse_registry
from finance_bot.migration_apply import MigrationError
from finance_bot.native_migration import native_formula
from finance_bot.taxonomy import normalized_name

JOURNAL_VERSION = 2


class CategoryConflict(MigrationError):
    def __init__(self, stage, sheet='', key=None, code='conflict'):
        self.stage, self.sheet, self.key, self.code = stage, sheet, key, code
        address = ('!' + cell_address(*key)) if key else ''
        super().__init__(f'Этап: {stage}. Код: {code}. Область: {sheet}{address}. '
                         'Не повторяйте конфликтующий шаг; доступен проверяемый откат или восстановление оператором.')


def cell_state(sheet, key, target):
    if key in sheet.formulas:
        return {'formula': native_formula(sheet.formulas[key], target)}
    return {'value': sheet.cells.get(key, '') if sheet.cells.get(key) is not None else ''}


def cell_steps(source, output, target):
    steps = []
    for name, sheet in output.sheets.items():
        if name == METADATA_SHEET:
            continue
        original = source.sheets.get(name)
        if original is None:
            raise CategoryConflict('plan', name, code='new_sheet_unsupported')
        edits = []
        for key in sorted(set(original.cells) | set(sheet.cells) | set(original.formulas) | set(sheet.formulas)):
            before, after = cell_state(original, key, target), cell_state(sheet, key, target)
            if before != after:
                # Mutations may change labels B/C, never money/date/user notes.
                if name == 'Траты' and key[0] > 1 and key[1] not in {2, 3}:
                    raise CategoryConflict('plan', name, key, 'financial_write_forbidden')
                edits.append({'key': list(key), 'before': before, 'after': after})
        if edits:
            steps.append({'id': name, 'kind': 'cells', 'sheet': name, 'edits': edits, 'phase': 'prepared'})
    return steps


def check_edits(book, steps, target, stage):
    for step in steps:
        if step['kind'] != 'cells':
            continue
        if step['sheet'] not in book.sheets:
            raise CategoryConflict(stage, step['sheet'], code='missing_sheet')
        sheet = book.sheets[step['sheet']]
        for edit in step['edits']:
            key = tuple(edit['key'])
            if cell_state(sheet, key, target) not in (edit['before'], edit['after']):
                raise CategoryConflict(stage, step['sheet'], key)


def financial_invariants(source, actual):
    """Protect input fields and financial/identity cells, including removed rows."""
    for name, original in source.sheets.items():
        ledger = name == 'Траты'
        archive = name == 'Архив' or name.startswith('Архив (')
        if not ledger and not archive:
            continue
        observed = actual.sheets.get(name)
        if observed is None:
            raise CategoryConflict('verify', name, code='missing_sheet')
        keys = set(original.cells) | set(observed.cells)
        for key in keys:
            before, after = original.cells.get(key, ''), observed.cells.get(key, '')
            protected = (ledger and key[0] > 1 and key[1] not in {2, 3}) or (
                archive and (isinstance(before, (int, float)) or isinstance(after, (int, float))))
            if protected and before != after:
                raise CategoryConflict('verify', name, key, 'financial_conflict')
    for key in ((1, 4), (2, 4)):
        if source.sheets['Аналитика'].cell(*key) != actual.sheets['Аналитика'].cell(*key):
            raise CategoryConflict('verify', 'Аналитика', key, 'period_conflict')


def require_business(book, spreadsheet_id):
    meta = WorkbookMetadata.from_rows(sheet_rows(book.sheets[METADATA_SHEET]))
    if meta.spreadsheet_id != spreadsheet_id:
        raise CategoryConflict('verify', METADATA_SHEET, code='scope')
    registry = parse_registry(meta, {name: sheet_rows(book.sheets[name]) for name in REGISTRY_HEADERS})
    actual_view = sheet_rows(book.sheets['Категории'])
    # Empty cleared cells are padding, not extra category rows.
    def trim(rows):
        rows = [list(row) for row in rows]
        while rows and not any(value not in ('', None) for value in rows[-1]): rows.pop()
        width = max((max((i + 1 for i, v in enumerate(row) if v not in ('', None)), default=0) for row in rows), default=0)
        return [(row + [''] * width)[:width] for row in rows]
    if trim(actual_view) != trim(sheet_rows(taxonomy_view(registry))):
        raise CategoryConflict('verify', 'Категории', code='view_mismatch')
    ledger = book.sheets['Траты']
    for row in range(2, ledger.last_row + 1):
        if ledger.cell(row, 10) == 'valid':
            parent, child = str(ledger.cell(row, 7)), str(ledger.cell(row, 8))
            registry.validate_pair(parent, child, new_operation=False)
            if (normalized_name(ledger.cell(row,2)) != normalized_name(registry.category(parent).name)
                    or normalized_name(ledger.cell(row,3)) != normalized_name(registry.subcategory(child).name)):
                raise CategoryConflict('verify','Траты',(row,2),'label_mismatch')
    return registry


def rollback_projection(source, output, current, steps, target):
    """Preflight *all* edits and dependencies before the first compensation."""
    check_edits(current, steps, target, 'rollback_preflight')
    created = set()
    for name in REGISTRY_HEADERS:
        old = {row[0] for row in sheet_rows(source.sheets[name])[1:] if row and row[0]}
        new = {row[0] for row in sheet_rows(output.sheets[name])[1:] if row and row[0]}
        if name != '_financebot_category_history': created |= new - old
    planned = {(step['sheet'], tuple(edit['key'])) for step in steps if step['kind'] == 'cells' for edit in step['edits']}
    for name, sheet in current.sheets.items():
        for key, value in sheet.cells.items():
            if isinstance(value, str) and value in created and (name, key) not in planned:
                raise CategoryConflict('rollback_preflight', name, key, 'new_dependency')
    projected = deepcopy(current)
    for step in reversed(steps):
        if step['kind'] != 'cells': continue
        sheet = projected.sheets[step['sheet']]
        for edit in step['edits']:
            key, value = tuple(edit['key']), edit['before']
            sheet.cells.pop(key, None); sheet.formulas.pop(key, None)
            if 'formula' in value: sheet.formulas[key] = value['formula']
            elif value['value'] not in ('', None): sheet.cells[key] = value['value']
    require_business(projected, target.id)
    return projected
