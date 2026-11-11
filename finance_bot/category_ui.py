"""Telegram category conversations; domain mutations belong to the adapter."""
from __future__ import annotations

import asyncio
import secrets
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ConversationHandler, MessageHandler

from finance_bot.category_management import CategoryManagementError, validate_name, validate_children
from finance_bot.sheets import SheetsError

CATEGORIES_BUTTON = "Категории"
MENU, TEXT, CONFIRM = 100, 101, 102
PAGE_SIZE = 8
FLOW_KEY = "tax_flow"


def _store(update, context):
    return context.application.bot_data["sheets_stores"][update.effective_user.id]


def _authorized(update, context):
    user, chat = update.effective_user, update.effective_chat
    allowed = context.application.bot_data["settings"].authorized_telegram_user_ids
    query = update.callback_query
    return (user is not None and user.id in allowed and chat is not None and chat.type == "private"
            and (query is None or query.from_user.id == user.id))


def _flow(context):
    return context.user_data[FLOW_KEY]


async def _screen(update, context, text, buttons, state=MENU):
    flow = _flow(context)
    flow["token"] = secrets.token_hex(4)
    token = flow["token"]
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"tax:{token}:{action}")
                                     for label, action in row] for row in buttons])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(text, reply_markup=keyboard)
    return state


def _pages(items, page, verb, list_kind):
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    buttons = [[(label, f"{verb}:{key}")] for key, label in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
    nav = []
    if page:
        nav.append(("←", f"page:{list_kind}:{page - 1}"))
    if page + 1 < pages:
        nav.append(("→", f"page:{list_kind}:{page + 1}"))
    if nav:
        buttons.append(nav)
    return buttons, page, pages


async def _catalog(update, context):
    return await asyncio.to_thread(_store(update, context).category_catalog)


async def _root(update, context, page=0):
    registry = await _catalog(update, context)
    flow = _flow(context)
    inactive = flow.get("inactive", False)
    choices = [(c.category_id, c.name + (" · доход" if c.transaction_type == "Доход" else ""))
               for c in registry.categories if c.active != inactive]
    buttons, page, count = _pages(choices, page, "c", "root")
    buttons += [[("Создать категорию", "new")],
                [("Активные" if inactive else "Неактивные", "filter")], [("Закрыть", "cancel")]]
    flow.update(phase="browse", category_id=None, entity_id=None, root_page=page)
    return await _screen(update, context, f"{'Неактивные' if inactive else 'Активные'} категории · {page + 1}/{count}", buttons)


async def _category(update, context, category_id, page=0):
    registry = await _catalog(update, context)
    parent = registry.category(category_id)
    flow = _flow(context)
    inactive = flow.get("inactive_children", not parent.active)
    children = [c for c in registry.subcategories if c.current_category_id == category_id and c.active != inactive]
    buttons, page, count = _pages([(c.subcategory_id, c.name) for c in children], page, "s", "children")
    if parent.active:
        buttons += [[("Добавить подкатегорию", "add")], [("Переименовать", "rename"), ("Удалить", "delete")]]
    else:
        buttons += [[("Переименовать", "rename")]]
    buttons += [[("Активные подкатегории" if inactive else "Неактивные подкатегории", "childfilter")], [("Назад", "root"), ("Закрыть", "cancel")]]
    flow.update(phase="browse", category_id=category_id, entity_id=category_id, entity_kind="category", child_page=page,
                inactive_children=inactive)
    return await _screen(update, context,
        f"Категория: {parent.name}\nТип: {parent.transaction_type}\n{'Активна' if parent.active else 'Неактивна'}"
        f"\nПодкатегории · {page + 1}/{count}", buttons)


async def _child(update, context, child_id):
    registry = await _catalog(update, context)
    child = registry.subcategory(child_id)
    parent = registry.category(child.current_category_id)
    flow = _flow(context)
    if flow.get("category_id") != parent.category_id:
        raise CategoryManagementError("Подкатегория перенесена. Откройте список заново.")
    flow.update(phase="browse", entity_id=child_id, entity_kind="subcategory")
    buttons = [[("Переименовать", "rename")]]
    if child.active and parent.active:
        buttons += [[("Перенести", "move"), ("Удалить", "delete")]]
    buttons += [[("Назад", "parent"), ("Закрыть", "cancel")]]
    return await _screen(update, context, f"Подкатегория: {child.name}\nКатегория: {parent.name}\n"
                         + ("Активна" if child.active else "Неактивна"), buttons)


async def _targets(update, context, page=0):
    registry = await _catalog(update, context)
    flow = _flow(context)
    child = registry.subcategory(flow["entity_id"])
    parent = registry.category(child.current_category_id)
    if not child.active or not parent.active:
        raise CategoryManagementError("Перенос доступен только для активной подкатегории.")
    choices = [(p.category_id, p.name) for p in registry.categories
               if p.active and p.transaction_type == parent.transaction_type and p.category_id != parent.category_id]
    buttons, page, count = _pages(choices, page, "target", "targets")
    buttons += [[("Назад", "parent"), ("Отмена", "cancel")]]
    flow["phase"] = "target"
    return await _screen(update, context, f"Куда перенести «{child.name}»? · {page + 1}/{count}", buttons)


async def _preview(update, context, **params):
    flow = _flow(context)
    preview = await asyncio.to_thread(_store(update, context).preview_category_change, update.effective_user.id, **params)
    flow.update(phase="preview", request_id=preview.request_id, request_status="preview")
    return await _screen(update, context, preview.text, [[("Подтвердить", "confirm"), ("Отмена", "cancel")]], CONFIRM)


async def _close(update, context):
    flow = context.user_data.get(FLOW_KEY, {})
    if flow.get("applying"):
        return await _screen(update, context, "Изменение применяется. Отмена не является откатом.", [], CONFIRM)
    if flow.get("request_status") == "preview":
        await asyncio.to_thread(_store(update, context).cancel_category_change, update.effective_user.id, flow["request_id"])
    context.user_data.pop(FLOW_KEY, None)
    if update.callback_query:
        await update.callback_query.edit_message_text("Управление категориями закрыто.")
    else:
        await update.effective_message.reply_text("Управление категориями закрыто.")
    return ConversationHandler.END


async def start_categories(update, context):
    if not _authorized(update, context):
        return ConversationHandler.END
    from finance_bot.bot import _forget_flow
    previous = context.user_data.get(FLOW_KEY)
    if previous and previous.get("request_status") == "preview":
        await asyncio.to_thread(_store(update, context).cancel_category_change, update.effective_user.id, previous["request_id"])
    _forget_flow(context)
    context.user_data[FLOW_KEY] = {"owner_id": update.effective_user.id}
    try:
        pending = await asyncio.to_thread(_store(update, context).category_change_status, update.effective_user.id)
        if pending:
            flow = _flow(context)
            flow.update(phase="recover", request_id=pending.request_id, request_status=pending.status)
            return await _screen(update, context, "Незавершённое изменение. Продолжить тот же запрос?\n\n" + pending.text,
                                 [[("Продолжить", "resume")], [("Закрыть", "cancel")]], CONFIRM)
        return await _root(update, context)
    except SheetsError as exc:
        context.user_data.pop(FLOW_KEY, None)
        await update.effective_message.reply_text(str(exc))
        return ConversationHandler.END


async def handle_category_callback(update, context):
    query = update.callback_query
    if not _authorized(update, context):
        if query:
            await query.answer()
        return ConversationHandler.END
    flow = context.user_data.get(FLOW_KEY)
    parts = (query.data or "").split(":", 2)
    if not flow or flow.get("owner_id") != update.effective_user.id or len(parts) != 3 or parts[1] != flow.get("token"):
        await query.answer("Кнопка устарела. Откройте /categories.", show_alert=True)
        return None if flow else ConversationHandler.END
    await query.answer()
    verb, _, value = parts[2].partition(":")
    try:
        if flow.get("applying"):
            return CONFIRM
        if verb == "cancel":
            return await _close(update, context)
        if verb == "restart":
            return await start_categories(update, context)
        if verb in {"confirm", "resume"}:
            if (verb == "confirm" and flow.get("phase") != "preview") or (verb == "resume" and flow.get("phase") != "recover"):
                raise CategoryManagementError("Это подтверждение уже не активно.")
            flow["applying"] = True
            flow["token"] = secrets.token_hex(4)
            await query.edit_message_text("Применяю изменение. Пока не редактируйте таблицу…")
            try:
                result = await asyncio.to_thread(_store(update, context).confirm_category_change,
                    update.effective_user.id, flow["request_id"], resume=(verb == "resume"))
                if result.status != "complete":
                    raise CategoryManagementError("Изменение ещё не завершено. Откройте /categories для проверки статуса.")
            finally:
                flow["applying"] = False
            flow.pop("request_id", None);flow.pop("request_status", None)
            return await _screen(update, context, "Изменение выполнено.", [[("К категориям", "root"), ("Закрыть", "cancel")]])
        if verb == "root":
            return await _root(update, context, flow.get("root_page", 0))
        if verb == "filter":
            flow["inactive"] = not flow.get("inactive", False)
            return await _root(update, context)
        if verb == "c":
            flow.pop("inactive_children", None)
            return await _category(update, context, value)
        if verb == "s":
            return await _child(update, context, value)
        if verb == "parent":
            return await _category(update, context, flow["category_id"], flow.get("child_page", 0))
        if verb == "childfilter":
            flow["inactive_children"] = not flow.get("inactive_children", False)
            return await _category(update, context, flow["category_id"])
        if verb == "page":
            kind, _, number = value.partition(":")
            page = max(0, int(number))
            if kind == "root":return await _root(update, context, page)
            if kind == "children":return await _category(update, context, flow["category_id"], page)
            if kind == "targets":return await _targets(update, context, page)
            raise CategoryManagementError("Неверная страница.")
        if verb == "new":
            flow.update(phase="type", draft={"action": "create"})
            return await _screen(update, context, "Тип новой категории?",
                [[("Расход", "type:expense"), ("Доход", "type:income")], [("Отмена", "cancel")]])
        if verb == "type" and flow.get("phase") == "type" and value in {"expense", "income"}:
            flow["draft"]["transaction_type"] = "Расход" if value == "expense" else "Доход"
            flow["phase"] = "new_name"
            return await _screen(update, context, "Введите название категории (1–64 символа).", [[("Отмена", "cancel")]], TEXT)
        if verb == "add":
            flow.update(phase="add_name", draft={"action": "create", "parent_id": flow["category_id"]})
            return await _screen(update, context, "Введите название новой подкатегории.", [[("Отмена", "cancel")]], TEXT)
        if verb == "rename":
            flow.update(phase="rename", draft={"action": "rename", "entity_id": flow["entity_id"]})
            return await _screen(update, context, "Введите новое название (1–64 символа).", [[("Отмена", "cancel")]], TEXT)
        if verb == "move" and flow.get("entity_kind") == "subcategory":
            return await _targets(update, context)
        if verb == "target" and flow.get("phase") == "target":
            return await _preview(update, context, action="move", entity_id=flow["entity_id"], parent_id=value)
        if verb == "delete":
            return await _preview(update, context, action="deactivate", entity_id=flow["entity_id"])
        raise CategoryManagementError("Кнопка не активна. Откройте /categories.")
    except (SheetsError, ValueError, KeyError) as exc:
        text = str(exc) if isinstance(exc, SheetsError) else "Список изменился. Откройте /categories заново."
        return await _screen(update, context, text, [[("Открыть категории", "restart"), ("Закрыть", "cancel")]])


async def receive_category_text(update, context):
    if not _authorized(update, context):
        return ConversationHandler.END
    flow = context.user_data.get(FLOW_KEY)
    if not flow or flow.get("owner_id") != update.effective_user.id:
        return ConversationHandler.END
    try:
        if flow["phase"] == "new_children":
            flow["draft"]["subcategories"] = validate_children(update.effective_message.text)
            return await _preview(update, context, **flow["draft"])
        name = validate_name(update.effective_message.text)
        if flow["phase"] == "new_name":
            flow["draft"]["name"] = name
            flow["phase"] = "new_children"
            return await _screen(update, context, "Введите подкатегории: от 1 до 20 названий, каждое с новой строки.", [[("Отмена", "cancel")]], TEXT)
        if flow["phase"] in {"add_name", "rename"}:
            return await _preview(update, context, **{**flow["draft"], "name": name})
        return await _root(update, context)
    except SheetsError as exc:
        return await _screen(update, context, str(exc) + "\nИсправьте ввод или отмените.", [[("Отмена", "cancel")]], TEXT)


def conversation_states(text_filter):
    return {MENU: [CallbackQueryHandler(handle_category_callback, pattern=r"^tax:")],
            TEXT: [MessageHandler(text_filter, receive_category_text), CallbackQueryHandler(handle_category_callback, pattern=r"^tax:")],
            CONFIRM: [CallbackQueryHandler(handle_category_callback, pattern=r"^tax:")]}


async def reject_stale_category_callback(update, context):
    if _authorized(update, context) and update.callback_query:
        await update.callback_query.answer("Кнопка устарела. Откройте /categories.", show_alert=True)
    # A stale category button must not end an unrelated transaction dialog.
    return None


async def leave_categories(update, context):
    flow = getattr(context, "user_data", {}).get(FLOW_KEY)
    if not flow:
        return
    if flow.get("request_status") == "preview":
        try:
            await asyncio.to_thread(_store(update, context).cancel_category_change, flow["owner_id"], flow["request_id"])
        except SheetsError:
            pass  # The UI token is invalidated even if its private file is unavailable.
    context.user_data.pop(FLOW_KEY, None)
