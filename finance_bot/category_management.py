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
        if action not in {"create", "rename", "move", "deactivate"}:
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
        if action in {"rename", "move", "deactivate"} and entity is None:
            raise CategoryManagementError("Эта сущность больше не найдена. Откройте список заново.")
        try:
            change = prepare_change(source, spreadsheet_id=self.store.spreadsheet_id,
                                    expected_revision=meta.taxonomy_revision, request_id=request_id, **params)
        except (TaxonomyError, ValueError) as exc:
            messages = {
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
        return CategoryPreview(request_id, saved.get("text", "Изменение справочника"), saved["status"])

    def cancel(self, request_id: str) -> None:
        path, saved = self._load(request_id)
        if saved["status"] == "preview":
            saved["status"] = "cancelled"
            _private_json(path, saved)

    def apply(self, request_id: str, *, resume: bool = False) -> CategoryPreview:
        path, saved = self._load(request_id)
        if saved["status"] == "complete":
            return CategoryPreview(request_id, saved["text"], "complete")
        if saved["status"] not in ({"applying"} if resume else {"preview"}):
            raise CategoryManagementError("Этот запрос уже не активен. Откройте «Категории» заново.")
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
        if not resume:
            current = read_google_spreadsheet(self.store._get_spreadsheet())
            if fingerprint(current) != change.source_digest:
                saved["status"] = "stale"; _private_json(path, saved)
                raise StaleCategoryPlan("Данные изменились. Откройте действие заново и подтвердите новый итог.")
        from finance_bot.dashboard_changes import validate_presentation
        try:
            validate_presentation(self.store._get_spreadsheet(), change.presentation,
                                  change.presentation_digest, resume=resume)
        except MigrationError as exc:
            if not resume:
                saved["status"] = "stale"
                _private_json(path, saved)
            raise CategoryManagementError(str(exc)) from exc
        saved["status"] = "applying"; _private_json(path, saved)
        try:
            self.store.apply_category_change(source, change, path.parent, confirmed=True, maintenance_confirmed=True)
        except Exception as exc:
            native = path.parent / (request_id + "-native.json")
            if not native.exists():
                saved["status"] = "stale"; _private_json(path, saved)
                raise StaleCategoryPlan("Изменение не начато. Откройте новый предпросмотр.") from exc
            raise CategoryManagementError("Изменение не завершено. Откройте «Категории» и продолжите сохранённый запрос.") from exc
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
                if saved.status == "applying":
                    return saved
        return None
