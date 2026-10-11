from __future__ import annotations

import asyncio
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import ConversationHandler

from finance_bot.bot import (
    _parse_amount,
    _parse_date,
    _reject_unauthorized_callback,
    build_conversation,
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


class AppendOnlyStoreTests(unittest.TestCase):
    def test_append_writes_only_six_values_to_transactions_tab(self) -> None:
        calls: list[tuple[str, list[object], dict[str, object]]] = []

        class FakeWorksheet:
            def append_row(self, values: list[object], **kwargs: object) -> None:
                calls.append(("append", values, kwargs))

        class FakeSpreadsheet:
            def worksheet(self, name: str) -> FakeWorksheet:
                calls.append(("worksheet", [name], {}))
                return FakeWorksheet()

        store = GoogleSheetsStore("unused.json", "unused-id")
        store._spreadsheet = FakeSpreadsheet()
        row = ["10.10.2026", "Доход", "Зарплата", 1000, "Доход", ""]
        store.append_transaction(row)

        self.assertEqual(calls[0], ("worksheet", ["Траты"], {}))
        self.assertEqual(calls[1][0:2], ("append", row))
        self.assertEqual(
            calls[1][2],
            {
                "value_input_option": "RAW",
                "insert_data_option": "INSERT_ROWS",
                "table_range": "A:F",
            },
        )

    def test_rejects_non_six_column_row_without_touching_sheet(self) -> None:
        store = GoogleSheetsStore("unused.json", "unused-id")
        with self.assertRaises(ValueError):
            store.append_transaction(["too", "short"])


class TelegramHandlerTests(unittest.TestCase):
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
