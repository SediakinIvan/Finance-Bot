"""Telegram conversation for appending transactions to the PnL ledger."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
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
from finance_bot.sheets import (
    CategoryOption,
    SheetsError,
    TransactionNotWrittenError,
    StaleTransactionError,
    TransactionRecord,
)
from finance_bot.version import version_text
from finance_bot.taxonomy import new_id
from finance_bot.v2_sheets import TaxonomyChangedError
from finance_bot import category_ui

logger = logging.getLogger(__name__)
ADD_TRANSACTION_BUTTON = "Добавить транзакцию"
ADD_TODAY_TRANSACTION_BUTTON = "Добавить транзакцию (сегодня)"
ADD_YESTERDAY_TRANSACTION_BUTTON = "Добавить транзакцию (вчера)"
RECENT_TRANSACTIONS_BUTTON = "Последние 10 операций"
DELETE_LAST_BUTTON = "Удалить последнюю"
DELETE_RECENT_BUTTON = "Выбрать для удаления"
ANALYTICS_BUTTON = "Аналитика"
CATEGORY_ANALYTICS_BUTTON = "Аналитика по категориям"
VERSION_BUTTON = "Версия"
CANCEL_TRANSACTION_BUTTON = "Отменить текущую запись"
_MAX_REPLY_TEXT_LENGTH = 2000
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
    DELETE_SELECT = 7
    DELETE_CONFIRM = 8


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
    "pnl_amount_input",
    "pnl_amount_token",
    "delete_records",
    "delete_pending",
    "delete_token",
    "pnl_category_id",
    "pnl_subcategory_id",
    "pnl_revision",
    "pnl_transaction_id",
    category_ui.FLOW_KEY,
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
            [DELETE_LAST_BUTTON, DELETE_RECENT_BUTTON],
            [ANALYTICS_BUTTON, CATEGORY_ANALYTICS_BUTTON],
            [VERSION_BUTTON, CANCEL_TRANSACTION_BUTTON],
            [category_ui.CATEGORIES_BUTTON],
        ],
        resize_keyboard=True,
        is_persistent=False,
    )


def _choice_keyboard(
    choices: Sequence[str],
    callback_prefix: str,
    columns: int = 2,
    emoji_context: str = "subcategory",
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            f"{_choice_emoji(label, emoji_context)} {label}",
            callback_data=f"{callback_prefix}:{index}",
        )
        for index, label in enumerate(choices)
    ]
    rows = [buttons[index : index + columns] for index in range(0, len(buttons), columns)]
    rows.extend(_cancel_keyboard())
    return InlineKeyboardMarkup(rows)


_CATEGORY_EMOJIS = {
    "еда офис": "🍱",
    "кафе/рестораны": "🍽️",
    "транспорт": "🚗",
    "продукты": "🛒",
    "вб+озон": "📦",
    "спорт": "🏐",
    "подписки": "🔁",
    "отдых/развлечения": "🎭",
    "здоровье": "🩺",
    "квартира": "🏠",
    "развитие": "📚",
    "путешествия": "✈️",
}
_INCOME_EMOJIS = {
    "зарплата": "💼",
    "стипендия": "🎓",
    "от родителей": "👪",
    "другое": "💰",
}
_SUBCATEGORY_EMOJIS = {
    "столовая": "🍽️",
    "самокат": "🛴",
    "сладкое": "🍰",
    "кафе": "☕",
    "кофе": "☕",
    "ресторан": "🍽️",
    "фастфуд": "🍔",
    "магазин": "🛍️",
    "доставка": "🛵",
    "перекус": "🥪",
    "готовая еда": "🍱",
    "штраф": "🚨",
    "штрафы": "🚨",
    "бенз": "⛽",
    "общественный": "🚌",
    "платка": "🛣️",
    "самокаты": "🛴",
    "карш": "🚘",
    "такси": "🚕",
    "whoosh": "🛴",
    "вб+озон": "📦",
    "волейбол": "🏐",
    "подписки": "🔁",
    "отдых/развлечения": "🎭",
    "здоровье": "🩺",
    "квартира": "🏠",
    "развитие": "📚",
    "путешествия": "✈️",
}


def _choice_emoji(label: str, context: str) -> str:
    normalized = label.strip().casefold()
    if context == "category":
        return _CATEGORY_EMOJIS.get(normalized, "🏷️")
    if context == "income":
        return _INCOME_EMOJIS.get(normalized, "🔹")
    return _SUBCATEGORY_EMOJIS.get(normalized, "🔹")


def _amount_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    token = context.user_data["pnl_amount_token"]
    rows = [
        [
            InlineKeyboardButton(
                str(number),
                callback_data=f"amount:{token}:digit:{number}",
            )
            for number in numbers
        ]
        for numbers in ((1, 2, 3), (4, 5, 6), (7, 8, 9))
    ]
    rows.append(
        [
            InlineKeyboardButton("⌫", callback_data=f"amount:{token}:backspace"),
            InlineKeyboardButton("0", callback_data=f"amount:{token}:digit:0"),
            InlineKeyboardButton("🧹", callback_data=f"amount:{token}:clear"),
        ]
    )
    rows.append(
        [InlineKeyboardButton("Готово", callback_data=f"amount:{token}:done")]
    )
    rows.extend(_cancel_keyboard())
    return InlineKeyboardMarkup(rows)


def _delete_confirmation_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🗑 Удалить",
                    callback_data=f"delete:confirm:{token}",
                ),
                InlineKeyboardButton(
                    "Не удалять",
                    callback_data=f"delete:cancel:{token}",
                ),
            ]
        ]
    )


def _delete_choice_keyboard(
    records: Sequence[TransactionRecord], token: str
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, record in enumerate(reversed(records), start=1):
        date_value, category, _, amount, _, _ = record.values
        label = f"{index}. {date_value} · {category[:22]} · {amount[:12]} ₽"
        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"delete:select:{token}:{index - 1}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "Отмена",
                callback_data=f"delete:cancel:{token}",
            )
        ]
    )
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


def _store(context: ContextTypes.DEFAULT_TYPE, update: Update) -> Any:
    user = update.effective_user
    if user is None:
        raise RuntimeError("Cannot select a spreadsheet without a Telegram user.")
    return context.application.bot_data["sheets_stores"][user.id]


def _get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


async def _reject_unauthorized_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """CallbackQueryHandler has no PTB user filter; enforce the allowlist here."""
    query = update.callback_query
    settings = _get_settings(context)
    chat = update.effective_chat
    if (
        query is None
        or query.from_user.id not in settings.authorized_telegram_user_ids
        or chat is None
        or chat.type != "private"
    ):
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


def _taxonomy(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, Any]]:
    return context.user_data["pnl_options"]


def _set_subcategory(
    context: ContextTypes.DEFAULT_TYPE, subcategory: str
) -> str:
    transaction_type = context.user_data["pnl_type"]
    context.user_data["pnl_subcategory"] = subcategory
    income_matches = [item for item in context.user_data.get("pnl_options", [])
                      if item["type"] == "Доход" and item["subcategory"] == subcategory]
    if transaction_type == "Доход":
        selected = context.user_data.get("pnl_category")
        context.user_data["pnl_category"] = (
            selected if any(item["category"] == selected for item in income_matches)
            else income_matches[0]["category"] if len(income_matches) == 1 else "Доход"
        )
    matching = [item for item in context.user_data.get("pnl_options", [])
                if item["type"] == transaction_type
                and item["category"] == context.user_data["pnl_category"]
                and item["subcategory"] == subcategory]
    if len(matching) == 1 and matching[0].get("category_id"):
        context.user_data["pnl_category_id"] = matching[0]["category_id"]
        context.user_data["pnl_subcategory_id"] = matching[0]["subcategory_id"]
        context.user_data["pnl_revision"] = matching[0]["revision"]
        context.user_data["pnl_transaction_id"] = new_id()
    context.user_data["pnl_amount_input"] = ""
    context.user_data["pnl_amount_token"] = secrets.token_hex(4)
    return _amount_prompt(context)


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
    await category_ui.leave_categories(update, context)
    await update.effective_message.reply_text(
        "Команды:\n"
        f"Кнопка «{ADD_TODAY_TRANSACTION_BUTTON}» — добавить операцию за сегодня\n"
        f"Кнопка «{ADD_YESTERDAY_TRANSACTION_BUTTON}» — добавить операцию за вчера\n"
        f"Кнопка «{ADD_TRANSACTION_BUTTON}» или /add — указать произвольную дату\n"
        f"Кнопка «{RECENT_TRANSACTIONS_BUTTON}» — показать последние 10 операций\n"
        f"Кнопки «{DELETE_LAST_BUTTON}» и «{DELETE_RECENT_BUTTON}» — удалить операцию\n"
        f"Кнопка «{ANALYTICS_BUTTON}» — сводка за месяц и траты по категориям\n"
        f"Кнопка «{CATEGORY_ANALYTICS_BUTTON}» — аналитика выбранной категории\n"
        f"Кнопка «{VERSION_BUTTON}» или /version — показать версию бота\n"
        f"Кнопка «{category_ui.CATEGORIES_BUTTON}» или /categories — управление категориями\n"
        f"Кнопка «{CANCEL_TRANSACTION_BUTTON}» или /cancel — отменить текущий ввод\n"
        "/help — показать эту справку\n\n"
        "После заполнения бот покажет строку для подтверждения и добавит её "
        "только в лист «Траты».",
        reply_markup=_main_menu_keyboard(),
    )


async def show_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await category_ui.leave_categories(update, context)
    await update.effective_message.reply_text(version_text())


def _recent_transaction_messages(records: Sequence[TransactionRecord]) -> list[str]:
    if not records:
        return ["В таблице пока нет операций."]

    blocks = ["Последние операции (от более старых к новым):"]
    for number, record in enumerate(records, start=1):
        row = record.values
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
    await category_ui.leave_categories(update, context)
    try:
        rows = await asyncio.to_thread(_store(context, update).get_recent_transactions)
    except SheetsError as exc:
        logger.warning("Could not read recent transactions: %s", exc)
        await update.effective_message.reply_text(
            "Не удалось прочитать последние операции из листа «Траты». "
            "Проверьте доступ Google и попробуйте ещё раз."
        )
        return

    for message in _recent_transaction_messages(rows):
        await update.effective_message.reply_text(message)


def _transaction_details(record: TransactionRecord) -> str:
    date_value, category, subcategory, amount, transaction_type, note = record.values
    return (
        f"Дата: {date_value}\n"
        f"Тип: {transaction_type}\n"
        f"Категория: {category}\n"
        f"Подкатегория: {subcategory}\n"
        f"Сумма: {amount} ₽\n"
        f"Примечание: {note or '—'}"
    )


async def _start_deletion(
    update: Update, context: ContextTypes.DEFAULT_TYPE, last_only: bool
) -> int:
    await category_ui.leave_categories(update, context)
    _forget_flow(context)
    token = secrets.token_hex(4)
    context.user_data["delete_token"] = token
    try:
        records = await asyncio.to_thread(
            _store(context, update).get_recent_transactions, 1 if last_only else 10
        )
    except SheetsError as exc:
        logger.warning("Could not load transactions for deletion: %s", exc)
        await update.effective_message.reply_text(
            "Не удалось прочитать операции из листа «Траты». Попробуйте ещё раз."
        )
        _forget_deletion(context)
        return ConversationHandler.END

    if not records:
        await update.effective_message.reply_text("В таблице пока нет операций для удаления.")
        _forget_deletion(context)
        return ConversationHandler.END

    if last_only:
        record = records[-1]
        context.user_data["delete_pending"] = record
        await update.effective_message.reply_text(
            "Удалить последнюю операцию?\n\n" + _transaction_details(record),
            reply_markup=_delete_confirmation_keyboard(token),
        )
        return State.DELETE_CONFIRM

    context.user_data["delete_records"] = records
    await update.effective_message.reply_text(
        "Выберите операцию для удаления (сначала самые новые):",
        reply_markup=_delete_choice_keyboard(records, token),
    )
    return State.DELETE_SELECT


async def start_delete_last(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    return await _start_deletion(update, context, last_only=True)


async def start_delete_recent(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    return await _start_deletion(update, context, last_only=False)


async def select_transaction_for_deletion(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _reject_unauthorized_callback(update, context):
        return ConversationHandler.END
    query = update.callback_query
    try:
        _, _, token, index_text = query.data.split(":")
        if token != context.user_data.get("delete_token"):
            await query.answer(
                "Этот список устарел. Используйте актуальные кнопки удаления.",
                show_alert=True,
            )
            return State.DELETE_SELECT
        index = int(index_text)
        records = context.user_data["delete_records"]
        record = list(reversed(records))[index]
    except (IndexError, KeyError, ValueError):
        await query.answer()
        await query.edit_message_text(
            "Список устарел. Откройте «Выбрать для удаления» ещё раз."
        )
        return ConversationHandler.END

    await query.answer()
    context.user_data["delete_pending"] = record
    await query.edit_message_text(
        "Подтвердите удаление этой операции:\n\n" + _transaction_details(record),
        reply_markup=_delete_confirmation_keyboard(token),
    )
    return State.DELETE_CONFIRM


def _forget_deletion(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("delete_records", None)
    context.user_data.pop("delete_pending", None)
    context.user_data.pop("delete_token", None)


async def confirm_transaction_deletion(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _reject_unauthorized_callback(update, context):
        return ConversationHandler.END
    query = update.callback_query
    action, token = query.data.rsplit(":", maxsplit=1)
    if token != context.user_data.get("delete_token"):
        await query.answer(
            "Эта кнопка устарела. Текущая операция не изменена.",
            show_alert=True,
        )
        return (
            State.DELETE_CONFIRM
            if context.user_data.get("delete_pending") is not None
            else State.DELETE_SELECT
        )
    await query.answer()
    if action == "delete:cancel":
        await query.edit_message_text("Удаление отменено; таблица не изменена.")
        _forget_deletion(context)
        return ConversationHandler.END

    record = context.user_data.get("delete_pending")
    if not isinstance(record, TransactionRecord):
        await query.edit_message_text(
            "Выбор устарел. Откройте удаление операции ещё раз."
        )
        _forget_deletion(context)
        return ConversationHandler.END

    try:
        await asyncio.to_thread(_store(context, update).delete_transaction, record)
    except StaleTransactionError:
        logger.info("Deletion rejected because the selected transaction row changed.")
        await query.edit_message_text(
            "Операция изменилась после выбора; ничего не удалено. "
            "Откройте список операций ещё раз."
        )
        _forget_deletion(context)
        return ConversationHandler.END
    except SheetsError as exc:
        logger.warning("Could not delete transaction: %s", exc)
        await query.edit_message_text(
            "Не удалось удалить операцию из листа «Траты». "
            "Проверьте таблицу и попробуйте снова."
        )
        _forget_deletion(context)
        return ConversationHandler.END

    await query.edit_message_text("✅ Операция удалена из листа «Траты».")
    _forget_deletion(context)
    return ConversationHandler.END


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
    await category_ui.leave_categories(update, context)
    try:
        analytics = await asyncio.to_thread(_store(context, update).get_monthly_analytics)
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
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await category_ui.leave_categories(update, context)
    if update.callback_query:
        if await _reject_unauthorized_callback(update, context):
            return
        await update.callback_query.answer()
        page = int(update.callback_query.data.rsplit(":", 1)[1])
    else:
        page = 0
    try:
        choices = await asyncio.to_thread(_store(context, update).get_analytics_categories)
    except SheetsError:
        await update.effective_message.reply_text("Не удалось загрузить категории аналитики. Проверьте таблицу.")
        return
    if not choices:
        await update.effective_message.reply_text("Нет категорий для аналитики.")
        return
    pages = (len(choices) + 7) // 8
    if not 0 <= page < pages:
        await update.effective_message.reply_text("Список изменился. Откройте аналитику заново.")
        return
    buttons = [
        InlineKeyboardButton(
            f"{_choice_emoji(category, 'category')} {category}",
            callback_data=f"analytics:category:{category_id}",
        )
        for category_id, category in choices[page * 8:page * 8 + 8]
    ]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton("Назад", callback_data=f"analytics:page:{page - 1}"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton("Далее", callback_data=f"analytics:page:{page + 1}"))
    if navigation:
        rows.append(navigation)
    reply = update.callback_query.edit_message_text if update.callback_query else update.effective_message.reply_text
    await reply(
        "Выберите категорию:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def show_category_analytics(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if await _reject_unauthorized_callback(update, context):
        return

    query = update.callback_query
    await query.answer()
    category = query.data.rsplit(":", 1)[1]
    try:
        analytics = await asyncio.to_thread(
            _store(context, update).get_category_analytics, category
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
    await category_ui.leave_categories(update, context)
    _forget_flow(context)
    try:
        options: list[CategoryOption] = await asyncio.to_thread(
            _store(context, update).load_taxonomy
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
            "category_id": option.category_id,
            "subcategory_id": option.subcategory_id,
            "revision": option.revision,
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
        categories = sorted({row["category"] for row in options if row["type"] == "Доход"}, key=str.casefold)
        if len(categories) > 1:
            context.user_data["pnl_choices"] = categories
            await query.edit_message_text("Выберите категорию дохода:", reply_markup=_choice_keyboard(categories, "cat"))
            return State.CATEGORY
        if categories:
            context.user_data["pnl_category"] = categories[0]
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
            await query.edit_message_text(
                _set_subcategory(context, choices[0]),
                reply_markup=_amount_keyboard(context),
            )
            return State.AMOUNT
        context.user_data["pnl_choices"] = choices
        await query.edit_message_text(
            "Выберите источник дохода:",
            reply_markup=_choice_keyboard(choices, "sub", emoji_context="income"),
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
        reply_markup=_choice_keyboard(choices, "cat", emoji_context="category"),
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
            if row["type"] == context.user_data["pnl_type"] and row["category"] == category
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
        await query.edit_message_text(
            _set_subcategory(context, choices[0]),
            reply_markup=_amount_keyboard(context),
        )
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

    await query.edit_message_text(
        _set_subcategory(context, subcategory),
        reply_markup=_amount_keyboard(context),
    )
    return State.AMOUNT


def _amount_prompt(context: ContextTypes.DEFAULT_TYPE) -> str:
    amount = context.user_data.get("pnl_amount_input", "")
    return (
        f"Категория: {context.user_data['pnl_category']}\n"
        f"Подкатегория: {context.user_data['pnl_subcategory']}\n"
        "Введите сумму целым числом рублей:\n\n"
        f"Сумма: {amount or '—'} ₽"
    )


async def _complete_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int
) -> None:
    context.user_data["pnl_amount"] = amount
    context.user_data["pnl_note"] = ""
    if update.callback_query:
        await update.callback_query.edit_message_text(
            _render_preview(context),
            reply_markup=_confirmation_keyboard(include_add_note=True),
        )
    else:
        await update.effective_message.reply_text(
            _render_preview(context),
            reply_markup=_confirmation_keyboard(include_add_note=True),
        )


async def receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = _parse_amount(update.effective_message.text)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return State.AMOUNT

    await _complete_amount(update, context, amount)
    return State.CONFIRM


async def update_amount_from_keypad(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if await _reject_unauthorized_callback(update, context):
        return ConversationHandler.END
    query = update.callback_query
    _, token, action, *digit = query.data.split(":")
    if token != context.user_data.get("pnl_amount_token"):
        await query.answer(
            "Эта клавиатура устарела. Используйте текущий ввод суммы.",
            show_alert=True,
        )
        return State.AMOUNT
    value = context.user_data.get("pnl_amount_input", "")

    if action == "done":
        try:
            amount = _parse_amount(value)
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return State.AMOUNT
        await query.answer()
        await _complete_amount(update, context, amount)
        return State.CONFIRM

    if action == "backspace":
        value = value[:-1]
    elif action == "clear":
        value = ""
    elif action == "digit":
        value += digit[0]
    else:
        await query.answer("Эта кнопка больше не активна.", show_alert=True)
        return State.AMOUNT

    if value == context.user_data.get("pnl_amount_input", ""):
        await query.answer()
        return State.AMOUNT

    context.user_data["pnl_amount_input"] = value
    await query.answer()
    await query.edit_message_text(
        _amount_prompt(context),
        reply_markup=_amount_keyboard(context),
    )
    return State.AMOUNT


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
        selected = context.user_data
        kwargs = ({
            "category_id": selected["pnl_category_id"],
            "subcategory_id": selected["pnl_subcategory_id"],
            "expected_revision": selected["pnl_revision"],
            "transaction_id": selected["pnl_transaction_id"],
        } if selected.get("pnl_category_id") else {})
        await asyncio.to_thread(_store(context, update).append_transaction, row, **kwargs)
    except TaxonomyChangedError as exc:
        await query.message.reply_text(str(exc))
        _forget_flow(context)
        return ConversationHandler.END
    except TransactionNotWrittenError as exc:
        await query.message.reply_text(str(exc))
        _forget_flow(context)
        return ConversationHandler.END
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
    if category_ui.FLOW_KEY in context.user_data:
        return await category_ui._close(update, context)
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
    authorized = (
        filters.User(user_id=settings.authorized_telegram_user_ids)
        & filters.ChatType.PRIVATE
    )
    main_menu_button = filters.Regex(
        rf"^({re.escape(ADD_TRANSACTION_BUTTON)}|"
        rf"{re.escape(ADD_TODAY_TRANSACTION_BUTTON)}|"
        rf"{re.escape(ADD_YESTERDAY_TRANSACTION_BUTTON)}|"
        rf"{re.escape(RECENT_TRANSACTIONS_BUTTON)}|"
        rf"{re.escape(DELETE_LAST_BUTTON)}|"
        rf"{re.escape(DELETE_RECENT_BUTTON)}|"
        rf"{re.escape(ANALYTICS_BUTTON)}|"
        rf"{re.escape(CATEGORY_ANALYTICS_BUTTON)}|"
        rf"{re.escape(VERSION_BUTTON)}|"
        rf"{re.escape(category_ui.CATEGORIES_BUTTON)}|"
        rf"{re.escape(CANCEL_TRANSACTION_BUTTON)})$"
    )
    text_input = filters.TEXT & ~filters.COMMAND & ~main_menu_button & authorized
    return ConversationHandler(
        entry_points=[
            CommandHandler("categories", category_ui.start_categories, filters=authorized),
            MessageHandler(filters.Regex(rf"^{re.escape(category_ui.CATEGORIES_BUTTON)}$") & authorized, category_ui.start_categories),
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
            MessageHandler(
                filters.Regex(rf"^{re.escape(DELETE_LAST_BUTTON)}$") & authorized,
                start_delete_last,
            ),
            MessageHandler(
                filters.Regex(rf"^{re.escape(DELETE_RECENT_BUTTON)}$") & authorized,
                start_delete_recent,
            ),
        ],
        states={
            **category_ui.conversation_states(text_input),
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
            State.AMOUNT: [
                MessageHandler(text_input, receive_amount),
                CallbackQueryHandler(
                    update_amount_from_keypad,
                    pattern=r"^amount:[0-9a-f]{8}:(?:digit:[0-9]|backspace|clear|done)$",
                ),
            ],
            State.NOTE_TEXT: [MessageHandler(text_input, receive_note)],
            State.CONFIRM: [
                CallbackQueryHandler(confirm_save, pattern=r"^save:(yes|no)$"),
                CallbackQueryHandler(request_optional_note, pattern=r"^note:add$"),
            ],
            State.DELETE_SELECT: [
                CallbackQueryHandler(
                    select_transaction_for_deletion,
                    pattern=r"^delete:select:[0-9a-f]{8}:[0-9]+$",
                ),
                CallbackQueryHandler(
                    confirm_transaction_deletion,
                    pattern=r"^delete:cancel:[0-9a-f]{8}$",
                ),
            ],
            State.DELETE_CONFIRM: [
                CallbackQueryHandler(
                    confirm_transaction_deletion,
                    pattern=r"^delete:(?:confirm|cancel):[0-9a-f]{8}$",
                )
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
            CallbackQueryHandler(category_ui.reject_stale_category_callback, pattern=r"^tax:"),
            CallbackQueryHandler(stale_callback),
        ],
        allow_reentry=True,
        # This flow intentionally mixes command/text updates with inline buttons;
        # per-user + per-chat isolation is therefore used instead of per-message.
        per_user=True,
        per_chat=True,
    )


def register_handlers(application: Application, settings: Settings) -> None:
    authorized = (
        filters.User(user_id=settings.authorized_telegram_user_ids)
        & filters.ChatType.PRIVATE
    )
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
            pattern=r"^analytics:category:[0-9a-f-]{36}$",
        )
    )
    application.add_handler(CallbackQueryHandler(show_category_analytics_choices, pattern=r"^analytics:page:[0-9]+$"))
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
    )
