import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from finance_bot.bot import _set_subcategory, confirm_save, show_category_analytics, show_category_analytics_choices
from finance_bot.analytics import AnalyticsValue, CategoryAnalytics
from finance_bot.taxonomy import new_id
from finance_bot.v2_sheets import TaxonomyChangedError


class V2HandlerTests(unittest.TestCase):
    def context(self, store):
        return SimpleNamespace(user_data={}, application=SimpleNamespace(bot_data={
            "settings": SimpleNamespace(authorized_telegram_user_ids={1}),
            "sheets_stores": {1: store},
        }))

    def update(self, callback=None):
        return SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(type="private"),
            callback_query=callback, effective_message=SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock()))

    def test_selected_ids_and_revision_reach_confirmed_write(self):
        store = SimpleNamespace(append_transaction=Mock())
        context = self.context(store)
        parent, child = new_id(), new_id()
        context.user_data.update(pnl_type="Расход", pnl_date="11.11.2026", pnl_category="Новая", pnl_amount=100,
            pnl_options=[{"type": "Расход", "category": "Новая", "subcategory": "Первая", "category_id": parent, "subcategory_id": child, "revision": 4}])
        _set_subcategory(context, "Первая")
        transaction_id = context.user_data["pnl_transaction_id"]
        query = SimpleNamespace(from_user=SimpleNamespace(id=1), data="save:yes", answer=AsyncMock(), edit_message_reply_markup=AsyncMock(), edit_message_text=AsyncMock(), message=SimpleNamespace(reply_text=AsyncMock()))
        asyncio.run(confirm_save(self.update(query), context))
        store.append_transaction.assert_called_once_with(["11.11.2026", "Новая", "Первая", 100, "Расход", ""], category_id=parent, subcategory_id=child, expected_revision=4, transaction_id=transaction_id)
        self.assertNotIn("pnl_transaction_id", context.user_data)

    def test_stale_registry_does_not_retry_or_claim_success(self):
        store = SimpleNamespace(append_transaction=Mock(side_effect=TaxonomyChangedError("Справочник изменился")))
        context = self.context(store)
        context.user_data.update(pnl_date="11.11.2026", pnl_category="Новая", pnl_subcategory="Первая", pnl_amount=100, pnl_type="Расход", pnl_category_id=new_id(), pnl_subcategory_id=new_id(), pnl_revision=1, pnl_transaction_id=new_id())
        query = SimpleNamespace(from_user=SimpleNamespace(id=1), data="save:yes", answer=AsyncMock(), edit_message_reply_markup=AsyncMock(), edit_message_text=AsyncMock(), message=SimpleNamespace(reply_text=AsyncMock()))
        asyncio.run(confirm_save(self.update(query), context))
        store.append_transaction.assert_called_once()
        query.edit_message_text.assert_not_awaited()
        query.message.reply_text.assert_awaited_once_with("Справочник изменился")
        self.assertFalse(context.user_data)

    def test_prewrite_rejection_does_not_claim_uncertain_write(self):
        from finance_bot.sheets import TransactionNotWrittenError
        store=SimpleNamespace(append_transaction=Mock(side_effect=TransactionNotWrittenError("Финансовая запись не отправлена. HTTP 429.")))
        context=self.context(store)
        context.user_data.update(pnl_date="11.11.2026",pnl_category="Новая",pnl_subcategory="Первая",pnl_amount=100,pnl_type="Расход")
        query=SimpleNamespace(from_user=SimpleNamespace(id=1),data="save:yes",answer=AsyncMock(),edit_message_reply_markup=AsyncMock(),message=SimpleNamespace(reply_text=AsyncMock()))
        asyncio.run(confirm_save(self.update(query),context))
        query.message.reply_text.assert_awaited_once_with("Финансовая запись не отправлена. HTTP 429.")
        store.append_transaction.assert_called_once()

    def test_category_pages_and_callbacks_use_ids_not_positions(self):
        choices = [(new_id(), f"Категория {number}") for number in range(12)]
        store = SimpleNamespace(get_analytics_categories=lambda: choices)
        context = self.context(store)
        update = self.update()
        asyncio.run(show_category_analytics_choices(update, context))
        keyboard = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(buttons[0].callback_data, "analytics:category:" + choices[0][0])
        self.assertEqual(buttons[-1].callback_data, "analytics:page:1")
        self.assertTrue(all(len(button.callback_data.encode()) <= 64 for button in buttons))
        query = SimpleNamespace(from_user=SimpleNamespace(id=1), data="analytics:page:1", answer=AsyncMock(), edit_message_text=AsyncMock())
        asyncio.run(show_category_analytics_choices(self.update(query), context))
        keyboard = query.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "analytics:category:" + choices[8][0])

    def test_old_category_button_does_not_follow_reordered_positions(self):
        selected = new_id()
        store = SimpleNamespace(get_category_analytics=Mock(return_value=CategoryAnalytics("Категория", 11, 2026, 100, (AnalyticsValue("Подкатегория", 100),))))
        context = self.context(store)
        query = SimpleNamespace(from_user=SimpleNamespace(id=1), data="analytics:category:" + selected, answer=AsyncMock(), edit_message_text=AsyncMock())
        with patch("finance_bot.bot.render_donut_chart", return_value=b"chart"):
            asyncio.run(show_category_analytics(self.update(query), context))
        store.get_category_analytics.assert_called_once_with(selected)


if __name__ == "__main__":
    unittest.main()
