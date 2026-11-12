"""Sheets v2 runtime. Legacy books are rejected until migration is verified."""

from __future__ import annotations

import logging
from functools import wraps
from threading import RLock
from typing import Any, Sequence
from uuid import UUID

from finance_bot.analytics import (
    AnalyticsValue, CategoryAnalytics, MonthlyAnalytics, _amount, parse_monthly_analytics,
)
from finance_bot.category_projections import DETAIL_HEADERS, DETAIL_SHEET, SNAPSHOT_HEADERS, SNAPSHOT_SHEET, sheet_rows, taxonomy_view
from finance_bot.category_schema import RegistryReader, SchemaError, TRANSACTION_HEADERS, read_table, METADATA_SHEET, WorkbookMetadata
from finance_bot.ledger import LedgerError, RowIdentity, reconcile_rows, require_valid, resolve_new_pair, sheet_date
from finance_bot.sheets import CategoryOption, GoogleSheetsStore, SheetsError, StaleTransactionError, TransactionRecord, TransactionNotWrittenError
from finance_bot.taxonomy import Taxonomy, TaxonomyError


class TaxonomyChangedError(SheetsError):
    pass


def _guard(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            appending = method.__name__ == "append_transaction"
            if appending: self._financial_write_started = False
            try:
                return method(self, *args, **kwargs)
            except TaxonomyChangedError:
                raise
            except Exception as exc:
                from finance_bot.category_changes_native import safe_failure
                info = safe_failure(exc, method.__name__)
                if isinstance(exc, SheetsError):
                    message = str(exc)
                elif isinstance(exc, (SchemaError, TaxonomyError, LedgerError)):
                    message = "Таблица требует завершения миграции или исправления строк."
                else:
                    logging.getLogger(__name__).warning("Sheets operation=%s code=%s http=%s type=%s",
                        method.__name__, info["code"], info.get("http_status"), info.get("exception_type"))
                    message = ("Google Sheets ограничил частоту запросов (HTTP 429). Подождите минуту."
                               if info.get("http_status") == 429 else
                               "Ошибка Google Sheets. Проверьте результат в таблице перед повтором.")
                rejected = info.get("http_status") in {400, 401, 403, 404, 429}
                if appending and (not self._financial_write_started or rejected):
                    raise TransactionNotWrittenError("Финансовая запись не отправлена или отклонена Google. " + message) from exc
                if isinstance(exc, SheetsError): raise
                raise SheetsError(message) from exc
    return wrapped



class V2GoogleSheetsStore(GoogleSheetsStore):
    schema_version = 2

    def __init__(self, *args, category_change_state_dir=None, **kwargs):
        super().__init__(*args, **kwargs)
        from pathlib import Path
        self.category_change_state_dir = Path(category_change_state_dir or "secrets/category-changes")
        self._lock = RLock()

    def _category_manager(self, owner_id):
        from finance_bot.category_management import CategoryManager
        return CategoryManager(self, self.category_change_state_dir, owner_id)

    @_guard
    def category_catalog(self):
        return self._registry()

    @_guard
    def preview_category_change(self, owner_id, **params):
        return self._category_manager(owner_id).preview(**params)

    @_guard
    def category_change_status(self, owner_id):
        return self._category_manager(owner_id).pending()

    @_guard
    def confirm_category_change(self, owner_id, request_id, *, resume=False):
        return self._category_manager(owner_id).apply(request_id, resume=resume)

    @_guard
    def cancel_category_change(self, owner_id, request_id):
        self._category_manager(owner_id).cancel(request_id)

    @_guard
    def preview_category_rollback(self, owner_id, request_id):
        return self._category_manager(owner_id).preview_rollback(request_id)

    @_guard
    def rollback_category_change(self, owner_id, request_id, *, confirmed=False, resume=False):
        return self._category_manager(owner_id).rollback(request_id, confirmed=confirmed, resume=resume)

    @_guard
    def category_recovery_ready(self, owner_id):
        return self._category_manager(owner_id).recovery_ready()

    def _registry(self) -> Taxonomy:
        registry = RegistryReader(self._get_spreadsheet(), self.spreadsheet_id).load()
        view = self._rows(self.categories_sheet)
        while view and not any(str(value).strip() for value in view[-1]):
            view.pop()
        if view != sheet_rows(taxonomy_view(registry)):
            raise SchemaError("The readable category view was edited outside the category service.")
        return registry

    @staticmethod
    def _canonical_rows(rows):
        rows = [list(row) for row in rows]
        while rows and not any(value not in ("", None) for value in rows[-1]):
            rows.pop()
        width = max((len(row) for row in rows), default=0)
        return [row + [""] * (width - len(row)) for row in rows]

    def _rows(self, title: str) -> list[list[Any]]:
        book = self._get_spreadsheet()
        if callable(getattr(type(book), "values_get", None)):
            result = book.values_get(self._range(title, ""), params={"valueRenderOption": "UNFORMATTED_VALUE"})
            return self._canonical_rows(result.get("values", []))
        return self._canonical_rows(book.worksheet(title).get_all_values(value_render_option="UNFORMATTED_VALUE"))

    def _rows_many(self, titles):
        book = self._get_spreadsheet()
        if callable(getattr(type(book), "values_batch_get", None)):
            result = book.values_batch_get([self._range(title, "") for title in titles],
                                           params={"valueRenderOption": "UNFORMATTED_VALUE"})
            values = result.get("valueRanges", [])
            if len(values) != len(titles):
                raise SheetsError("Не удалось полностью прочитать данные для сверки.")
            return [self._canonical_rows(item.get("values", [])) for item in values]
        return [self._rows(title) for title in titles]

    def _require_revision(self, revision):
        meta = WorkbookMetadata.from_rows(self._rows(METADATA_SHEET))
        meta.require_ready(self.spreadsheet_id, expected_revision=revision)

    def _batch(self, data: list[dict]) -> None:
        self._get_spreadsheet().values_batch_update({"valueInputOption": "RAW", "data": data})

    @staticmethod
    def _range(title: str, address: str) -> str:
        return "'" + title.replace("'", "''") + "'!" + address

    def _ledger(self, registry: Taxonomy):
        raw, initial_snapshots = self._rows_many((self.transactions_sheet, SNAPSHOT_SHEET))
        if not raw or tuple(raw[0]) != TRANSACTION_HEADERS:
            raise SchemaError("Ledger layout is incompatible with generated analytics.")
        snapshots = {}
        for item in read_table(initial_snapshots, SNAPSHOT_HEADERS):
            identity = RowIdentity(**{key: str(item[key]) for key in SNAPSHOT_HEADERS})
            if identity.transaction_id in snapshots:
                raise SchemaError("Duplicate saved transaction identity.")
            snapshots[identity.transaction_id] = identity
        reconciled = reconcile_rows(raw, registry, snapshots)
        service = [row.service_values() for row in reconciled]
        snapshot_values = [list(SNAPSHOT_HEADERS)]
        for row in reconciled:
            if row.state == "valid":
                item = row.identity()
            elif row.transaction_id in snapshots:
                item = snapshots[row.transaction_id]
            else:
                continue
            values = [getattr(item, key) for key in SNAPSHOT_HEADERS]
            if values not in snapshot_values:
                snapshot_values.append(values)
        # One batched recheck protects input, identity snapshots and revision.
        latest, meta_rows, saved_rows = self._rows_many((self.transactions_sheet, METADATA_SHEET, SNAPSHOT_SHEET))
        if latest != raw or saved_rows != initial_snapshots:
            raise SheetsError("Таблица изменилась во время сверки; повторите запрос.")
        WorkbookMetadata.from_rows(meta_rows).require_ready(self.spreadsheet_id, expected_revision=registry.revision)
        snapshot_values += [[""] * len(SNAPSHOT_HEADERS)] * max(0, len(saved_rows) - len(snapshot_values))
        data = []
        if snapshot_values != saved_rows:
            identity_sheet = self._get_spreadsheet().worksheet(SNAPSHOT_SHEET)
            if len(snapshot_values) > identity_sheet.row_count:
                identity_sheet.add_rows(len(snapshot_values) - identity_sheet.row_count)
            data.append({"range": self._range(SNAPSHOT_SHEET, f"A1:F{len(snapshot_values)}"), "values": snapshot_values})
        current_service = [(list(row[6:11]) + [""] * 5)[:5] for row in raw[1:]]
        if service and service != current_service:
            data.append({"range": self._range(self.transactions_sheet, f"G2:K{len(service) + 1}"), "values": service})
        if data:
            self._batch(data)
        require_valid(reconciled)
        # Return the state after *our own* reconciliation, not the stale input.
        # Append/delete guards must still detect external edits, not this write.
        synced = [list(row) for row in raw]
        for index, values in enumerate(service, 1):
            synced[index] = (synced[index] + [""] * 11)[:11]
            synced[index][6:11] = values
        return self._canonical_rows(synced), reconciled

    @_guard
    def load_taxonomy(self) -> list[CategoryOption]:
        registry = self._registry()
        options = [CategoryOption(
            registry.category(pair.category_id).transaction_type,
            registry.category(pair.category_id).name,
            registry.subcategory(pair.subcategory_id).name,
            pair.category_id, pair.subcategory_id, registry.revision,
        ) for pair in registry.active_pairs()]
        if not options:
            raise SheetsError("В справочнике нет активных вариантов.")
        return options

    @_guard
    def apply_category_change(self, source, change, directory, *, confirmed: bool, maintenance_confirmed: bool) -> None:
        """Internal service boundary for the confirmed management dialog.

        Shares the same per-user store lock as appends and deletions; no new
        Telegram command is registered by exposing this method.
        """
        from finance_bot.category_changes_native import apply_change_to_google_copy
        if change.spreadsheet_id != self.spreadsheet_id:
            raise SchemaError("Category change belongs to another user's table.")
        apply_change_to_google_copy(source, change, self._get_spreadsheet(), directory,
                                   confirmed=confirmed, maintenance_confirmed=maintenance_confirmed)

    @_guard
    def append_transaction(
        self, row: Sequence[str | int], *, category_id: str,
        subcategory_id: str, expected_revision: int, transaction_id: str,
    ) -> None:
        if len(row) != 6 or str(UUID(transaction_id)) != transaction_id:
            raise ValueError("Invalid transaction contract.")
        registry = self._registry()
        if registry.revision != expected_revision:
            raise TaxonomyChangedError("Справочник изменился. Начните ввод заново и подтвердите актуальную операцию.")
        try:
            registry.validate_pair(category_id, subcategory_id, new_operation=True)
        except TaxonomyError as exc:
            raise TaxonomyChangedError("Выбранная пара больше недоступна; начните ввод заново.") from exc
        if resolve_new_pair(registry, row[4], row[1], row[2]) != (category_id, subcategory_id):
            raise TaxonomyChangedError("Названия выбранной пары изменились; начните ввод заново.")
        raw, entries = self._ledger(registry)
        if any(item.transaction_id == transaction_id for item in entries):
            raise SheetsError("Операция с этим ID уже существует; проверьте таблицу.")
        target = next((item.row_number for item in entries if not item.state), len(raw) + 1)
        # Never reuse a row merely because A is empty while B:F contain data.
        values = [*row, category_id, subcategory_id, transaction_id, "valid", sheet_date(row[0])]
        check = reconcile_rows([list(TRANSACTION_HEADERS), values], registry, {})
        require_valid(check)
        meta_rows, latest, saved = self._rows_many((METADATA_SHEET, self.transactions_sheet, SNAPSHOT_SHEET))
        WorkbookMetadata.from_rows(meta_rows).require_ready(self.spreadsheet_id, expected_revision=expected_revision)
        if latest != raw:
            raise SheetsError("Таблица изменилась перед записью; повторите ввод.")
        worksheet = self._get_spreadsheet().worksheet(self.transactions_sheet)
        if target > worksheet.row_count:
            worksheet.add_rows(target - worksheet.row_count)
        identity_sheet = self._get_spreadsheet().worksheet(SNAPSHOT_SHEET)
        if len(saved) + 1 > identity_sheet.row_count:
            identity_sheet.add_rows(len(saved) + 1 - identity_sheet.row_count)
        identity = check[0].identity()
        self._financial_write_started = True
        self._batch([
            {"range": self._range(self.transactions_sheet, f"A{target}:K{target}"), "values": [values]},
            {"range": self._range(SNAPSHOT_SHEET, f"A{len(saved) + 1}:F{len(saved) + 1}"),
             "values": [[getattr(identity, key) for key in SNAPSHOT_HEADERS]]},
        ])

    @_guard
    def get_recent_transactions(self, limit: int = 10) -> list[TransactionRecord]:
        if limit <= 0:
            raise ValueError("Limit must be positive.")
        _, rows = self._ledger(self._registry())
        return [TransactionRecord(row.row_number, tuple(str(value) for value in row.values), row.transaction_id)
                for row in rows if row.state][-limit:]

    @_guard
    def delete_transaction(self, record: TransactionRecord) -> None:
        registry = self._registry()
        _, rows = self._ledger(registry)
        current = next((row for row in rows if row.row_number == record.row_number), None)
        if (not record.transaction_id or current is None
                or current.transaction_id != record.transaction_id
                or tuple(str(value) for value in current.values) != record.values):
            raise StaleTransactionError("Selected transaction changed; select it again.")
        self._require_revision(registry.revision)
        latest = self._rows(self.transactions_sheet)
        values = latest[record.row_number - 1] if record.row_number <= len(latest) else []
        if tuple(str(value) for value in values[:6]) != record.values or len(values) < 9 or values[8] != record.transaction_id:
            raise StaleTransactionError("Selected transaction changed before deletion.")
        self._get_spreadsheet().worksheet(self.transactions_sheet).delete_rows(record.row_number)

    def _analytics_rows(self, registry: Taxonomy):
        self._ledger(registry)
        rows = self._rows(self.analytics_sheet)
        if not rows or len(rows[0]) < 6 or rows[0][5] != "ready":
            raise SheetsError("Аналитика ещё не готова или содержит ошибочные строки.")
        return rows

    @_guard
    def get_monthly_analytics(self) -> MonthlyAnalytics:
        return parse_monthly_analytics(self._analytics_rows(self._registry()))

    @_guard
    def get_analytics_categories(self) -> list[tuple[str, str]]:
        registry = self._registry()
        summary = parse_monthly_analytics(self._analytics_rows(registry))
        amounts = {item.label: item.amount for item in summary.categories}
        return [(item.category_id, item.name) for item in registry.categories
                if item.transaction_type == "Расход" and (item.active or amounts.get(item.name, 0) != 0)]

    @_guard
    def get_category_analytics(self, category_id: str) -> CategoryAnalytics:
        registry = self._registry()
        parent = registry.category(category_id)
        summary_rows = self._analytics_rows(registry)
        summary = parse_monthly_analytics(summary_rows)
        details = []
        for item in read_table(self._rows(DETAIL_SHEET), DETAIL_HEADERS):
            if item["category_id"] == category_id:
                registry.validate_pair(category_id, str(item["subcategory_id"]), new_operation=False)
                child = registry.subcategory(str(item["subcategory_id"]))
                amount = _amount(item["amount"], field="subcategory")
                if child.active or amount != 0:
                    details.append(AnalyticsValue(child.name, amount))
        amount = next((item.amount for item in summary.categories if item.label == parent.name), None)
        if amount is None or not details:
            raise SheetsError("Категория отсутствует в актуальной аналитике.")
        return CategoryAnalytics(parent.name, summary.month, summary.year, amount, tuple(details))
