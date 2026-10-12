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
    show_analytics,
    show_category_analytics,
    show_category_analytics_choices,
    State,
    build_conversation,
    choose_category,
    choose_subcategory,
    choose_type,
    receive_amount,
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
from finance_bot.charts import render_donut_chart
from finance_bot.analytics import (
    AnalyticsError,
    AnalyticsValue,
    CategoryAnalytics,
    MonthlyAnalytics,
    parse_category_analytics,
    parse_monthly_analytics,
)
from finance_bot.sheets import CategoryOption, GoogleSheetsStore, SheetsError, parse_taxonomy_rows


ANALYTICS_ROWS = [
    [
        "ТЕКУЩАЯ АНАЛИТИКА",
        "",
        "Месяц:",
        "10",
        "",
        "",
        "Подкатегории: еда офис",
        "",
        "",
        "",
        "Подкатегории: Кафе/Рестораны",
    ],
    [
        "",
        "",
        "Год:",
        "2026",
        "",
        "",
        "Подкатегория",
        "Сумма",
        "",
        "",
        "Подкатегория",
        "Сумма",
    ],
    ["Расходы за месяц", "р.27 457", "", "", "", "", "Столовая", "3381", "", "", "Кафе", "2706"],
    ["Доход за месяц", "р.160 516", "", "", "", "", "Самокат", "686", "", "", "Ресторан", "3070"],
    [
        "Остаток на текущий момент",
        "р.133 059",
        "",
        "",
        "",
        "",
        "Сладкое",
        "487",
        "",
        "",
        "Фастфуд",
        "1170",
    ],
    ["", "", "", "", "", "", "", "", "", "", "", ""],
    ["Траты по категориям (за месяц)"],
    ["Категория", "Сумма", "Процент от трат"],
    ["Еда офис", "р.4 554", "16,59%"],
    ["Кафе/Рестораны", "р.6 946", "25,30%"],
    ["Транспорт", "р.2 048", "7,46%"],
    ["Продукты", "р.4 112", "14,98%"],
    ["", "", "", "", "", "Подкатегории: продукты", "", "", "", "Подкатегории: Транспорт"],
    ["", "", "", "", "", "Подкатегория", "Сумма", "", "", "Подкатегория", "Сумма"],
    ["", "", "", "", "", "Магазин", "1900", "", "", "Штрафы", "762"],
    ["", "", "", "", "", "Доставка", "1402", "", "", "Бенз", "0"],
    ["", "", "", "", "", "Перекус", "810", "", "", "Общественный", "0"],
    ["", "", "", "", "", "Готовая еда", "0", "", "", "Платка", "0"],
    ["", "", "", "", "", "", "", "", "", "Самокаты", "170"],
    ["", "", "", "", "", "", "", "", "", "Такси", "1116"],
]


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


class AnalyticsTests(unittest.TestCase):
    def test_parses_monthly_summary_and_category_totals(self) -> None:
        analytics = parse_monthly_analytics(ANALYTICS_ROWS)

        self.assertEqual(analytics.month, 10)
        self.assertEqual(analytics.year, 2026)
        self.assertEqual(analytics.expenses, 27457)
        self.assertEqual(analytics.income, 160516)
        self.assertEqual(analytics.balance, 133059)
        self.assertEqual(
            analytics.categories,
            (
                AnalyticsValue("Еда офис", 4554),
                AnalyticsValue("Кафе/Рестораны", 6946),
                AnalyticsValue("Транспорт", 2048),
                AnalyticsValue("Продукты", 4112),
            ),
        )

    def test_parses_selected_category_subcategories_case_insensitively(self) -> None:
        analytics = parse_category_analytics(ANALYTICS_ROWS, "ЕДА ОФИС")

        self.assertEqual(analytics.category, "ЕДА ОФИС")
        self.assertEqual(analytics.month, 10)
        self.assertEqual(analytics.year, 2026)
        self.assertEqual(analytics.total, 4554)
        self.assertEqual(
            analytics.subcategories,
            (
                AnalyticsValue("Столовая", 3381),
                AnalyticsValue("Самокат", 686),
                AnalyticsValue("Сладкое", 487),
            ),
        )

    def test_parses_subcategories_for_all_requested_categories(self) -> None:
        expected = {
            "Еда офис": ("Столовая", "Самокат", "Сладкое"),
            "Кафе/Рестораны": ("Кафе", "Ресторан", "Фастфуд"),
            "Транспорт": ("Штрафы", "Бенз", "Общественный", "Платка", "Самокаты", "Такси"),
            "Продукты": ("Магазин", "Доставка", "Перекус", "Готовая еда"),
        }

        for category, subcategory_names in expected.items():
            with self.subTest(category=category):
                analytics = parse_category_analytics(ANALYTICS_ROWS, category)
                self.assertEqual(
                    tuple(value.label for value in analytics.subcategories),
                    subcategory_names,
                )

    def test_rejects_missing_or_invalid_analytics_values(self) -> None:
        with self.assertRaises(AnalyticsError):
            parse_monthly_analytics([])
        with self.assertRaises(AnalyticsError):
            parse_category_analytics(ANALYTICS_ROWS, "Неизвестная категория")

    def test_store_reads_configured_analytics_sheet(self) -> None:
        calls: list[str] = []

        class FakeWorksheet:
            def get_all_values(self) -> list[list[str]]:
                return ANALYTICS_ROWS

        class FakeSpreadsheet:
            def worksheet(self, name: str) -> FakeWorksheet:
                calls.append(name)
                return FakeWorksheet()

        store = GoogleSheetsStore(
            "unused.json",
            "unused-id",
            analytics_sheet="Моя аналитика",
        )
        store._spreadsheet = FakeSpreadsheet()

        self.assertEqual(store.get_monthly_analytics().month, 10)
        self.assertEqual(calls, ["Моя аналитика"])

    def test_renders_donut_chart_as_png(self) -> None:
        chart = render_donut_chart(
            "Траты по категориям",
            (AnalyticsValue("Еда офис", 4554), AnalyticsValue("Транспорт", 2048)),
        )

        self.assertEqual(chart.read(8), b"\x89PNG\r\n\x1a\n")

    def test_donut_chart_sorts_values_and_keeps_legend_inside_image(self) -> None:
        from matplotlib.backends.backend_agg import FigureCanvasAgg

        original_print_png = FigureCanvasAgg.print_png
        layout: dict[str, object] = {}

        def capture_chart_layout(canvas, image, *args, **kwargs) -> None:
            canvas.draw()
            legend = canvas.figure.legends[0]
            layout["labels"] = [text.get_text() for text in legend.get_texts()]
            layout["legend_right"] = legend.get_window_extent(
                canvas.get_renderer()
            ).x1
            layout["figure_right"] = canvas.figure.bbox.x1
            original_print_png(canvas, image, *args, **kwargs)

        values = (
            AnalyticsValue("Категория с длинным названием номер один", 1500),
            AnalyticsValue("Категория с длинным названием номер два", 4500),
            AnalyticsValue("Категория с длинным названием номер три", 3000),
        )
        with patch.object(FigureCanvasAgg, "print_png", capture_chart_layout):
            render_donut_chart("Траты по категориям", values)

        self.assertEqual(
            layout["labels"],
            [
                "Категория с длинным названием номер два: 4 500 ₽ (50,0%)",
                "Категория с длинным названием номер три: 3 000 ₽ (33,3%)",
                "Категория с длинным названием номер один: 1 500 ₽ (16,7%)",
            ],
        )
        self.assertLessEqual(layout["legend_right"], layout["figure_right"])


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
                ["Аналитика", "Аналитика по категориям"],
                ["Версия", "Отменить текущую запись"],
            ],
        )

    def test_monthly_analytics_sends_summary_and_chart(self) -> None:
        reply_text = AsyncMock()
        reply_photo = AsyncMock()
        analytics = MonthlyAnalytics(
            10,
            2026,
            27457,
            160516,
            133059,
            (
                AnalyticsValue("Еда офис", 4554),
                AnalyticsValue("Кафе/Рестораны", 6946),
            ),
        )
        store = SimpleNamespace(get_monthly_analytics=lambda: analytics)
        update = SimpleNamespace(
            effective_message=SimpleNamespace(
                reply_text=reply_text,
                reply_photo=reply_photo,
            )
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"sheets_store": store})
        )

        with patch("finance_bot.bot.render_donut_chart", return_value=b"chart") as chart:
            asyncio.run(show_analytics(update, context))

        reply_text.assert_awaited_once_with(
            "Аналитика за октябрь 2026:\n"
            "Расходы: 27 457 ₽\n"
            "Доход: 160 516 ₽\n"
            "Остаток на текущий момент: 133 059 ₽\n\n"
            "Траты по категориям:\n"
            "• Еда офис: 4 554 ₽\n"
            "• Кафе/Рестораны: 6 946 ₽"
        )
        reply_photo.assert_awaited_once_with(photo=b"chart")
        chart.assert_called_once_with(
            "Траты по категориям · октябрь 2026",
            analytics.categories,
        )

    def test_category_analytics_offers_requested_categories(self) -> None:
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply_text)
        )

        asyncio.run(show_category_analytics_choices(update, SimpleNamespace()))

        keyboard = reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(
            [
                (button.text, button.callback_data)
                for row in keyboard.inline_keyboard
                for button in row
            ],
            [
                ("Еда офис", "analytics:category:0"),
                ("Кафе/Рестораны", "analytics:category:1"),
                ("Транспорт", "analytics:category:2"),
                ("Продукты", "analytics:category:3"),
            ],
        )

    def test_category_analytics_returns_values_and_chart(self) -> None:
        answer = AsyncMock()
        edit_message_text = AsyncMock()
        reply_photo = AsyncMock()
        settings = Settings(
            telegram_token="123456:unit-test-token",
            allowed_telegram_user_id=123456,
            google_credentials_file=Path("service-account.json"),
            spreadsheet_id="test-sheet-id",
        )
        category = CategoryAnalytics(
            "Еда офис",
            10,
            2026,
            4554,
            (AnalyticsValue("Столовая", 3381), AnalyticsValue("Самокат", 686)),
        )
        store = SimpleNamespace(get_category_analytics=lambda _value: category)
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                from_user=SimpleNamespace(id=123456),
                data="analytics:category:0",
                answer=answer,
                edit_message_text=edit_message_text,
            ),
            effective_message=SimpleNamespace(reply_photo=reply_photo),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"settings": settings, "sheets_store": store}
            )
        )

        with patch("finance_bot.bot.render_donut_chart", return_value=b"chart") as chart:
            asyncio.run(show_category_analytics(update, context))

        answer.assert_awaited_once_with()
        edit_message_text.assert_awaited_once_with(
            "Аналитика по категории «Еда офис» за октябрь 2026:\n"
            "Всего: 4 554 ₽\n"
            "Подкатегории:\n"
            "• Столовая: 3 381 ₽\n"
            "• Самокат: 686 ₽"
        )
        reply_photo.assert_awaited_once_with(photo=b"chart")
        chart.assert_called_once_with(
            "Распределение · Еда офис · октябрь 2026",
            category.subcategories,
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
            self.assertTrue(context.user_data["pnl_optional_note"])

        self.assertEqual(reply_text.await_count, 3)
        self.assertIn("Введите дату", reply_text.await_args_list[-1].args[0])

    def test_subcategory_selection_proceeds_to_amount_for_preset_date(self) -> None:
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
                "pnl_optional_note": True,
            },
            application=SimpleNamespace(bot_data={"settings": settings}),
        )

        result = asyncio.run(choose_subcategory(update, context))

        self.assertEqual(result, State.AMOUNT)
        self.assertEqual(context.user_data["pnl_subcategory"], "Кофе")
        self.assertIn("Введите сумму", edit_message_text.await_args.args[0])

    def test_single_expense_subcategory_is_selected_automatically(self) -> None:
        answer = AsyncMock()
        edit_message_text = AsyncMock()
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                from_user=SimpleNamespace(id=123456),
                data="cat:0",
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
                "pnl_choices": ["REST"],
                "pnl_options": [
                    {"type": "Расход", "category": "REST", "subcategory": "REST"}
                ],
            },
            application=SimpleNamespace(bot_data={"settings": settings}),
        )

        result = asyncio.run(choose_category(update, context))

        self.assertEqual(result, State.AMOUNT)
        self.assertEqual(context.user_data["pnl_category"], "REST")
        self.assertEqual(context.user_data["pnl_subcategory"], "REST")
        self.assertIn("Введите сумму", edit_message_text.await_args.args[0])
        self.assertIsNone(edit_message_text.await_args.kwargs.get("reply_markup"))

    def test_multiple_expense_subcategories_still_require_selection(self) -> None:
        answer = AsyncMock()
        edit_message_text = AsyncMock()
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                from_user=SimpleNamespace(id=123456),
                data="cat:0",
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
                "pnl_choices": ["Кафе"],
                "pnl_options": [
                    {"type": "Расход", "category": "Кафе", "subcategory": "Кофе"},
                    {"type": "Расход", "category": "Кафе", "subcategory": "Обед"},
                ],
            },
            application=SimpleNamespace(bot_data={"settings": settings}),
        )

        result = asyncio.run(choose_category(update, context))

        self.assertEqual(result, State.SUBCATEGORY)
        self.assertEqual(context.user_data["pnl_choices"], ["Кофе", "Обед"])
        self.assertIn("Выберите подкатегорию", edit_message_text.await_args.args[0])
        self.assertIsNotNone(edit_message_text.await_args.kwargs["reply_markup"])

    def test_single_income_subcategory_is_selected_automatically(self) -> None:
        answer = AsyncMock()
        edit_message_text = AsyncMock()
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                from_user=SimpleNamespace(id=123456),
                data="type:income",
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
                "pnl_options": [
                    {"type": "Доход", "category": "Доход", "subcategory": "Зарплата"}
                ]
            },
            application=SimpleNamespace(bot_data={"settings": settings}),
        )

        result = asyncio.run(choose_type(update, context))

        self.assertEqual(result, State.AMOUNT)
        self.assertEqual(context.user_data["pnl_category"], "Доход")
        self.assertEqual(context.user_data["pnl_subcategory"], "Зарплата")
        self.assertIn("Введите сумму", edit_message_text.await_args.args[0])
        self.assertIsNone(edit_message_text.await_args.kwargs.get("reply_markup"))

    def test_amount_proceeds_directly_to_confirmation_with_optional_comment(self) -> None:
        reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(text="250", reply_text=reply_text)
        )
        context = SimpleNamespace(
            user_data={
                "pnl_date": "11.10.2026",
                "pnl_type": "Расход",
                "pnl_category": "Кафе",
                "pnl_subcategory": "Кофе",
                "pnl_optional_note": True,
            }
        )

        result = asyncio.run(receive_amount(update, context))

        self.assertEqual(result, State.CONFIRM)
        self.assertEqual(context.user_data["pnl_amount"], 250)
        self.assertEqual(context.user_data["pnl_note"], "")
        self.assertIn("Примечание: —", reply_text.await_args.args[0])
        callback_data = [
            button.callback_data
            for row in reply_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("note:add", callback_data)
        self.assertIn("save:yes", callback_data)
        confirmation_labels = [
            button.text
            for row in reply_text.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
            if button.callback_data in {"save:yes", "save:no"}
        ]
        self.assertEqual(
            confirmation_labels,
            ["✅ Подтвердить", "❌ Отменить"],
        )

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
