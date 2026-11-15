"""Native dashboard presentation; calculations and IDs stay in their contracts."""
from finance_bot.category_migration import _coordinate


def report_presentation_requests(sheet_id, labels, category_count, existing_chart_ids=()):
    requests = [{"deleteEmbeddedObject": {"objectId": key}} for key in existing_chart_ids]
    def grid(r, c, end_r, end_c):
        return {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": end_r,
                "startColumnIndex": c, "endColumnIndex": end_c}
    def format_range(area, value):
        requests.append({"repeatCell": {"range": area, "cell": {"userEnteredFormat": value},
            "fields": "userEnteredFormat(" + ",".join(value) + ")"}})
    money = {"type": "NUMBER", "pattern": '#,##0.00" ₽"'}
    format_range(grid(2, 1, 5, 2), {"numberFormat": money, "horizontalAlignment": "RIGHT"})
    format_range(grid(8, 1, 8 + category_count, 2), {"numberFormat": money, "horizontalAlignment": "RIGHT"})
    format_range(grid(8, 2, 8 + category_count, 3), {"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}})
    # D1:D2 remain visible editable controls. Hide only the textual helper values.
    format_range(grid(7, 3, 8 + category_count, 4), {"numberFormat": {"type": "NUMBER", "pattern": ";;;"}})
    for col, width in {0:230, 1:135, 2:145, 3:95, 4:100, 5:85, 6:240, 7:135, 8:24,
                       9:240, 10:135, 11:24, 12:240, 13:135}.items():
        requests.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
            "startIndex": col, "endIndex": col + 1}, "properties": {"pixelSize": width}, "fields": "pixelSize"}})
    def pie(title, first, last, col, anchor_row, anchor_col, width=370):
        if last <= first:
            return
        def source(column):
            return {"sourceRange": {"sources": [grid(first, column, last, column + 1)]}}
        requests.append({"addChart": {"chart": {"spec": {"title": title, "fontName": "Arial",
            "pieChart": {"legendPosition": "RIGHT_LEGEND", "domain": source(col), "series": source(col + 1)}},
            "position": {"overlayPosition": {"anchorCell": {"sheetId": sheet_id,
                "rowIndex": anchor_row, "columnIndex": anchor_col}, "widthPixels": width, "heightPixels": 190}}}}})
    pie("Категории", 8, 8 + category_count, 0, max(28, 11 + category_count), 0, 510)
    headings = [row for row in labels if row[2] == "heading"]
    for entry in headings:
        row, col = _coordinate(entry[1])
        children = [item for item in labels if item[2] == "subcategory" and item[3] == entry[3]]
        if not children:
            continue
        end = max(_coordinate(item[1])[0] for item in children)
        title = entry[5] if len(entry) > 5 else "Подкатегории"
        format_range(grid(row - 1, col - 1, row + 1, col + 1), {
            "textFormat": {"bold": True}, "backgroundColor": {"red": .91, "green": .93, "blue": .95}})
        format_range(grid(row - 1, col, end, col + 1), {"numberFormat": money, "horizontalAlignment": "RIGHT"})
        pie(title, row + 1, end, col - 1, end + 1, col - 1)
    return requests


def preserve_chart_requests(requests, existing_charts):
    """Migrate known legacy chart sources in place; keep unrelated objects."""
    def area(spec):
        sources = spec.get("pieChart", {}).get("domain", {}).get("sourceRange", {}).get("sources", [])
        if len(sources) != 1:
            return None
        grid = sources[0]
        return (grid.get("sheetId"), grid.get("startColumnIndex", 0), grid.get("startRowIndex", 0))
    result, used = [], set()
    for request in requests:
        if "addChart" not in request:
            result.append(request)
            continue
        chart = request["addChart"]["chart"]
        key = area(chart["spec"])
        candidates = [x for x in existing_charts if area(x["spec"]) in (
            key, (key[0], key[1], key[2] - 2) if key and key[1] else None)]
        if len(candidates) > 1:
            raise ValueError("Ambiguous legacy chart source; cannot migrate dashboard")
        if not candidates:
            result.append(request)
            continue
        identifier = candidates[0]["chartId"]
        if identifier in used:
            raise ValueError("Legacy chart matches multiple report blocks")
        used.add(identifier)
        result.extend([
            {"updateChartSpec": {"chartId": identifier, "spec": chart["spec"]}},
            {"updateEmbeddedObjectPosition": {"objectId": identifier,
                "newPosition": chart["position"], "fields": "*"}},
        ])
    return result


def apply_report_presentation(target, summary, label_rows, title="Аналитика", *, preserve_existing=False):
    metadata = target.fetch_sheet_metadata(params={"fields": "sheets(properties(sheetId,title),charts)"})
    properties = next(item for item in metadata["sheets"] if item["properties"]["title"] == title)
    sheet_id = properties["properties"]["sheetId"] if "sheetId" in properties["properties"] else target.worksheet(title).id
    labels = [list(row) for row in label_rows[1:] if row[0] == title]
    for row in labels:
        if row[2] == "heading":
            value = summary.cell(*_coordinate(row[1]))
            row.append(value.removeprefix("Подкатегории: ") + ": подкатегории")
    category_count = sum(row[2] == "category" for row in labels)
    charts = properties.get("charts", [])
    requests = report_presentation_requests(sheet_id, labels, category_count,
        [] if preserve_existing else [item["chartId"] for item in charts])
    if preserve_existing:
        requests = preserve_chart_requests(requests, charts)
    if title == "Аналитика":
        for item in metadata["sheets"]:
            name = item["properties"]["title"]
            key = item["properties"]["sheetId"] if "sheetId" in item["properties"] else target.worksheet(name).id
            if name.startswith("_financebot_"):
                requests.append({"updateSheetProperties": {"properties": {"sheetId": key, "hidden": True}, "fields": "hidden"}})
            if name == "Траты":
                requests.append({"updateDimensionProperties": {"range": {"sheetId": key, "dimension": "COLUMNS", "startIndex": 6, "endIndex": 11},
                    "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
    target.batch_update({"requests": requests})
