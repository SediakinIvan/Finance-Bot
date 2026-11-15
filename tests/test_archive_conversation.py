from unittest.mock import AsyncMock
from telegram import Update
from telegram.ext import ConversationHandler
from finance_bot.bot import register_handlers, State
from test_category_conversation import CategoryConversationTests, USER, CHAT

RID='10000000-0000-4000-8000-000000000001'


class ArchiveConversationTests(CategoryConversationTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        s=self.app.bot_data['settings'];s.month_archive_enabled=True;s.allowed_telegram_user_id=1
        self.client=AsyncMock()
        self.client.begin.return_value={'protocol':1,'ok':True,'request_id':RID,'status':'preview','name':'Ноябрь 2026'}
        def act(action,rid):
            state={'archive':'archived','clear':'complete','cancel':'kept','status':'complete'}[action]
            return {'protocol':1,'ok':True,'request_id':rid,'status':state,'name':'Ноябрь 2026','url':'https://docs.google.com/spreadsheets/d/test/edit'}
        self.client.action.side_effect=act;self.app.bot_data['month_archive_client']=self.client
        self.app.handlers.clear();register_handlers(self.app,s)
        self.conv=next(h for handlers in self.app.handlers.values() for h in handlers if isinstance(h,ConversationHandler))

    async def message(self,text):
        if not text.startswith('/'):
            return await super().message(text)
        self.counter+=1
        message={'message_id':self.counter,'date':1,'chat':CHAT,'from':USER,'text':text,
                 'entities':[{'type':'bot_command','offset':0,'length':len(text.split()[0])}]}
        await self.app.process_update(Update.de_json({'update_id':self.counter,'message':message},self.app.bot))
        self.assertEqual(self.errors,[])

    async def test_two_explicit_confirmations_and_status_callback(self):
        await self.message('Архивация месяца')
        self.client.action.assert_not_awaited()
        await self.callback(label='Сохранить архив')
        self.assertEqual(self.client.action.await_args.args,('archive',RID))
        await self.callback(label='Очистить траты и перейти к следующему месяцу')
        self.assertEqual(self.client.action.await_args.args,('clear',RID))
        self.assertIn('период переключён',self.api.messages[self.api.last_id]['text'])
        await self.callback(data=f'arc:status:{RID}')
        self.assertEqual(self.client.action.await_args.args,('status',RID))

    async def test_replace_archive_is_explicit_and_keep_does_not_send_clear(self):
        self.client.begin.return_value.update(replace=True)
        await self.message('/archive_month')
        await self.callback(label='Заменить архив')
        await self.callback(label='Оставить траты')
        self.assertEqual([c.args[0] for c in self.client.action.await_args_list],['archive','cancel'])
        self.assertIn('траты не очищены',self.api.messages[self.api.last_id]['text'])

    async def test_help_keyboard_and_entry_interrupt_transaction_input(self):
        await self.message('/help')
        keyboard=self.api.messages[self.api.last_id]['reply_markup']
        self.assertEqual(keyboard['keyboard'][-1],[{'text':'Архивация месяца'}])
        self.assertFalse(keyboard['is_persistent'])
        self.app.user_data[1]['pnl_note']='draft'
        self.conv._conversations[(1,1)]=State.NOTE_TEXT
        await self.message('Архивация месяца')
        self.assertNotIn('pnl_note',self.app.user_data[1]);self.assertNotIn((1,1),self.conv._conversations)
        self.client.begin.assert_awaited_once()

    async def test_foreign_and_group_callbacks_do_not_reach_client(self):
        await self.message('Архивация месяца');self.client.action.reset_mock()
        for user,chat in [(dict(USER,id=2),CHAT),(USER,dict(CHAT,type='group'))]:
            message=dict(self.api.messages[self.api.last_id],chat=chat)
            update=Update.de_json({'update_id':1000,'callback_query':{'id':'x','from':user,'chat_instance':'x','message':message,'data':f'arc:clear:{RID}'}},self.app.bot)
            await self.app.process_update(update)
        self.client.action.assert_not_awaited();self.assertEqual(self.errors,[])

    async def test_disabled_configuration_has_no_button_and_does_not_start(self):
        s=self.app.bot_data['settings'];s.month_archive_enabled=False
        self.app.handlers.clear();register_handlers(self.app,s)
        await self.message('/help');keyboard=self.api.messages[self.api.last_id]['reply_markup']['keyboard']
        self.assertFalse(any('Архивация месяца'==b['text'] for row in keyboard for b in row))
        await self.message('/archive_month');self.client.begin.assert_not_awaited()
