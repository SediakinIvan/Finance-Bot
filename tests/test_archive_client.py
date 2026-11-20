import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import httpx
from finance_bot.archive_client import (
    ArchiveClient,
    ArchiveError,
    IFT_ARCHIVE_SHEET,
    PRODUCTION_ARCHIVE_SHEET,
    validate_archive_config,
)

URL='https://script.google.com/macros/s/test-deployment/exec'
KEY='offline-test-key-'*4


class ArchiveClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)

    def client(self, handler):
        return ArchiveClient(url=URL,key=KEY,owner=1,spreadsheet=IFT_ARCHIVE_SHEET,directory=Path(self.temp.name),transport=httpx.MockTransport(handler))

    async def test_exact_signature_and_durable_preview_after_restart(self):
        seen=[]
        def handler(request):
            data=json.loads(request.content);payload=json.loads(data['payload']);seen.append(payload)
            self.assertEqual(hmac.new(KEY.encode(),data['payload'].encode(),hashlib.sha256).hexdigest(),data['signature'])
            self.assertEqual(payload['spreadsheet_id'],IFT_ARCHIVE_SHEET)
            return httpx.Response(200,json={'protocol':1,'ok':True,'status':'preview','request_id':payload['request_id']})
        first=self.client(handler);state=await first.begin()
        self.assertEqual(first.path.stat().st_mode & 0o777,0o600)
        second=self.client(handler);again=await second.begin()
        self.assertEqual(again['request_id'],state['request_id'])
        self.assertEqual([p['action'] for p in seen],['preview','status'])

    async def test_ambiguous_clear_is_never_replayed_only_status_is_requested(self):
        actions=[];rid=str(uuid4())
        def handler(request):
            p=json.loads(json.loads(request.content)['payload']);actions.append(p['action'])
            if p['action']=='clear':raise httpx.ReadTimeout('private payload must not escape')
            return httpx.Response(200,json={'protocol':1,'ok':True,'status':'complete','request_id':rid})
        self.assertEqual((await self.client(handler).action('clear',rid))['status'],'complete')
        self.assertEqual(actions,['clear','status'])

    async def test_content_service_redirect_is_get_without_secret_or_signed_body(self):
        rid=str(uuid4());seen=[]
        def handler(request):
            seen.append(request)
            if request.method=='POST':return httpx.Response(302,headers={'location':'https://script.googleusercontent.com/macros/echo?one-time=token'})
            self.assertEqual(request.content,b'');self.assertNotIn('signature',str(request.url))
            return httpx.Response(200,json={'protocol':1,'ok':True,'status':'complete','request_id':rid})
        await self.client(handler).action('status',rid)
        self.assertEqual([r.method for r in seen],['POST','GET'])

    async def test_foreign_redirect_invalid_protocol_and_foreign_request_are_rejected(self):
        for response in [httpx.Response(303,headers={'location':'https://evil.example/'}),
                         httpx.Response(200,json={'ok':True}),
                         httpx.Response(200,json={'protocol':1,'ok':True,'status':'complete','request_id':str(uuid4())})]:
            calls=[]
            def handler(request):calls.append(request);return response
            with self.assertRaises(ArchiveError):await self.client(handler).action('status',str(uuid4()))
            self.assertEqual(len(calls),1)

    async def test_lost_local_file_reconnects_to_remote_active_request(self):
        active=str(uuid4())
        def handler(request):
            p=json.loads(json.loads(request.content)['payload'])
            if p['action']=='preview':return httpx.Response(200,json={'protocol':1,'ok':False,'code':'request_in_progress','request_id':active})
            self.assertEqual(p['request_id'],active)
            return httpx.Response(200,json={'protocol':1,'ok':True,'status':'archived','request_id':active})
        client=self.client(handler)
        self.assertEqual((await client.begin())['status'],'archived');self.assertEqual(client._load(),active)

    def test_production_and_unsafe_or_unconfigured_endpoints_are_refused(self):
        validate_archive_config(URL, KEY, PRODUCTION_ARCHIVE_SHEET)
        for url,key,book in [(URL,KEY,'unknown-book'),('https://script.google.com/dev',KEY,IFT_ARCHIVE_SHEET),(URL,'short',IFT_ARCHIVE_SHEET)]:
            with self.assertRaises(ValueError):validate_archive_config(url,key,book)

    def test_enabled_settings_are_ift_only_and_do_not_expose_key_in_repr(self):
        from unittest.mock import patch
        from finance_bot.config import Settings, ConfigurationError
        credentials=Path(self.temp.name)/'key.json';credentials.write_text('{}')
        env={'TELEGRAM_BOT_TOKEN':'1:offline','TELEGRAM_ALLOWED_USER_ID':'1',
             'GOOGLE_CREDENTIALS_FILE':str(credentials),'GOOGLE_SHEET_ID':IFT_ARCHIVE_SHEET,
             'MONTH_ARCHIVE_ENABLED':'true','MONTH_ARCHIVE_URL':URL,'MONTH_ARCHIVE_KEY':KEY}
        with patch.dict('os.environ',env,clear=True):
            settings=Settings.from_environment();self.assertTrue(settings.month_archive_enabled);self.assertNotIn(KEY,repr(settings))
        with patch.dict('os.environ',{**env,'GOOGLE_SHEET_ID':'production'},clear=True):
            with self.assertRaises(ConfigurationError):Settings.from_environment()
