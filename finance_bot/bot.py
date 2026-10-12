"""Telegram conversation for appending transactions to the PnL ledger."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import IntEnum
from typing import Any, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from finance_bot.analytics import CategoryAnalytics, MonthlyAnalytics
from finance_bot.charts import render_donut_chart
from finance_bot.config import Settings
from finance_bot.sheets import CategoryOption, SheetsError
from finance_bot.version import version_text

logger = logging.getLogger(__name__)
ADD_TRANSACTION_BUTTON = "Добавить транзакцию"
ADD_TODAY_TRANSACTION_BUTTON = "Добавить транзакцию (сегодня)"
ADD_YESTERDAY_TRANSACTION_BUTTON = "Добавить транзакцию (вчера)"
RECENT_TRANSACTIONS_BUTTON = "Последние 10 операций"
ANALYTICS_BUTTON = "Аналитика"
CATEGORY_ANALYTICS_BUTTON = "Аналитика по категориям"
VERSION_BUTTON = "Версия"
CANCEL_TRANSACTION_BUTTON = "Отменить текущую запись"
_MAX_REPLY_TEXT_LENGTH = 2000
_ANALYTICS_CATEGORIES = (
    "Еда офис",
    "Кафе/Рестораны",
    "Транспорт",
    "Продукты",
)
_MONTH_NAMES = (
    "",
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)


class State(IntEnum):
    CUSTOM_DATE = 0
    TYPE = 1
    CATEGORY = 2
    SUBCATEGORY = 3
    AMOUNT = 4
    NOTE_TEXT = 5
    CONFIRM = 6


_FLOW_KEYS = (
    "pnl_date",
    "pnl_type",
    "pnl_category",
    "pnl_subcategory",
    "pnl_amount",
    "pnl_note",
    "pnl_optional_note",
    "pnl_options",
    "pnl_choices",
)


def _forget_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in _FLOW_KEYS:
        context.user_data.pop(key, None)


def _cancel_keyboard() -> list[list[InlineKeyboardButton]]:
    return [[InlineKeyboardButton("Отмена", callback_data="flow:cancel")]]


def _main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [ADD_TODAY_TRANSACTION_BUTTON, ADD_YESTERDAY_TRANSACTION_BUTTON],
            [ADD_TRANSACTION_BUTTON, RECENT_TRANSACTIONS_BUTTON],
            [ANALYTICS_BUTTON, CATEGORY_ANALYTICS_BUTTON],
            [VERSION_BUTTON, CANCEL_TRANSACTION_BUTTON],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _choice_keyboard(
    choices: Sequence[str], callback_prefix: str, columns: int = 2
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(label, callback_data=f"{callback_prefix}:{index}")
        for index, label in enumerate(choices)
    ]
    rows = [buttons[index : index + columns] for index in range(0, len(buttons), columns)]
    rows.extend(_cancel_keyboard())
    return InlineKeyboardMarkup(rows)


def _format_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _parse_date(value: str) -> date:
    text = value.strip()
    for date_format in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    raise ValueError("Введите дату в формате ДД.ММ.ГГГГ, например 10.10.2026.")


def _parse_amount(value: str) -> int:
    text = value.strip()
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError("Введите сумму целым числом рублей, без пробелов и копеек.")
    amount = int(text)
    if amount <= 0:
        raise ValueError("Сумма должна быть больше нуля.")
    return amount


def _store(context: ContextTypes.DEFAULT_TYPE) -> Any:
    return context.application.bot_data["sheets_store"]


def _get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


async def _reject_unauthorized_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """CallbackQueryHandler has no PTB user filter; enforce the allowlist here."""
    query = update.callback_query
    settings = _get_settings(context)
    if query is None or query.from_user.id != settings.allowed_telegram_user_id:
        if query is not None:
            await query.answer()
        return True
    return False


def _timezone(context: ContextTypes.DEFAULT_TYPE) -> ZoneInfo:
    timezone_name = _get_settings(context).timezone
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown BOT_TIMEZONE: {timezone_name}") from exc


def _taxonomy(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, str]]:
    return context.user_data["pnl_options"]


def _set_subcategory(
    context: ContextTypes.DEFAULT_TYPE, subcategory: str
) -> str:
    transaction_type = context.user_data["pnl_type"]
    context.user_data["pnl_subcategory"] = subcategory
    context.user_data["pnl_category"] = (
        "Доход"
        if transaction_type == "Доход"
        else context.user_data["pnl_category"]
    )
    return f"Подкатегория: {subcategory}\nВведите сумму целым числом рублей:"


def _render_preview(context: ContextTypes.DEFAULT_TYPE) -> str:
    kind = context.user_data["pnl_type"]
    label = "Доход" if kind == "Доход" else "Расход"
    note = context.user_data.get("pnl_note", "") or "—"
    return (
        "Проверьте операцию:\n"
        f"Дата: {context.user_data['pnl_date']}\n"
        f"Тип: {label}\n"
        f"Категория: {context.user_data['pnl_category']}\n"
        f"Подкатегория: {context.user_data['pnl_subcategory']}\n"
        f"Сумма: {context.user_data['pnl_amount']} ₽\n"
        f"Примечание: {note}\n\n"
        "Записать эту строку в лист «Траты»?"
    )


def _type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Расход", callback_data="type:expense"),
                InlineKeyboardButton("Доход", callback_data="type:income"),
            ],
            *_cancel_keyboard(),
        ]
    )


def _confirmation_keyboard(include_add_note: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if include_add_note:
        rows.append([InlineKeyboardButton("Добавить комментарий", callback_data="note:add")])
    rows.append(
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data="save:yes"),
            InlineKeyboardButton("❌ Отменить", callback_data="save:no"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def start_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Команды:\n"
        f"Кнопка «{ADD_TODAY_TRANSACTION_BUTTON}» — добавить операцию за сегодня\n"
        f"Кнопка «{ADD_YESTERDAY_TRANSACTION_BUTTON}» — добавить операцию за вчера\n"
        f"Кнопка «{ADD_TRANSACTION_BUTTON}» или /add — указать произвольную дату\n"
        f"Кнопка «{RECENT_TRANSACTIONS_BUTTON}» — показать последние 10 операций\n"
        f"Кнопка «{ANALYTICS_BUTTON}» — сводка за месяц и траты по категориям\n"
        f"Кнопка «{CATEGORY_ANALYTICS_BUTTON}» — аналитика выбранной категории\n"
        f"Кнопка «{VERSION_BUTTON}» или /version — показать версию бота\n"
        f"Кнопка «{CANCEL_TRANSACTION_BUTTON}» или /cancel — отменить текущий ввод\n"
        "/help — показать эту справку\n\n"
        "После заполнения бот покажет строку для подтверждения и добавит её "
        "только в лист «Траты».",
        reply_markup=_main_menu_keyboard(),
    )


async def show_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(version_text())


def _recent_transaction_messages(rows: Sequence[Sequence[str]]) -> list[str]:
    if not rows:
        return ["В таблице пока нет операций."]

    blocks = ["Последние операции (от более старых к новым):"]
    for number, row in enumerate(rows, start=1):
        date_value, category, subcategory, amount, transaction_type, note = row
        blocks.append(
            f"{number}. Дата: {date_value}\n"
            f"   Тип: {transaction_type}\n"
            f"   Категория: {category}\n"
            f"   Подкатегория: {subcategory}\n"
            f"   Сумма: {amount} ₽\n"
            f"   Примечание: {note or '—'}"
        )

    messages: list[str] = []
    current = ""
    for block in blocks:
        for offset in range(0, len(block), _MAX_REPLY_TEXT_LENGTH):
            part = block[offset : offset + _MAX_REPLY_TEXT_LENGTH]
            if current and len(current) + len(part) + 2 > _MAX_REPLY_TEXT_LENGTH:
                messages.append(current)
                current = ""
            current = f"{current}\n\n{part}" if current else part
    if current:
        messages.append(current)
    return messages


async def show_recent_transactions(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    try:
        rows = await asyncio.to_thread(_store(context).get_recent_transactions)
    except SheetsError as exc:
        logger.warning("Could not read recent transactions: %s", exc)
        await update.effective_message.reply_text(
            "Не удалось прочитать последние операции из листа «Траты». "
            "Проверьте доступ Google и попробуйте ещё раз."
        )
        return

    for message in _recent_transaction_messages(rows):
        await update.effective_message.reply_text(message)


def _format_rubles(amount: Decimal) -> str:
    return f"{amount:,.2f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")


def _analytics_period(analytics: MonthlyAnalytics) -> str:
    return f"{_MONTH_NAMES[analytics.month]} {analytics.year}"


def _monthly_analytics_text(analytics: MonthlyAnalytics) -> str:
    categories = "\n".join(
        f"• {value.label}: {_format_rubles(value.amount)} ₽"
        for value in analytics.categories
    )
    return (
        f"Аналитика за {_analytics_period(analytics)}:\n"
        f"Расходы: {_format_rubles(analytics.expenses)} ₽\n"
        f"Доход: {_format_rubles(analytics.income)} ₽\n"
        f"Остаток на текущий момент: {_format_rubles(analytics.balance)} ₽\n\n"
        f"Траты по категориям:\n{categories}"
    )


def _category_analytics_text(analytics: CategoryAnalytics, period: str) -> str:
    subcategories = "\n".join(
        f"• {value.label}: {_format_rubles(value.amount)} ₽"
        for value in analytics.subcategories
    )
    return (
        f"Аналитика по категории «{analytics.category}» за {period}:\n"
        f"Всего: {_format_rubles(analytics.total)} ₽\n"
        f"Подкатегории:\n{subcategories}"
    )


async def show_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        analytics = await asyncio.to_thread(_store(context).get_monthly_analytics)
    except SheetsError as exc:
        logger.warning("Could not read monthly analytics: %s", exc)
        await update.effective_message.reply_text(
            "Не удалось загрузить аналитику из листа «Аналитика». "
            "Проверьте лист и доступ Google, затем попробуйте ещё раз."
        )
        return

    period = _analytics_period(analytics)
    await update.effective_message.reply_text(_monthly_analytics_text(analytics))
    await update.effective_message.reply_photo(
        photo=render_donut_chart(f"Траты по категориям · {period}", analytics.categories)
    )


async def show_category_analytics_choices(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    buttons = [
        InlineKeyboardButton(category, callback_data=f"analytics:category:{index}")
        for index, category in enumerate(_ANALYTICS_CATEGORIES)
    ]
    keyboard = InlineKeyboardMarkup(
        [buttons[:2], buttons[2:]]
    )
    await update.effective_message.reply_text(
        "Выберите категорию:",
        reply_markup=keyboard,
    )


async def show_category_analytics(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if await _reject_unauthorized_callback(update, context):
        return

    query = update.callback_query
    await query.answer()
    category_index = int(query.data.rsplit(":", 1)[1])
    if not 0 <= category_index < len(_ANALYTICS_CATEGORIES):
        await query.edit_message_text("Не удалось определить категорию. Откройте выбор ещё раз.")
        return

    category = _ANALYTICS_CATEGORIES[category_index]
    try:
        analytics = await asyncio.to_thread(
            _store(context).get_category_analytics, category
        )
    except SheetsError as exc:
        logger.warning("Could not read analytics for %s: %s", category, exc)
        await query.edit_message_text(
            "Не удалось загрузить аналитику из листа «Аналитика». "
            "Проверьте лист и доступ Google, затем попробуйте ещё раз."
        )
        return

    period = f"{_MONTH_NAMES[analytics.month]} {analytics.year}"
    await query.edit_message_text(_category_analytics_text(analytics, period))
    await update.effective_message.reply_photo(
        photo=render_donut_chart(
            f"Распределение · {analytics.category} · {period}",
            analytics.subcategories,
        )
    )


async def _start_add(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    preset_day: str | None = None,
) -> int:
    _forget_flow(context)
    try:
        options: list[CategoryOption] = await asyncio.to_thread(
            _store(context).load_taxonomy
        )
    except SheetsError as exc:
        logger.warning("Could not load category choices: %s", exc)
        await update.effective_message.reply_text(
            "Не удалось загрузить категории из таблицы. Проверьте лист «Категории» "
            "и доступ Google, затем попробуйте /add ещё раз."
        )
        return ConversationHandler.END

    context.user_data["pnl_options"] = [
        {
            "type": option.transaction_type,
            "category": option.category,
            "subcategory": option.subcategory,
        }
        for option in options
    ]
    if preset_day is None:
        context.user_data["pnl_optional_note"] = True
        await update.effective_message.reply_text(
            "Введите дату в формате ДД.ММ.ГГГГ (например, 10.10.2026).",
            reply_markup=InlineKeyboardMarkup(_cancel_keyboard()),
        )
        return State.CUSTOM_DATE

    local_today = datetime.now(_timezone(context)).date()
    selected = local_today if preset_day == "today" else local_today - timedelta(days=1)
    context.user_data["pnl_date"] = _format_date(selected)
    context.user_data["pnl_optional_note"] = True
    await update.effective_message.reply_text(
        f"Дата: {_format_date(selected)}\nВыберите тип операции:",
        reply_markup=_type_keyboard(),
    )
    return State.TYPE


async def start_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _start_add(update, context)


async def start_add_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _start_add(update, context, "today")


async def start_add_yesterday(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    return await _start_add(update, context, "yesterday")


async def receive_custom_date(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    try:
        selected = _parse_date(update.effective_message.text)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return State.CUSTOM_DATE

    context.user_data["pnl_date"] = _format_date(selected)
    await update.effective_message.reply_text(
        f"Дата: {_format_date(selected)}\nВыберите тип операции:",
        reply_markup=_type_keyboard(),
    )
    return State.TYPE


async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _reject_unauthorized_callback(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    transaction_type = "Доход" if query.data.endswith("income") else "Расход"
    context.user_data["pnl_type"] = transaction_type
    options = _taxonomy(context)

    if transaction_type == "Доход":
        choices = sorted(
            {row["subcategory"] for row in options if row["type"] == "Доход"},
            key=str.casefold,
        )
        if not choices:
            await query.edit_message_text(
                "Для доходов нет подкатегорий в листе «Категории»."
            )
            return ConversationHandler.END
        if len(choices) == 1:
            await query.edit_message_text(_set_subcategory(context, choices[0]))
            return State.AMOUNT
        context.user_data["pnl_choices"] = choices
        await query.edit_message_text(
            "Выберите источник дохода:",
            reply_markup=_choice_keyboard(choices, "sub"),
        )
        return State.SUBCATEGORY

    choices = sorted(
        {row["category"] for row in options if row["type"] == "Расход"},
        key=str.casefold,
    )
    if not choices:
        await query.edit_message_text("Для расходов нет категорий в листе «Категории». ")
        return ConversationHandler.END
    context.user_data["pnl_choices"] = choices
    await query.edit_message_text(
        "Выберите категорию расхода:",
        reply_markup=_choice_keyboard(choices, "cat"),
    )
    return State.CATEGORY


async def choose_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _reject_unauthorized_callback(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.split(":", maxsplit=1)[1])
        category = context.user_data["pnl_choices"][index]
    except (IndexError, KeyError, ValueError):
        await query.edit_message_text("Кнопка устарела. Начните заново командой /add.")
        return ConversationHandler.END

    context.user_data["pnl_category"] = category
    choices = sorted(
        {
            row["subcategory"]
            for row in _taxonomy(context)
            if row["type"] == "Расход" and row["category"] == category
        },
        key=str.casefold,
    )
    if not choices:
        await query.edit_message_text(
            "Для этой категории нет подкатегорий в листе «Категории». "
            "Обновите список и начните заново командой /add."
        )
        return ConversationHandler.END
    if len(choices) == 1:
        await query.edit_message_text(_set_subcategory(context, choices[0]))
        return State.AMOUNT
    context.user_data["pnl_choices"] = choices
    await query.edit_message_text(
        f"Категория: {category}\nВыберите подкатегорию:",
        reply_markup=_choice_keyboard(choices, "sub"),
    )
    return State.SUBCATEGORY


async def choose_subcategory(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _reject_unauthorized_callback(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.split(":", maxsplit=1)[1])
        subcategory = context.user_data["pnl_choices"][index]
    except (IndexError, KeyError, ValueError):
        await query.edit_message_text("Кнопка устарела. Начните заново командой /add.")
        return ConversationHandler.END

    await query.edit_message_text(_set_subcategory(context, subcategory))
    return State.AMOUNT


async def receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = _parse_amount(update.effective_message.text)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return State.AMOUNT

    context.user_data["pnl_amount"] = amount
    context.user_data["pnl_note"] = ""
    await update.effective_message.reply_text(
        _render_preview(context),
        reply_markup=_confirmation_keyboard(include_add_note=True),
    )
    return State.CONFIRM


async def receive_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    note = update.effective_message.text.strip()
    if not note:
        await update.effective_message.reply_text("Примечание не может быть пустым.")
        return State.NOTE_TEXT
    if len(note) > 1000:
        await update.effective_message.reply_text(
            "Слишком длинное примечание. Максимум — 1000 символов."
        )
        return State.NOTE_TEXT

    context.user_data["pnl_note"] = note
    await update.effective_message.reply_text(
        _render_preview(context),
        reply_markup=_confirmation_keyboard(
            include_add_note=bool(context.user_data.get("pnl_optional_note"))
            and not note
        ),
    )
    return State.CONFIRM


async def request_optional_note(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _reject_unauthorized_callback(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Введите комментарий (до 1000 символов):",
        reply_markup=InlineKeyboardMarkup(_cancel_keyboard()),
    )
    return State.NOTE_TEXT


async def confirm_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await _reject_unauthorized_callback(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    if query.data.endswith("no"):
        await query.edit_message_text("Операция отменена; таблица не изменена.")
        _forget_flow(context)
        return ConversationHandler.END

    row: list[str | int] = [
        context.user_data["pnl_date"],
        context.user_data["pnl_category"],
        context.user_data["pnl_subcategory"],
        context.user_data["pnl_amount"],
        context.user_data["pnl_type"],
        context.user_data.get("pnl_note", ""),
    ]
    await query.edit_message_reply_markup(reply_markup=None)
    try:
        await asyncio.to_thread(_store(context).append_transaction, row)
    except SheetsError as exc:
        logger.warning("Transaction append failed or has an uncertain outcome: %s", exc)
        await query.message.reply_text(
            "Не удалось подтвердить результат записи. Чтобы не создать дубль, "
            "бот не повторял запрос. Проверьте лист «Траты» вручную перед новой попыткой."
        )
        _forget_flow(context)
        return ConversationHandler.END

    await query.edit_message_text("✅ Операция добавлена в лист «Траты».")
    _forget_flow(context)
    return ConversationHandler.END


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        if await _reject_unauthorized_callback(update, context):
            return ConversationHandler.END
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "Ввод отменён; таблица не изменена."
        )
    elif update.effective_message:
        await update.effective_message.reply_text("Ввод отменён; таблица не изменена.")
    _forget_flow(context)
    return ConversationHandler.END


async def cancel_current_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not any(key in context.user_data for key in _FLOW_KEYS):
        await update.effective_message.reply_text("Нет активной записи для отмены.")
        return ConversationHandler.END
    return await cancel_flow(update, context)


async def stale_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if await _reject_unauthorized_callback(update, context):
        return ConversationHandler.END
    if query:
        await query.answer("Эта кнопка уже не активна. Начните с /add.", show_alert=True)
    return ConversationHandler.END


def build_conversation(settings: Settings) -> ConversationHandler:
    authorized = filters.User(user_id=settings.allowed_telegram_user_id)
    main_menu_button = filters.Regex(
        rf"^({re.escape(ADD_TRANSACTION_BUTTON)}|"
        rf"{re.escape(ADD_TODAY_TRANSACTION_BUTTON)}|"
        rf"{re.escape(ADD_YESTERDAY_TRANSACTION_BUTTON)}|"
        rf"{re.escape(RECENT_TRANSACTIONS_BUTTON)}|"
        rf"{re.escape(ANALYTICS_BUTTON)}|"
        rf"{re.escape(CATEGORY_ANALYTICS_BUTTON)}|"
        rf"{re.escape(VERSION_BUTTON)}|"
        rf"{re.escape(CANCEL_TRANSACTION_BUTTON)})$"
    )
    text_input = filters.TEXT & ~filters.COMMAND & ~main_menu_button & authorized
    return ConversationHandler(
        entry_points=[
            CommandHandler("add", start_add, filters=authorized),
            MessageHandler(
                filters.Regex(rf"^{re.escape(ADD_TRANSACTION_BUTTON)}$") & authorized,
                start_add,
            ),
            MessageHandler(
                filters.Regex(rf"^{re.escape(ADD_TODAY_TRANSACTION_BUTTON)}$") & authorized,
                start_add_today,
            ),
            MessageHandler(
                filters.Regex(rf"^{re.escape(ADD_YESTERDAY_TRANSACTION_BUTTON)}$") & authorized,
                start_add_yesterday,
            ),
            MessageHandler(
                filters.Regex(rf"^{re.escape(CANCEL_TRANSACTION_BUTTON)}$") & authorized,
                cancel_current_entry,
            ),
        ],
        states={
            State.CUSTOM_DATE: [MessageHandler(text_input, receive_custom_date)],
            State.TYPE: [
                CallbackQueryHandler(choose_type, pattern=r"^type:(expense|income)$")
            ],
            State.CATEGORY: [
                CallbackQueryHandler(choose_category, pattern=r"^cat:[0-9]+$")
            ],
            State.SUBCATEGORY: [
                CallbackQueryHandler(choose_subcategory, pattern=r"^sub:[0-9]+$")
            ],
            State.AMOUNT: [MessageHandler(text_input, receive_amount)],
            State.NOTE_TEXT: [MessageHandler(text_input, receive_note)],
            State.CONFIRM: [
                CallbackQueryHandler(confirm_save, pattern=r"^save:(yes|no)$"),
                CallbackQueryHandler(request_optional_note, pattern=r"^note:add$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_flow, filters=authorized),
            CommandHandler("help", start_help, filters=authorized),
            CommandHandler("version", show_version, filters=authorized),
            MessageHandler(
                filters.Regex(rf"^{re.escape(CANCEL_TRANSACTION_BUTTON)}$") & authorized,
                cancel_current_entry,
            ),
            CallbackQueryHandler(cancel_flow, pattern=r"^flow:cancel$"),
            CallbackQueryHandler(stale_callback),
        ],
        allow_reentry=True,
        # This flow intentionally mixes command/text updates with inline buttons;
        # per-user + per-chat isolation is therefore used instead of per-message.
        per_user=True,
        per_chat=True,
    )


def register_handlers(application: Application, settings: Settings) -> None:
    authorized = filters.User(user_id=settings.allowed_telegram_user_id)
    application.add_handler(
        MessageHandler(
            filters.Regex(rf"^{re.escape(ANALYTICS_BUTTON)}$") & authorized,
            show_analytics,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(rf"^{re.escape(CATEGORY_ANALYTICS_BUTTON)}$") & authorized,
            show_category_analytics_choices,
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            show_category_analytics,
            pattern=r"^analytics:category:[0-9]+$",
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(rf"^{re.escape(RECENT_TRANSACTIONS_BUTTON)}$") & authorized,
            show_recent_transactions,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(rf"^{re.escape(VERSION_BUTTON)}$") & authorized,
            show_version,
        )
    )
    application.add_handler(CommandHandler("version", show_version, filters=authorized))
    application.add_handler(build_conversation(settings))
    application.add_handler(CommandHandler(["start", "help"], start_help, filters=authorized))
    application.add_handler(
        CallbackQueryHandler(stale_callback),
        group=1,
    )
