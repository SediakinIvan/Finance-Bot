from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import ConversationHandler

from finance_bot.bot import (
    _parse_amount,
    _parse_date,
    _reject_unauthorized_callback,
    State,
    build_conversation,
    choose_subcategory,
    cancel_current_entry,
    request_optional_note,
    show_recent_transactions,
    start_add,
    start_add_today,
    start_add_yesterday,
    start_help,
    show_version,
)
from finance_bot.config import Settings
from finance_bot.sheets import CategoryOption, GoogleSheetsStore, SheetsError, parse_taxonomy_rows


class TaxonomyTests(unittest.TestCase):
    def test_parses_expenses_and_normalizes_income_category(self) -> None:
        rows = [
            ["Тип", "Категория", "Подкатегория"],
            ["Расход", "Еда офис", "Столовая"],
            ["Доход", "Доход", "Зарплата"],
        ]
        self.assertEqual(
            parse_taxonomy_rows(rows),
            [
                CategoryOption("Расход", "Еда офис", "Столовая"),
                CategoryOption("Доход", "Доход", "Зарплата"),
            ],
        )

    def test_ignores_blank_and_unknown_type_rows_and_deduplicates(self) -> None:
        rows = [
            ["Тип", "Категория", "Подкатегория"],
            ["Расход", "Кафе", "Кофе"],
            ["Расход", "Кафе", "Кофе"],
            ["Unknown", "Other", "Other"],
            ["Доход", "Доход", ""],
            ["", "", ""],
        ]
        self.assertEqual(
            parse_taxonomy_rows(rows),
            [CategoryOption("Расход", "Кафе", "Кофе")],
        )

    def test_requires_expected_headers_and_usable_rows(self) -> None:
        with self.assertRaises(SheetsError):
            parse_taxonomy_rows([["Category", "Subcategory"]])
        with self.assertRaises(SheetsError):
            parse_taxonomy_rows(
                [["Тип", "Категория", "Подкатегория"], ["Расход", "", ""]]
            )


class InputValidationTests(unittest.TestCase):
    def test_accepts_supported_date_formats(self) -> None:
        self.assertEqual(_parse_date("10.10.2026"), date(2026, 10, 10))
        self.assertEqual(_parse_date("2026-10-10"), date(2026, 10, 10))

    def test_rejects_invalid_dates(self) -> None:
        for value in ("31.02.2026", "yesterday", "10/10/2026"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _parse_date(value)

    def test_amount_must_be_positive_integer_rubles(self) -> None:
        self.assertEqual(_parse_amount(" 2500 "), 2500)
        for value in ("0", "-1", "1.5", "1,5", "1 000", "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _parse_amount(value)


class TransactionsStoreTests(unittest.TestCase):
    def test_writes_to_first_blank_date_even_when_rows_are_precreated(self) -> None:
        calls: list[tuple[str, object, object]] = []

        class FakeWorksheet:
            row_count = 10

            def get(self, range_name: str) -> list[list[str]]:
                calls.append(("get", range_name, None))
                return [["01.10.2026"], ["02.10.2026"]]

            def update(self, **kwargs: object) -> None:
                calls.append(("update", kwargs, None))

        class FakeSpreadsheet:
            def worksheet(self, name: str) -> FakeWorksheet:
                calls.append(("worksheet", name, None))
                return FakeWorksheet()

        store = GoogleSheetsStore("unused.json", "unused-id")
        store._spreadsheet = FakeSpreadsheet()
        row = ["10.10.2026", "Доход", "Зарплата", 1000, "Доход", ""]
        store.append_transaction(row)

        self.assertEqual(calls[0], ("worksheet", "Траты", None))
        self.assertEqual(calls[1], ("get", "A2:A10", None))
        self.assertEqual(
            calls[2][1],
            {
                "values": [row],
                "range_name": "A4:F4",
                "value_input_option": "RAW",
            },
        )

    def test_rejects_non_six_column_row_without_touching_sheet(self) -> None:
        store = GoogleSheetsStore("unused.json", "unused-id")
        with self.assertRaises(ValueError):
            store.append_transaction(["too", "short"])

    def test_returns_last_ten_dated_rows_in_sheet_order(self) -> None:
        dated_rows = [
            [f"{day:02}.10.2026", "Кафе", "Кофе", str(day * 100), "Расход", ""]
            for day in range(1, 13)
        ]
        transaction_rows = [dated_rows[0], [], *dated_rows[1:]]

        class FakeWorksheet:
            row_count = 20

            def get(self, range_name: str) -> list[list[str]]:
                self.range_name = range_name
                return transaction_rows

        worksheet = FakeWorksheet()

        class FakeSpreadsheetWithRows:
            def worksheet(self, name: str) -> FakeWorksheet:
                return worksheet

        store = GoogleSheetsStore("unused.json", "unused-id")
        store._spreadsheet = FakeSpreadsheetWithRows()

        self.assertEqual(
            store.get_recent_transactions(),
            dated_rows[-10:],
        )
        self.assertEqual(worksheet.range_name, "A2:F20")


class TelegramHandlerTests(unittest.TestCase):
    def test_help_shows_requested_persistent_menu_buttons(self) -> None:
        reply_text = AsyncMock()
        update = SimpleNamespace(
            callback_query=None,
            effective_message=SimpleNamespace(reply_text=reply_text)
        )

        asyncio.run(start_help(update, SimpleNamespace()))

        keyboard = reply_text.await_args.kwargs["reply_markup"]
        self.assertTrue(keyboard.is_persistent)
        self.assertEqual(
            [[button.text for button in row] for row in keyboard.keyboard],
            [
                ["Добавить транзакцию (сегодня)", "Добавить транзакцию (вчера)"],
                ["Добавить транзакцию", "Последние 10 операций"],
                ["Версия", "Отменить текущую запись"],
            ],
        )

    def test_date_buttons_prefill_today_and_yesterday(self) -> None:
        settings = Settings(
            telegram_token="123456:unit-test-token",
            allowed_telegram_user_id=123456,
            google_credentials_file=Path("service-account.json"),
            spreadsheet_id="test-sheet-id",
            timezone="UTC",
        )
        store = SimpleNamespace(
            load_taxonomy=lambda: [CategoryOption("Расход", "Кафе", "Кофе")]
        )
        context = SimpleNamespace(
            user_data={},
            application=SimpleNamespace(
                bot_data={"settings": settings, "sheets_store": store}
            ),
        )
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply_text)
        )

        with patch("finance_bot.bot.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(
                2026, 10, 11, 12, 0, tzinfo=timezone.utc
            )
            today_result = asyncio.run(start_add_today(update, context))
            self.assertEqual(today_result, State.TYPE)
            self.assertEqual(context.user_data["pnl_date"], "11.10.2026")
            self.assertTrue(context.user_data["pnl_optional_note"])

            yesterday_result = asyncio.run(start_add_yesterday(update, context))
            self.assertEqual(yesterday_result, State.TYPE)
            self.assertEqual(context.user_data["pnl_date"], "10.10.2026")
            self.assertTrue(context.user_data["pnl_optional_note"])

            custom_result = asyncio.run(start_add(update, context))
            self.assertEqual(custom_result, State.CUSTOM_DATE)
            self.assertFalse(context.user_data["pnl_optional_note"])

        self.assertEqual(reply_text.await_count, 3)
        self.assertIn("Введите дату", reply_text.await_args_list[-1].args[0])

    def test_preset_date_skips_note_question_but_offers_optional_comment(self) -> None:
        answer = AsyncMock()
        edit_message_text = AsyncMock()
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                from_user=SimpleNamespace(id=123456),
                data="sub:0",
                answer=answer,
                edit_message_text=edit_message_text,
            )
        )
        settings = Settings(
            telegram_token="123456:unit-test-token",
            allowed_telegram_user_id=123456,
            google_credentials_file=Path("service-account.json"),
            spreadsheet_id="test-sheet-id",
        )
        context = SimpleNamespace(
            user_data={
                "pnl_type": "Расход",
                "pnl_category": "Кафе",
                "pnl_choices": ["Кофе"],
                "pnl_date": "11.10.2026",
                "pnl_amount": 250,
                "pnl_optional_note": True,
            },
            application=SimpleNamespace(bot_data={"settings": settings}),
        )

        result = asyncio.run(choose_subcategory(update, context))

        self.assertEqual(result, State.CONFIRM)
        self.assertEqual(context.user_data["pnl_note"], "")
        self.assertIn("Примечание: —", edit_message_text.await_args.args[0])
        callback_data = [
            button.callback_data
            for row in edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("note:add", callback_data)
        self.assertIn("save:yes", callback_data)

    def test_optional_comment_button_prompts_for_comment(self) -> None:
        answer = AsyncMock()
        edit_message_text = AsyncMock()
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                from_user=SimpleNamespace(id=123456),
                data="note:add",
                answer=answer,
                edit_message_text=edit_message_text,
            )
        )
        settings = Settings(
            telegram_token="123456:unit-test-token",
            allowed_telegram_user_id=123456,
            google_credentials_file=Path("service-account.json"),
            spreadsheet_id="test-sheet-id",
        )
        context = SimpleNamespace(
            user_data={},
            application=SimpleNamespace(bot_data={"settings": settings}),
        )

        result = asyncio.run(request_optional_note(update, context))

        self.assertEqual(result, State.NOTE_TEXT)
        answer.assert_awaited_once_with()
        edit_message_text.assert_awaited_once()
        self.assertEqual(
            edit_message_text.await_args.args[0],
            "Введите комментарий (до 1000 символов):",
        )

    def test_cancel_button_reports_when_no_entry_is_active(self) -> None:
        reply_text = AsyncMock()
        update = SimpleNamespace(
            callback_query=None,
            effective_message=SimpleNamespace(reply_text=reply_text)
        )
        context = SimpleNamespace(user_data={})

        result = asyncio.run(cancel_current_entry(update, context))

        self.assertEqual(result, ConversationHandler.END)
        reply_text.assert_awaited_once_with("Нет активной записи для отмены.")

    def test_cancel_button_clears_active_entry(self) -> None:
        reply_text = AsyncMock()
        update = SimpleNamespace(
            callback_query=None,
            effective_message=SimpleNamespace(reply_text=reply_text)
        )
        context = SimpleNamespace(user_data={"pnl_date": "10.10.2026"})

        result = asyncio.run(cancel_current_entry(update, context))

        self.assertEqual(result, ConversationHandler.END)
        self.assertEqual(context.user_data, {})
        reply_text.assert_awaited_once_with("Ввод отменён; таблица не изменена.")

    def test_version_command_reports_build_version_and_short_commit(self) -> None:
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply_text)
        )
        with patch.dict(
            "os.environ",
            {"APP_VERSION": "v1.0.0", "GIT_COMMIT": "abcdef0123456789"},
            clear=True,
        ):
            asyncio.run(show_version(update, SimpleNamespace()))

        reply_text.assert_awaited_once_with("Версия: v1.0.0\nКоммит: abcdef0")

    def test_recent_button_returns_numbered_rows(self) -> None:
        reply_text = AsyncMock()
        row = ["10.10.2026", "Кафе", "Кофе", "250", "Расход", "с коллегой"]
        store = SimpleNamespace(get_recent_transactions=lambda: [row])
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply_text)
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"sheets_store": store})
        )

        asyncio.run(show_recent_transactions(update, context))

        reply_text.assert_awaited_once_with(
            "Последние операции (от более старых к новым):\n\n"
            "1. Дата: 10.10.2026\n"
            "   Тип: Расход\n"
            "   Категория: Кафе\n"
            "   Подкатегория: Кофе\n"
            "   Сумма: 250 ₽\n"
            "   Примечание: с коллегой"
        )

    def test_conversation_registers_with_configured_user_allowlist(self) -> None:
        settings = Settings(
            telegram_token="123456:unit-test-token",
            allowed_telegram_user_id=123456,
            google_credentials_file=Path("service-account.json"),
            spreadsheet_id="test-sheet-id",
        )
        handler = build_conversation(settings)
        self.assertIsInstance(handler, ConversationHandler)
        self.assertTrue(handler.per_user)
        self.assertTrue(handler.per_chat)

    def test_callback_allowlist_rejects_other_telegram_users(self) -> None:
        answer = AsyncMock()
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                from_user=SimpleNamespace(id=999), answer=answer
            )
        )
        settings = Settings(
            telegram_token="123456:unit-test-token",
            allowed_telegram_user_id=123456,
            google_credentials_file=Path("service-account.json"),
            spreadsheet_id="test-sheet-id",
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"settings": settings})
        )

        rejected = asyncio.run(_reject_unauthorized_callback(update, context))

        self.assertTrue(rejected)
        answer.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
