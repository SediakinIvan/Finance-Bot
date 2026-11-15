"""Keep owner-authored cells outside the legacy views during a v2 install."""
from copy import deepcopy

from finance_bot.category_migration import _coordinate, inspect_workbook


def legacy_view_cells(workbook, title):
    sheet = workbook.sheets[title]
    if title == "Категории":
        return {(r, c) for r in range(1, sheet.last_row + 1) for c in range(1, 4)}
    if title != "Аналитика":
        return set()
    # Only the existing control/summary area and identified detail blocks belong
    # to the generated view. Do not treat the entire worksheet as disposable.
    owned = {(r, c) for r in range(1, 9) for c in range(1, 5)}
    labels = [x for x in inspect_workbook(workbook)["report_labels"] if x["sheet"] == title]
    for label in labels:
        r, c = _coordinate(label["cell"])
        if label["kind"] == "category":
            owned.update((r, col) for col in range(c, c + 4))
        elif label["kind"] == "heading":
            children = [_coordinate(x["cell"])[0] for x in labels
                        if x["kind"] == "subcategory" and x["category"] == label["category"]
                        and _coordinate(x["cell"])[1] == c]
            last = max([r + 1, *children])
            owned.update((row, col) for row in range(r, last + 1) for col in (c, c + 1))
    return owned


def preserve_view(workbook, generated):
    """Overlay an explicit view footprint, refusing collisions with custom data."""
    source = workbook.sheets[generated.title]
    owned = legacy_view_cells(workbook, generated.title)
    for coordinate in generated.cells.keys() | generated.formulas.keys():
        if coordinate not in owned and (
            source.cells.get(coordinate, "") not in ("", None)
            or coordinate in source.formulas
        ):
            raise ValueError(f"Generated view overlaps owner data: {generated.title} {coordinate}")
    result = deepcopy(source)
    for coordinate in owned:
        result.cells[coordinate] = ""
        result.formulas.pop(coordinate, None)
    result.cells.update(generated.cells)
    result.formulas.update(generated.formulas)
    return result


def verify_owner_cells(source, output):
    for title in ("Аналитика", "Категории"):
        original, current = source.sheets[title], output.sheets[title]
        owned = legacy_view_cells(source, title)
        for coordinate in original.cells.keys() | original.formulas.keys():
            if coordinate in owned:
                continue
            if coordinate in original.formulas:
                if current.formulas.get(coordinate) != original.formulas[coordinate]:
                    raise ValueError(f"Owner formula changed: {title} {coordinate}")
            elif original.cells.get(coordinate, "") not in ("", None):
                if current.cells.get(coordinate, "") != original.cells[coordinate]:
                    raise ValueError(f"Owner value changed: {title} {coordinate}")
