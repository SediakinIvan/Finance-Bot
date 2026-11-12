"""Exercise the real PTB dispatcher with an in-memory Bot API, no network."""
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from telegram import Update
from telegram.ext import Application, ConversationHandler
from telegram.request import BaseRequest
from finance_bot import category_ui as ui
from finance_bot.analytics import MonthlyAnalytics, AnalyticsValue
from finance_bot.bot import register_handlers
from finance_bot.category_management import CategoryPreview
from finance_bot.sheets import SheetsError
from finance_bot.taxonomy import Category, Subcategory, Taxonomy, new_id

USER={'id':1,'is_bot':False,'first_name':'Tester'}
CHAT={'id':1,'type':'private'}
BOT={'id':999,'is_bot':True,'first_name':'IFT','username':'ift_test_bot'}


class MemoryBotAPI(BaseRequest):
    def __init__(self):self.messages={};self.next_id=100;self.last_id=None
    @property
    def read_timeout(self):return 5
    async def initialize(self):pass
    async def shutdown(self):pass
    async def do_request(self,url,method,request_data=None,**kwargs):
        endpoint=url.rsplit('/',1)[-1]
        params=request_data.parameters if request_data else {}
        if endpoint=='getMe':result=BOT
        elif endpoint=='answerCallbackQuery':result=True
        elif endpoint in {'sendMessage','sendPhoto','editMessageText','editMessageReplyMarkup'}:
            if endpoint.startswith('edit'):
                key=params['message_id'];result=self.messages[key]
            else:
                key=self.next_id;self.next_id+=1
                result={'message_id':key,'date':1,'chat':CHAT,'from':BOT}
                self.messages[key]=result
            if 'text' in params:result['text']=params['text']
            if endpoint=='editMessageReplyMarkup' or 'reply_markup' in params:
                markup=params.get('reply_markup')
                if isinstance(markup,str):markup=json.loads(markup)
                if markup:result['reply_markup']=markup
                else:result.pop('reply_markup',None)
            self.last_id=key
            result=dict(result)
        else:raise AssertionError('Unexpected Bot API call: '+endpoint)
        return 200,json.dumps({'ok':True,'result':result}).encode()


class CategoryConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api=MemoryBotAPI();self.app=Application.builder().token('999:offline').request(self.api).build()
        parent=Category(new_id(),'Расход','Продукты')
        self.store=NS(category_catalog=Mock(return_value=Taxonomy('copy',0,(parent,),(Subcategory(new_id(),parent.category_id,'Магазин'),))),
            category_change_status=Mock(return_value=None),preview_category_change=Mock(side_effect=lambda owner,**draft:CategoryPreview('saved-request','Создать: '+draft['name']+'\n'+'\n'.join(draft.get('subcategories',())))),
            confirm_category_change=Mock(return_value=CategoryPreview('saved-request','complete','complete')),cancel_category_change=Mock(),
            get_monthly_analytics=Mock(return_value=MonthlyAnalytics(11,2026,10,100,90,(AnalyticsValue('Продукты',10),))))
        settings=NS(authorized_telegram_user_ids={1},timezone='Europe/Moscow')
        self.app.bot_data.update(settings=settings,sheets_stores={1:self.store})
        register_handlers(self.app,settings)
        self.conv=next(h for handlers in self.app.handlers.values() for h in handlers if isinstance(h,ConversationHandler))
        self.errors=[]
        async def error(update,context):self.errors.append(context.error)
        self.app.add_error_handler(error)
        await self.app.initialize();self.counter=0
    async def asyncTearDown(self):await self.app.shutdown()

    async def message(self,text):
        self.counter+=1
        update=Update.de_json({'update_id':self.counter,'message':{'message_id':self.counter,'date':1,'chat':CHAT,'from':USER,'text':text}},self.app.bot)
        await self.app.process_update(update)
        self.assertEqual(self.errors,[])

    async def callback(self,data=None,label=None,message_id=None):
        key=message_id or self.api.last_id
        if data is None:
            buttons=[b for row in self.api.messages[key]['reply_markup']['inline_keyboard'] for b in row]
            data=next(b['callback_data'] for b in buttons if b['text']==label)
        self.counter+=1
        update=Update.de_json({'update_id':self.counter,'callback_query':{'id':str(self.counter),'from':USER,'chat_instance':'offline','message':dict(self.api.messages[key]),'data':data}},self.app.bot)
        await self.app.process_update(update)
        self.assertEqual(self.errors,[])
        return data

    async def draft(self,children):
        await self.message('Категории');await self.callback(label='Создать категорию');await self.callback(label='Расход')
        await self.message('Одежда');await self.message(children)

    async def test_real_dispatcher_single_and_multiline_create_preview_confirm_close(self):
        for children in ('Одежда','Белье\nВерхняя одежда\nОдежда\nАксессуары'):
            with self.subTest(children=children):
                self.store.confirm_category_change.reset_mock()
                await self.draft(children)
                self.assertEqual(self.conv._conversations[(1,1)],ui.CONFIRM)
                self.assertEqual(self.app.user_data[1][ui.FLOW_KEY]['phase'],'preview')
                self.assertIn(children,self.api.messages[self.api.last_id]['text'])
                self.store.confirm_category_change.assert_not_called()
                key=self.api.last_id
                old=await self.callback(label='Подтвердить')
                self.store.confirm_category_change.assert_called_once_with(1,'saved-request',resume=False)
                await self.callback(data=old,message_id=key)
                self.store.confirm_category_change.assert_called_once()
                await self.callback(label='Закрыть',message_id=key)
                self.assertEqual(self.api.messages[key]['text'],'Изменение выполнено.')
                self.assertNotIn((1,1),self.conv._conversations)

    async def test_error_retry_and_explicit_cancel_preserve_message(self):
        self.store.preview_category_change.side_effect=SheetsError('Нет архивного листа')
        await self.draft('Белье')
        self.assertEqual(self.conv._conversations[(1,1)],ui.TEXT)
        failed=self.api.last_id
        self.assertIn('Нет архивного листа',self.api.messages[failed]['text'])
        self.store.preview_category_change.side_effect=lambda owner,**draft:CategoryPreview('retry','Предпросмотр')
        await self.message('Аксессуары')
        self.assertEqual(self.conv._conversations[(1,1)],ui.CONFIRM)
        preview=self.api.last_id
        await self.callback(label='Отмена')
        self.assertEqual(self.api.messages[preview]['text'],'Предпросмотр')
        self.assertIn('Нет архивного листа',self.api.messages[failed]['text'])
        self.store.cancel_category_change.assert_called_once_with(1,'retry')
        self.store.confirm_category_change.assert_not_called()

    async def test_analytics_switch_invalidates_draft_without_applying(self):
        await self.message('Категории');await self.callback(label='Создать категорию');await self.callback(label='Расход');await self.message('Одежда')
        with patch('finance_bot.bot.render_donut_chart',return_value=b'chart'):
            await self.message('Аналитика')
        self.store.get_monthly_analytics.assert_called_once()
        self.assertNotIn(ui.FLOW_KEY,self.app.user_data[1])
        await self.message('Белье')
        self.store.preview_category_change.assert_not_called()
        self.store.confirm_category_change.assert_not_called()
