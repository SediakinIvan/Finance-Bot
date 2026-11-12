"""Private confirmed plans for the Telegram category editor."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

from finance_bot.category_changes import CategoryChange, prepare_change
from finance_bot.category_migration import read_google_spreadsheet
from finance_bot.category_projections import sheet_rows
from finance_bot.category_schema import METADATA_SHEET, REGISTRY_HEADERS, WorkbookMetadata, parse_registry
from finance_bot.migration_apply import MigrationError, _private_json, fingerprint, payload_snapshot, snapshot_payload
from finance_bot.sheets import SheetsError
from finance_bot.taxonomy import Period, TaxonomyError, normalized_name


class CategoryManagementError(SheetsError):
    pass


class StaleCategoryPlan(CategoryManagementError):
    pass


def validate_name(value: str) -> str:
    name = " ".join(value.split())
    if not 1 <= len(name) <= 64:
        raise CategoryManagementError("Название должно содержать от 1 до 64 символов.")
    return name


def validate_children(text: str) -> tuple[str, ...]:
    lines = text.splitlines()
    if not 1 <= len(lines) <= 20:
        raise CategoryManagementError("Введите от 1 до 20 подкатегорий, каждую с новой строки.")
    names = tuple(validate_name(line) for line in lines)
    if len({normalized_name(name) for name in names}) != len(names):
        raise CategoryManagementError("В списке есть повторяющиеся подкатегории.")
    return names


@dataclass(frozen=True)
class CategoryPreview:
    request_id: str
    text: str
    status: str = "preview"


class CategoryManager:
    def __init__(self, store, directory: Path, owner_id: int):
        self.store, self.owner_id = store, owner_id
        if type(owner_id) is not int or owner_id <= 0:
            raise CategoryManagementError("Не удалось определить владельца изменения.")
        scope = hashlib.sha256(store.spreadsheet_id.encode()).hexdigest()
        self.base = Path(directory).expanduser()
        self.root = self.base / scope / str(owner_id)

    def _directory(self, request_id: str) -> Path:
        try:
            if str(UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise CategoryManagementError("Неверный запрос изменения.") from exc
        path = self.root / request_id
        try:
            # The configured base may be a bind mount. Only our scope directories
            # are chmodded; no credentials or neighbouring directories are touched.
            for item in (self.base, self.root.parent, self.root, path):
                if item.is_symlink():
                    raise OSError("Symlink journal directory")
                item.mkdir(mode=0o700, parents=True, exist_ok=True)
                item.chmod(0o700)
        except OSError as exc:
            raise CategoryManagementError("Журнал изменений недоступен. Таблица не изменена.") from exc
        return path

    def _load(self, request_id: str) -> tuple[Path, dict]:
        path = self._directory(request_id) / "request.json"
        try:
            if path.is_symlink():
                raise ValueError
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved["owner_id"] != self.owner_id or saved["spreadsheet_id"] != self.store.spreadsheet_id:
                raise ValueError
            if saved["request_id"] != request_id:
                raise ValueError
            return path, saved
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CategoryManagementError("Журнал запроса отсутствует или повреждён. Нужно восстановление оператором.") from exc

    def preview(self, **params) -> CategoryPreview:
        request_id = str(uuid4())
        path = self._directory(request_id) / "request.json"
        # Prove the local journal is writable before any application can start.
        _private_json(path, {"owner_id": self.owner_id, "spreadsheet_id": self.store.spreadsheet_id,
                             "request_id": request_id, "status": "preparing"})
        source = read_google_spreadsheet(self.store._get_spreadsheet())
        meta = WorkbookMetadata.from_rows(sheet_rows(source.sheets[METADATA_SHEET]))
        meta.require_ready(self.store.spreadsheet_id)
        registry = parse_registry(meta, {name: sheet_rows(source.sheets[name]) for name in REGISTRY_HEADERS})
        action = params.get("action")
        if action not in {"create", "rename", "move", "deactivate", "reactivate"}:
            raise CategoryManagementError("Неизвестное действие.")
        if action in {"create", "rename"}:
            params["name"] = validate_name(params.get("name", ""))
        if action == "create" and not params.get("parent_id"):
            params["subcategories"] = validate_children("\n".join(params.get("subcategories", ())))
        period = Period(int(float(source.sheets["Аналитика"].cell(2, 4))),
                        int(float(source.sheets["Аналитика"].cell(1, 4))))
        if action == "deactivate":
            params["period"] = period
        entity_id = params.get("entity_id", "")
        entity = next((item for item in (*registry.categories, *registry.subcategories)
                       if getattr(item, "category_id", getattr(item, "subcategory_id", "")) == entity_id), None)
        if action in {"rename", "move", "deactivate", "reactivate"} and entity is None:
            raise CategoryManagementError("Эта сущность больше не найдена. Откройте список заново.")
        try:
            change = prepare_change(source, spreadsheet_id=self.store.spreadsheet_id,
                                    expected_revision=meta.taxonomy_revision, request_id=request_id, **params)
        except (TaxonomyError, ValueError) as exc:
            messages = {
                "Entity is already active.": "Эта сущность уже активна. Откройте список заново.",
                "Reactivate the parent category first.": "Сначала восстановите родительскую категорию: вместе с ней вернутся все её текущие подкатегории.",
                "Invalid archive column mapping.": "Повреждён реестр колонок Архива. Таблица не изменена; требуется проверка оператором.",
                "Ambiguous archive column mapping.": "Неоднозначное соответствие ID и колонок Архива. Таблица не изменена.",
                "Conflicting archive label mapping.": "Индекс подписей противоречит реестру колонок Архива. Таблица не изменена.",
                "Ambiguous category name within a transaction type.": "Категория с таким названием уже существует для этого типа операции.",
                "Ambiguous subcategory name within a category.": "В категории уже есть подкатегория с таким названием. Выберите другое имя.",
                "Cannot add a subcategory to an inactive parent.": "Нельзя добавить подкатегорию в неактивную категорию.",
                "Move requires active entities of the same transaction type.": "Для переноса нужны активные категории одного типа операции.",
                "There is no free space for a new category summary row.": "Нет свободной строки для общей сводки. Таблица не изменена.",
                "An indexed archive sheet is missing.": "Не удалось переименовать историю: в индексе есть отсутствующий архивный лист. Нужно восстановить архив или согласовать исправление индекса. Таблица не изменена.",
            }
            raise CategoryManagementError(messages.get(str(exc), "Недопустимое изменение: проверьте справочник и индексированные архивы.")) from exc
        from finance_bot.dashboard_changes import prepare_presentation, digest
        try:
            presentation = prepare_presentation(self.store._get_spreadsheet(), source, change.output)
        except MigrationError as exc:
            raise CategoryManagementError(str(exc)) from exc
        change = replace(change, presentation=presentation, presentation_digest=digest(presentation))
        if action == "create":
            text = "Создать: " + params["name"]
            if params.get("parent_id"):
                text += "\nВ категории: " + registry.category(params["parent_id"]).name
            else:
                text += "\nТип: " + params.get("transaction_type", "Расход")
                text += "\nПодкатегории:\n" + "\n".join(params["subcategories"])
        elif action == "rename":
            text = f"Переименовать: {entity.name} → {params['name']}\nНазвание изменится также в истории и архивах."
        elif action == "reactivate":
            text = f"Восстановить: {entity.name}\nID и история сохранятся."
            if hasattr(entity, "category_id"):
                names = [child.name for child in registry.subcategories if child.current_category_id == entity.category_id]
                text += f"\nВосстановятся все текущие подкатегории ({len(names)}):\n" + "\n".join(names)
                text += "\nВключая ранее отдельно отключённые. Перенесённые в другие категории не затрагиваются."
        elif action == "move":
            text = (f"Перенести: {entity.name}\nИз: {registry.category(entity.current_category_id).name}"
                    f"\nВ: {registry.category(params['parent_id']).name}\nТолько для новых операций; прежние останутся в старой категории.")
        else:
            text = f"Удалить из новых вариантов: {entity.name}\nИстория и архивы сохранятся."
            if hasattr(entity, "category_id"):
                text += f"\nПериод: {period}. После него архивная колонка заполняется нулями."
        if change.missing_archives:
            text += (f"\nВнимание: отсутствующих архивных снимков — {len(change.missing_archives)}. "
                     "Они пропущены и не восстановлены; названия обновятся только в существующих листах.")
        affected = sum(source.sheets.get(name) != sheet for name, sheet in change.output.sheets.items()
                       if name == "Траты" or name == "Архив" or name.startswith("Архив ("))
        text += f"\nЗатронутых листов операций/архивов: {affected}. Суммы сохраняются."
        text += "\nПодтвердить? Во время применения не редактируйте таблицу."
        _private_json(path, {"owner_id": self.owner_id, "spreadsheet_id": self.store.spreadsheet_id,
            "request_id": request_id, "status": "preview", "text": text,
            "source": snapshot_payload(source), "change": {
                "request_id": change.request_id, "expected_revision": change.expected_revision,
                "source_digest": change.source_digest, "target_digest": change.target_digest,
                "output": snapshot_payload(change.output), "presentation": change.presentation,
                "presentation_digest": change.presentation_digest, "missing_archives": list(change.missing_archives)}})
        return CategoryPreview(request_id, text)

    def status(self, request_id: str) -> CategoryPreview:
        _, saved = self._load(request_id)
        text = saved.get("text", "Изменение справочника")
        display_status = saved["status"]
        native = self._directory(request_id) / (request_id + "-native.json")
        if native.exists():
            try:
                state = json.loads(native.read_text())
                from finance_bot.category_changes_native import diagnostic
                if state.get("version") == 2 and state.get("finished") and saved["status"] in {"applying", "rolling_back"}:
                    expected = state.get("rollback_revision") if state.get("phase") == "rolled_back" else saved["change"]["expected_revision"] + 1
                    meta = WorkbookMetadata.from_rows(self.store._rows(METADATA_SHEET))
                    meta.require_ready(self.store.spreadsheet_id, expected_revision=expected)
                    saved["status"] = "rolled_back" if state.get("phase") == "rolled_back" else "complete"
                    _private_json(self._directory(request_id) / "request.json", saved)
                    display_status = saved["status"]
                if state.get("last_error"):
                    text += "\n\n" + diagnostic(state["last_error"], request_id)
                    if state["last_error"].get("code") in {"conflict", "financial_conflict", "period_conflict", "native_view_conflict", "missing_sheet", "new_dependency"} and saved["status"] == "applying":
                        display_status = "conflict"
                if state.get("version") != 2 and not state.get("finished") and saved["status"] in {"applying", "rolling_back"}:
                    text += "\nСтарый журнал: продолжение и откат требуют восстановления оператором."
                    display_status = "operator_required"
            except (ValueError, OSError):
                raise CategoryManagementError("Журнал повреждён. Нужно восстановление оператором.")
        return CategoryPreview(request_id, text, display_status)

    def cancel(self, request_id: str) -> None:
        path, saved = self._load(request_id)
        if saved["status"] == "preview":
            saved["status"] = "cancelled"
            _private_json(path, saved)

    def _plan(self, request_id, saved):
        try:
            fields = saved["change"]
            source = payload_snapshot(saved["source"])
            output = payload_snapshot(fields["output"])
            change = CategoryChange(request_id, self.store.spreadsheet_id, fields["expected_revision"],
                                    fields["source_digest"], fields["target_digest"], output,
                                    fields.get("presentation"), fields.get("presentation_digest", ""),
                                    tuple(fields.get("missing_archives", ())))
            if fingerprint(source) != change.source_digest or fingerprint(output) != change.target_digest:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise CategoryManagementError("Сохранённый план повреждён. Нужно восстановление оператором.") from exc
        return source, change

    def apply(self, request_id: str, *, resume: bool = False) -> CategoryPreview:
        path, saved = self._load(request_id)
        if saved["status"] == "complete":
            return CategoryPreview(request_id, saved["text"], "complete")
        if saved["status"] not in ({"applying"} if resume else {"preview"}):
            raise CategoryManagementError("Этот запрос уже не активен. Откройте «Категории» заново.")
        source, change = self._plan(request_id, saved)
        from finance_bot.dashboard_changes import digest
        if (not change.presentation or change.presentation.get("version") != 1
                or digest(change.presentation) != change.presentation_digest):
            if not resume:
                saved["status"] = "stale"
                _private_json(path, saved)
            raise CategoryManagementError("Старый или повреждённый план диаграмм. Нужен новый предпросмотр; pending требует оператора.")
        # The native service performs the fresh source/presentation preflight.
        # Do not repeat an entire workbook read before that same guarded check.
        saved.pop("rollback_confirmation", None)
        saved["status"] = "applying"; _private_json(path, saved)
        try:
            self.store.apply_category_change(source, change, path.parent, confirmed=True, maintenance_confirmed=True)
        except Exception as exc:
            native = path.parent / (request_id + "-native.json")
            if not native.exists():
                saved["status"] = "stale"; _private_json(path, saved)
                from finance_bot.category_changes_native import diagnostic, safe_failure
                detail = (str(exc) if isinstance(exc, MigrationError)
                          else diagnostic(safe_failure(exc,"preflight"),request_id))
                raise StaleCategoryPlan("Изменение не начато. " + detail) from exc
            try:
                state = json.loads(native.read_text())
                from finance_bot.category_changes_native import diagnostic, safe_failure
                details = diagnostic(state.get("last_error") or safe_failure(exc, "apply"), request_id)
            except (ValueError, OSError):
                details = "Журнал недоступен; требуется оператор."
            saved["last_error"] = details
            _private_json(path, saved)
            raise CategoryManagementError("Изменение не завершено. " + details +
                " Откройте «Категории»: продолжение или проверяемый откат. Не повторяйте конфликтующий шаг.") from exc
        saved["status"] = "complete"; _private_json(path, saved)
        return CategoryPreview(request_id, saved["text"], "complete")

    def pending(self) -> CategoryPreview | None:
        meta = WorkbookMetadata.from_rows(self.store._rows(METADATA_SHEET))
        if meta.spreadsheet_id != self.store.spreadsheet_id:
            raise CategoryManagementError("Таблица требует миграции.")
        if meta.pending_request_id:
            return self.status(meta.pending_request_id)
        meta.require_ready(self.store.spreadsheet_id)
        # A crash after applying or before first pending write can leave the
        # durable request ahead of metadata. Recover only this owner's plans.
        if self.root.exists():
            for path in sorted(self.root.glob("*/request.json")):
                saved = self.status(path.parent.name)
                if saved.status in {"applying", "rolling_back", "conflict", "operator_required"}:
                    return saved
        return None

    def preview_rollback(self, request_id: str) -> CategoryPreview:
        path, saved = self._load(request_id)
        if saved["status"] not in {"applying", "rolling_back"}:
            raise CategoryManagementError("Этот запрос уже завершён или не был начат. Откат недоступен.")
        source, change = self._plan(request_id, saved)
        from finance_bot.category_changes_native import prepare_rollback, diagnostic, safe_failure
        try:
            prepare_rollback(source, change, self.store._get_spreadsheet(), path.parent)
        except Exception as exc:
            raise CategoryManagementError("Откат не начат. " + diagnostic(safe_failure(exc, "rollback_preflight"), request_id) +
                                          " При конфликте требуется восстановление оператором.") from exc
        saved["rollback_confirmation"] = True
        _private_json(path, saved)
        return CategoryPreview(request_id,
            "Откатить незавершённое изменение? Вернутся исходные имена, активность и связи этого запроса. "
            "Нетронутые ручные правки, финансовые суммы и даты сохранятся. Подтвердить откат?", "rollback_preview")

    def rollback(self, request_id: str, *, confirmed=False, resume=False) -> CategoryPreview:
        path, saved = self._load(request_id)
        if saved["status"] == "rolled_back":
            return CategoryPreview(request_id, "Изменение отменено, исходные настройки восстановлены.", "rolled_back")
        if confirmed is not True or saved["status"] not in {"applying", "rolling_back"}:
            raise CategoryManagementError("Требуется отдельное подтверждение отката.")
        if not saved.get("rollback_confirmation") and not (resume and saved["status"] == "rolling_back"):
            raise CategoryManagementError("Откройте предпросмотр отката и подтвердите его.")
        source, change = self._plan(request_id, saved)
        saved["status"] = "rolling_back"
        _private_json(path, saved)
        from finance_bot.category_changes_native import rollback_change_to_google_copy, diagnostic, safe_failure
        try:
            rollback_change_to_google_copy(source, change, self.store._get_spreadsheet(), path.parent, confirmed=True)
        except Exception as exc:
            native = path.parent / (request_id + "-native.json")
            try:
                state = json.loads(native.read_text())
                if state.get("phase") not in {"rolling_back", "rollback_finalizing"}:
                    saved["status"] = "applying"
            except (ValueError, OSError):
                state = {}
            from finance_bot.category_journal import CategoryConflict
            details = diagnostic(safe_failure(exc, "rollback_preflight") if isinstance(exc, CategoryConflict)
                                 else state.get("last_error") or safe_failure(exc, "rollback_preflight"), request_id)
            saved["last_error"] = details; _private_json(path, saved)
            raise CategoryManagementError("Откат не завершён. " + details + " Откройте «Категории» для проверки статуса.") from exc
        saved["status"] = "rolled_back";saved.pop("rollback_confirmation", None)
        _private_json(path, saved)
        return CategoryPreview(request_id, "Изменение отменено, исходные настройки восстановлены.", "rolled_back")

    def recovery_ready(self) -> bool:
        pending = self.pending()
        if pending is None:return False
        path, saved = self._load(pending.request_id)
        source, change = self._plan(pending.request_id, saved)
        native = path.parent / (pending.request_id + "-native.json")
        if not native.exists():
            current = read_google_spreadsheet(self.store._get_spreadsheet())
            if fingerprint(current) == change.source_digest:return True
            raise CategoryManagementError("Журнал pending отсутствует; требуется оператор.")
        state = json.loads(native.read_text())
        from finance_bot.category_changes_native import _signature, _require_owner
        if (state.get("version") != 2 or state.get("request_id") != change.request_id
                or state.get("spreadsheet_id") != change.spreadsheet_id or state.get("plan_signature") != _signature(state)
                or state.get("source_digest") != change.source_digest or state.get("target_digest") != change.target_digest):
            raise CategoryManagementError("Старый или повреждённый pending-журнал: требуется оператор перед выпуском.")
        meta = WorkbookMetadata.from_rows(self.store._rows(METADATA_SHEET))
        _require_owner(meta, change, allow_ready=True, rollback_revision=state.get("rollback_revision"))
        return True
