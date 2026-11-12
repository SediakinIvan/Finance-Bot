"""Provision only the isolated test env; token arrives on stdin, never argv/logs."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import tempfile

TEST_SHEET = '1jaEzmJPqYzxSp-HtDw-zwt_kPqSEs4enBrkG-Lc-UzA'


def read_setting(path, key):
    for line in path.read_text().splitlines():
        name, separator, value = line.partition('=')
        if separator and name.strip() == key:
            parts = shlex.split(value, comments=True)
            return parts[0] if len(parts) == 1 else ''
    return ''


def prepare(production, destination, token):
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise ValueError('Invalid test token format')
    if token.split(':', 1)[0] == read_setting(production, 'TELEGRAM_BOT_TOKEN').split(':', 1)[0]:
        raise ValueError('Test bot must differ from the production bot')
    owner = read_setting(production, 'TELEGRAM_ALLOWED_USER_ID')
    if not owner.isdigit() or int(owner) <= 0:
        raise ValueError('Missing primary owner in production configuration')
    if destination.resolve() == production.resolve():
        raise ValueError('Refusing to overwrite production configuration')
    content = '\n'.join((f'TELEGRAM_BOT_TOKEN={token}', f'TELEGRAM_ALLOWED_USER_ID={owner}',
        'TELEGRAM_USER_SHEETS=', f'GOOGLE_SHEET_ID={TEST_SHEET}',
        'GOOGLE_TRANSACTIONS_SHEET=Траты', 'GOOGLE_CATEGORIES_SHEET=Категории',
        'GOOGLE_ANALYTICS_SHEET=Аналитика', 'BOT_TIMEZONE=Europe/Moscow', ''))
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent, prefix='.test-env-')
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(content)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--production-env', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.production_env, args.output, json.load(__import__('sys').stdin)['token'])
    except Exception as exc:
        raise SystemExit('Test configuration failed: ' + type(exc).__name__) from None
    print('Isolated test configuration prepared.')
