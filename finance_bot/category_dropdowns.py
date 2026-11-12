"""Generate dependent native validation without changing ledger values."""
from finance_bot.category_projections import sheet_rows
from finance_bot.taxonomy import normalized_name
from finance_bot.category_schema import REGISTRY_HEADERS, METADATA_SHEET, WorkbookMetadata, parse_registry


def dropdown_requests(sheet_id, max_rows, rows, taxonomy):
    parents = list(dict.fromkeys(item.name for item in taxonomy.categories if item.active))
    requests = []
    for column in (1, 2):
        segments = []
        for index in range(1, max_rows):
            row = rows[index] if index < len(rows) else []
            get = lambda col: str(row[col]) if col < len(row) and row[col] is not None else ""
            if column == 1:
                values = list(parents)
            else:
                values = [child.name for child in taxonomy.subcategories
                          if child.active and taxonomy.category(child.current_category_id).active
                          and normalized_name(taxonomy.category(child.current_category_id).name) == normalized_name(get(1))]
            if get(6) and get(7) and get(column):
                values.append(get(column))
            values = tuple(dict.fromkeys(values))
            if segments and segments[-1][2] == values:
                segments[-1][1] = index + 1
            else:
                segments.append([index, index + 1, values])
        for start, end, values in segments:
            request = {"range": {"sheetId": sheet_id, "startRowIndex": start, "endRowIndex": end,
                                  "startColumnIndex": column, "endColumnIndex": column + 1}}
            if values:
                request["rule"] = {"condition": {"type": "ONE_OF_LIST", "values": [
                    {"userEnteredValue": name} for name in values]}, "strict": True, "showCustomUi": True}
            requests.append({"setDataValidation": request})
    return requests


def apply_category_dropdowns(target, output):
    from finance_bot.native_migration import ledger_dropdown_table_updates
    metadata = target.fetch_sheet_metadata(params={"fields": "sheets(properties(sheetId,title,gridProperties),tables)"})
    ledger = next(item for item in metadata["sheets"] if item["properties"]["title"] == "Траты")
    worksheet = None
    props = ledger["properties"]
    if "sheetId" not in props or "gridProperties" not in props:
        worksheet = target.worksheet("Траты")
    sheet_id = props["sheetId"] if "sheetId" in props else worksheet.id
    max_rows = props["gridProperties"]["rowCount"] if "gridProperties" in props else worksheet.row_count
    meta = WorkbookMetadata.from_rows(sheet_rows(output.sheets[METADATA_SHEET]))
    taxonomy = parse_registry(meta, {name: sheet_rows(output.sheets[name]) for name in REGISTRY_HEADERS})
    # Changing typed table columns and assigning cell validation in one batch is
    # ordered by Sheets; keep all unrelated table properties and data intact.
    requests = ledger_dropdown_table_updates(metadata) + dropdown_requests(
        sheet_id, max_rows, sheet_rows(output.sheets["Траты"]), taxonomy)
    if requests:
        target.batch_update({"requests": requests})
