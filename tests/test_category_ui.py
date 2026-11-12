import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from finance_bot import category_ui as ui
from finance_bot.bot import cancel_flow, _set_subcategory
from finance_bot.category_management import CategoryPreview
from finance_bot.taxonomy import Category, Subcategory, Taxonomy, Period, new_id
from finance_bot.sheets import SheetsError


class CategoryUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.parents = tuple(Category(new_id(), 'Расход', f'Категория {i}') for i in range(18))
        self.income = Category(new_id(), 'Доход', 'Доход')
        self.inactive = Category(new_id(), 'Расход', 'Старая', False, Period(2026, 11))
        self.children = tuple(Subcategory(new_id(), self.parents[0].category_id, f'Подкатегория {i}') for i in range(10))
        self.registry = Taxonomy('test', 1, self.parents + (self.income, self.inactive), self.children)
        self.store = NS(category_catalog=Mock(return_value=self.registry), category_change_status=Mock(return_value=None),
            preview_category_change=Mock(return_value=CategoryPreview(new_id(), 'Предпросмотр')),
            confirm_category_change=Mock(return_value=CategoryPreview(new_id(), 'Готово', 'complete')),
            cancel_category_change=Mock())
        self.context = NS(user_data={}, application=NS(bot_data={'settings': NS(authorized_telegram_user_ids={1}), 'sheets_stores': {1:self.store}}))
        self.update = self.make_update()

    def make_update(self, data=None, user=1, chat='private', text=''):
        query = None if data is None else NS(data=data, from_user=NS(id=user), answer=AsyncMock(), edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock())
        return NS(effective_user=NS(id=user), effective_chat=NS(type=chat), callback_query=query,
                  effective_message=NS(text=text, reply_text=AsyncMock()))

    async def start(self):
        result = await ui.start_categories(self.update, self.context)
        self.keyboard = self.update.effective_message.reply_text.await_args.kwargs['reply_markup']
        return result

    async def click(self, action):
        update = self.make_update('tax:' + self.context.user_data[ui.FLOW_KEY]['token'] + ':' + action)
        state = await ui.handle_category_callback(update, self.context)
        if update.callback_query.edit_message_text.await_args:
            self.keyboard = update.callback_query.edit_message_text.await_args.kwargs.get('reply_markup')
        return state, update

    async def text(self, text):
        update = self.make_update(text=text)
        state = await ui.receive_category_text(update, self.context)
        return state, update

    def actions(self):
        return [button.callback_data.split(':',2)[2] for row in self.keyboard.inline_keyboard for button in row]

    async def test_all_pages_use_ids_and_filter_inactive(self):
        await self.start()
        seen=[]
        for page in range(3):
            if page: await self.click(f'page:root:{page}')
            seen += [a[2:] for a in self.actions() if a.startswith('c:')]
            self.assertLessEqual(sum(a.startswith('c:') for a in self.actions()),8)
            self.assertTrue(all(len(b.callback_data.encode())<=64 for row in self.keyboard.inline_keyboard for b in row))
        self.assertEqual(seen,[p.category_id for p in self.parents]+[self.income.category_id])
        await self.click('filter')
        self.assertIn('c:'+self.inactive.category_id,self.actions())
        await self.click('c:'+self.inactive.category_id)
        self.assertIn('rename',self.actions());self.assertNotIn('delete',self.actions())

    async def test_create_preview_cancel_and_double_confirmation(self):
        await self.start();await self.click('new');await self.click('type:expense')
        state,_=await self.text('  Новая   категория ')
        self.assertEqual(state,ui.TEXT)
        state,_=await self.text('Первая\nВторая')
        self.assertEqual(state,ui.CONFIRM)
        self.store.preview_category_change.assert_called_once_with(1,action='create',transaction_type='Расход',name='Новая категория',subcategories=('Первая','Вторая'))
        self.store.confirm_category_change.assert_not_called()
        old='tax:'+self.context.user_data[ui.FLOW_KEY]['token']+':confirm'
        await self.click('confirm')
        await ui.handle_category_callback(self.make_update(old),self.context)
        self.store.confirm_category_change.assert_called_once()
        await self.click('root');await self.click('new');await self.click('type:income')
        await self.text('Доход новый');await self.text('Зарплата');await self.click('cancel')
        self.store.cancel_category_change.assert_called_once()
        self.assertNotIn(ui.FLOW_KEY,self.context.user_data)

    async def test_invalid_text_does_not_prepare_or_apply(self):
        await self.start();await self.click('new');await self.click('type:expense')
        for value in (' ', 'x'*65):
            self.assertEqual((await self.text(value))[0],ui.TEXT)
        await self.text('Новая')
        for value in ('a\n A','\n'.join(str(i) for i in range(21)), 'a\n\nb'):
            self.assertEqual((await self.text(value))[0],ui.TEXT)
        self.store.preview_category_change.assert_not_called()
        self.store.confirm_category_change.assert_not_called()

    async def test_child_add_rename_move_and_deactivate(self):
        await self.start();await self.click('c:'+self.parents[0].category_id);await self.click('add');await self.text('Добавка')
        self.store.preview_category_change.assert_called_with(1,action='create',parent_id=self.parents[0].category_id,name='Добавка')
        await self.click('cancel');await self.start();await self.click('c:'+self.parents[0].category_id)
        await self.click('s:'+self.children[0].subcategory_id);await self.click('rename');await self.text('Новое имя')
        self.store.preview_category_change.assert_called_with(1,action='rename',entity_id=self.children[0].subcategory_id,name='Новое имя')
        await self.click('cancel');await self.start();await self.click('c:'+self.parents[0].category_id)
        await self.click('s:'+self.children[0].subcategory_id);await self.click('move')
        targets=[a for a in self.actions() if a.startswith('target:')]
        self.assertEqual(len(targets),8)
        self.assertNotIn('target:'+self.parents[0].category_id,targets)
        self.assertNotIn('target:'+self.income.category_id,targets)
        self.assertNotIn('target:'+self.inactive.category_id,targets)
        await self.click(targets[0])
        self.store.preview_category_change.assert_called_with(1,action='move',entity_id=self.children[0].subcategory_id,parent_id=targets[0].split(':')[1])
        await self.click('cancel');await self.start();await self.click('c:'+self.parents[0].category_id);await self.click('delete')
        self.store.preview_category_change.assert_called_with(1,action='deactivate',entity_id=self.parents[0].category_id)

    async def test_auth_private_scope_and_foreign_stale_tokens(self):
        for user,chat in ((2,'private'),(1,'group')):
            await ui.start_categories(self.make_update(user=user,chat=chat),self.context)
        self.store.category_catalog.assert_not_called()
        await self.start()
        for user,token in ((2,self.context.user_data[ui.FLOW_KEY]['token']),(1,'oldtoken')):
            await ui.handle_category_callback(self.make_update('tax:'+token+':confirm',user=user),self.context)
        self.store.confirm_category_change.assert_not_called()
        self.context.user_data['pnl_amount']=100
        await ui.reject_stale_category_callback(self.make_update('tax:old:confirm'),self.context)
        self.assertEqual(self.context.user_data['pnl_amount'],100)

    async def test_pending_requires_confirmation_and_false_success_is_rejected(self):
        pending=CategoryPreview(new_id(),'rename','applying')
        self.store.category_change_status.return_value=pending
        self.assertEqual(await self.start(),ui.CONFIRM)
        self.store.confirm_category_change.assert_not_called()
        self.store.confirm_category_change.return_value=pending
        _,update=await self.click('resume')
        self.store.confirm_category_change.assert_called_once_with(1,pending.request_id,resume=True)
        self.assertNotEqual(update.callback_query.edit_message_text.await_args.args[0],'Изменение выполнено.')
        self.store.confirm_category_change.side_effect=SheetsError('Журнал повреждён')
        await self.start();_,update=await self.click('resume')
        self.assertIn('Журнал повреждён',update.callback_query.edit_message_text.await_args.args[0])

    async def test_enter_clears_financial_flow_and_cancel_invalidates_preview(self):
        self.context.user_data.update(pnl_amount=100,pnl_transaction_id=new_id())
        await self.start()
        self.assertNotIn('pnl_amount',self.context.user_data)
        await self.click('c:'+self.parents[0].category_id);await self.click('delete')
        await cancel_flow(self.make_update(),self.context)
        self.store.cancel_category_change.assert_called_once()
        self.assertFalse(self.context.user_data)

    async def test_same_income_child_name_selects_explicit_parent(self):
        a,b=new_id(),new_id()
        self.context.user_data.update(pnl_type='Доход',pnl_category='Вторая',pnl_options=[
            {'type':'Доход','category':'Первая','subcategory':'Общая','category_id':a,'subcategory_id':new_id(),'revision':1},
            {'type':'Доход','category':'Вторая','subcategory':'Общая','category_id':b,'subcategory_id':new_id(),'revision':1}])
        _set_subcategory(self.context,'Общая')
        self.assertEqual(self.context.user_data['pnl_category_id'],b)

    async def test_switch_to_expense_cancels_preview_and_stale_tax_button_keeps_save(self):
        from finance_bot.bot import start_add,State
        from finance_bot.sheets import CategoryOption
        await self.start();await self.click('c:'+self.parents[0].category_id);await self.click('delete')
        old='tax:'+self.context.user_data[ui.FLOW_KEY]['token']+':confirm'
        self.store.load_taxonomy=Mock(return_value=[CategoryOption('Расход','Категория','Подкатегория')])
        state=await start_add(self.make_update(),self.context)
        self.assertEqual(state,State.CUSTOM_DATE)
        self.store.cancel_category_change.assert_called_once()
        self.assertNotIn(ui.FLOW_KEY,self.context.user_data)
        self.context.user_data['pnl_transaction_id']='pending-financial-save'
        self.assertIsNone(await ui.reject_stale_category_callback(self.make_update(old),self.context))
        self.assertEqual(self.context.user_data['pnl_transaction_id'],'pending-financial-save')
        self.store.confirm_category_change.assert_not_called()

    async def test_preview_error_keeps_draft_and_close_preserves_error_message(self):
        await self.start();await self.click('new');await self.click('type:expense');await self.text('Одежда')
        self.store.preview_category_change.side_effect=SheetsError('Отсутствует архивный лист')
        state,update=await self.text('Белье\nВерхняя одежда\nОдежда\nАксессуары')
        self.assertEqual(state,ui.TEXT)
        self.assertIn('Отсутствует архивный лист',update.effective_message.reply_text.await_args.args[0])
        self.assertEqual(self.context.user_data[ui.FLOW_KEY]['draft']['name'],'Одежда')
        self.assertEqual(self.context.user_data[ui.FLOW_KEY]['phase'],'new_children')
        _,closed=await self.click('cancel')
        closed.callback_query.edit_message_text.assert_not_awaited()
        closed.callback_query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        self.assertIn('Таблица не изменена',closed.effective_message.reply_text.await_args.args[0])
        self.store.confirm_category_change.assert_not_called()

    async def test_close_after_success_preserves_success_and_does_not_claim_cancel(self):
        await self.start();await self.click('new');await self.click('type:expense');await self.text('Одежда');await self.text('Одежда')
        await self.click('confirm')
        self.assertEqual(self.context.user_data[ui.FLOW_KEY]['phase'],'complete')
        _,closed=await self.click('cancel')
        closed.callback_query.edit_message_text.assert_not_awaited()
        self.assertNotIn('не изменена',closed.effective_message.reply_text.await_args.args[0])
        self.store.confirm_category_change.assert_called_once()

    async def test_pending_close_does_not_claim_rollback(self):
        self.store.category_change_status.return_value=CategoryPreview(new_id(),'rename','applying')
        await self.start();_,closed=await self.click('cancel')
        self.assertIn('не откатывает',closed.effective_message.reply_text.await_args.args[0])
        self.store.cancel_category_change.assert_not_called()
        closed.callback_query.edit_message_text.assert_not_awaited()

    async def test_rollback_has_separate_confirmation_and_stale_buttons_do_not_apply(self):
        request=new_id();self.store.category_change_status.return_value=CategoryPreview(request,'pending','applying')
        self.store.preview_category_rollback=Mock(return_value=CategoryPreview(request,'Подтвердить откат?','rollback_preview'))
        self.store.rollback_category_change=Mock(return_value=CategoryPreview(request,'Отменено','rolled_back'))
        await self.start();self.assertIn('rollback',self.actions())
        await self.click('rollback')
        self.store.rollback_category_change.assert_not_called()
        self.assertIn('confirm_rollback',self.actions())
        old='tax:'+self.context.user_data[ui.FLOW_KEY]['token']+':confirm_rollback'
        await self.click('confirm_rollback')
        await ui.handle_category_callback(self.make_update(old),self.context)
        self.store.rollback_category_change.assert_called_once_with(1,request,confirmed=True,resume=False)
        self.store.confirm_category_change.assert_not_called()

    async def test_declining_rollback_and_restarting_rolling_back_are_distinct(self):
        request=new_id();self.store.category_change_status.return_value=CategoryPreview(request,'pending','applying')
        self.store.preview_category_rollback=Mock(return_value=CategoryPreview(request,'rollback?','rollback_preview'))
        self.store.rollback_category_change=Mock(return_value=CategoryPreview(request,'Отменено','rolled_back'))
        await self.start();await self.click('rollback');await self.click('cancel')
        self.store.rollback_category_change.assert_not_called();self.store.cancel_category_change.assert_not_called()
        self.store.category_change_status.return_value=CategoryPreview(request,'pending','rolling_back')
        await self.start();self.assertNotIn('resume',self.actions());self.assertIn('resume_rollback',self.actions())
        await self.click('resume_rollback')
        self.store.rollback_category_change.assert_called_once_with(1,request,confirmed=True,resume=True)

    async def test_conflict_and_unsupported_journal_actions(self):
        self.store.category_change_status.return_value=CategoryPreview(new_id(),'conflict','conflict')
        await self.start();self.assertNotIn('resume',self.actions());self.assertIn('rollback',self.actions())
        self.store.category_change_status.return_value=CategoryPreview(new_id(),'operator','operator_required')
        await self.start();self.assertNotIn('resume',self.actions());self.assertNotIn('rollback',self.actions())

    async def test_inactive_category_and_child_have_confirmed_reactivation(self):
        await self.start();await self.click('filter');await self.click('c:'+self.inactive.category_id)
        self.assertIn('reactivate',self.actions())
        await self.click('reactivate')
        self.store.preview_category_change.assert_called_with(1,action='reactivate',entity_id=self.inactive.category_id)
        self.store.confirm_category_change.assert_not_called()
        await self.click('cancel');self.store.confirm_category_change.assert_not_called()
        from dataclasses import replace
        child=replace(self.children[0],active=False)
        self.store.category_catalog.return_value=Taxonomy('test',1,self.registry.categories,(child,))
        await self.start();await self.click('c:'+self.parents[0].category_id);await self.click('childfilter')
        await self.click('s:'+child.subcategory_id)
        self.assertIn('reactivate',self.actions());await self.click('reactivate');await self.click('confirm')
        self.store.preview_category_change.assert_called_with(1,action='reactivate',entity_id=child.subcategory_id)
        self.store.confirm_category_change.assert_called_once()
