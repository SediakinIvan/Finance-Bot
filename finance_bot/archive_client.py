"""Signed calls to the IFT Apps Script archive; never calculates financial totals."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx

IFT_ARCHIVE_SHEET = '1jaEzmJPqYzxSp-HtDw-zwt_kPqSEs4enBrkG-Lc-UzA'
TERMINAL = {'complete', 'kept', 'cancelled', 'stale'}


class ArchiveError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def validate_archive_config(url, key, spreadsheet):
    if spreadsheet != IFT_ARCHIVE_SHEET or not re.fullmatch(r'[A-Za-z0-9_-]{32,}', key) or not re.fullmatch(r'https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec', url):
        raise ValueError('Archive requires an IFT workbook, /exec endpoint and private key of at least 32 characters')


class ArchiveClient:
    def __init__(self, *, url, key, owner, spreadsheet, directory, transport=None):
        validate_archive_config(url, key, spreadsheet)
        self.url, self.key, self.owner, self.spreadsheet = url, key, int(owner), spreadsheet
        self.directory = Path(directory) / 'month-archive'
        self.path = self.directory / (str(self.owner) + '-' + hashlib.sha256(spreadsheet.encode()).hexdigest()[:16] + '.json')
        self.transport = transport
        self.lock = asyncio.Lock()

    def _save(self, request_id):
        UUID(request_id)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.directory, prefix='.request-')
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump({'request_id': request_id, 'owner': self.owner, 'scope': self.spreadsheet}, output)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _load(self):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text())
            if data['owner'] != self.owner or data['scope'] != self.spreadsheet:
                raise ValueError('scope')
            UUID(data['request_id'])
            return data['request_id']
        except (ValueError, KeyError, TypeError):
            raise ArchiveError('journal_invalid') from None

    async def _call(self, action, request_id):
        UUID(request_id)
        payload = json.dumps({'action': action, 'request_id': request_id, 'owner_id': self.owner,
            'spreadsheet_id': self.spreadsheet, 'timestamp': int(time.time() * 1000), 'nonce': str(uuid4())},
            ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        envelope = {'payload': payload, 'signature': hmac.new(self.key.encode(), payload.encode(), hashlib.sha256).hexdigest()}
        try:
            async with httpx.AsyncClient(timeout=25, follow_redirects=False, transport=self.transport) as http:
                response = await http.post(self.url, json=envelope)
                if response.status_code in {302, 303}:
                    destination = response.headers.get('location', '')
                    parsed = urlparse(destination)
                    if parsed.scheme != 'https' or parsed.hostname != 'script.googleusercontent.com' or parsed.username or parsed.password:
                        raise ArchiveError('unsafe_redirect')
                    # ContentService returns a one-time GET URL. Never forward the signed POST body.
                    response = await http.get(destination)
                response.raise_for_status()
                result = response.json()
        except ArchiveError:
            raise
        except (httpx.HTTPError, ValueError):
            raise ArchiveError('result_unknown') from None
        if not isinstance(result, dict) or result.get('protocol') != 1 or not isinstance(result.get('ok'), bool):
            raise ArchiveError('protocol_mismatch')
        if not result['ok']:
            if result.get('code') == 'request_in_progress':
                try:
                    UUID(result['request_id'])
                except (ValueError, KeyError, TypeError):
                    raise ArchiveError('protocol_mismatch') from None
                return result
            raise ArchiveError(result.get('code', 'endpoint_error') if result.get('code') in {'unauthorized', 'invalid_source', 'busy'} else 'endpoint_error')
        if result.get('request_id') != request_id or result.get('status') not in {
            'not_found', 'preview', 'archived', 'archiving', 'clearing', 'advancing', 'complete_pending',
            'complete', 'kept', 'cancelled', 'stale', 'recovery_required'}:
            raise ArchiveError('protocol_mismatch')
        return result

    async def begin(self):
        async with self.lock:
            previous = self._load()
            if previous:
                state = await self._call('status', previous)
                if state['status'] not in TERMINAL | {'not_found'}:
                    return state
            request_id = str(uuid4())
            self._save(request_id)  # Save before sending, including a preview with a lost answer.
            state = await self._call('preview', request_id)
            if state.get('code') == 'request_in_progress':
                self._save(state['request_id'])
                return await self._call('status', state['request_id'])
            return state

    async def action(self, action, request_id):
        if action not in {'archive', 'clear', 'cancel', 'status'}:
            raise ArchiveError('invalid_action')
        async with self.lock:
            try:
                return await self._call(action, request_id)
            except ArchiveError as error:
                if error.code != 'result_unknown' or action in {'status', 'cancel'}:
                    raise
                # An ambiguous mutation is never retried. Only query its durable status.
                return await self._call('status', request_id)
