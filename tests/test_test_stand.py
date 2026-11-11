from pathlib import Path
import tempfile
import unittest
from scripts.prepare_test_env import prepare, TEST_SHEET


class TestStandProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.production=self.root/'.env'
        self.original='TELEGRAM_BOT_TOKEN=111:production\nTELEGRAM_ALLOWED_USER_ID=42\nGOOGLE_SHEET_ID=production-book\nTELEGRAM_USER_SHEETS=77:other-book\n'
        self.production.write_text(self.original)
        self.target=self.root/'test'/'.env.test'

    def test_independent_token_table_owner_and_private_file(self):
        prepare(self.production,self.target,'222:test_token')
        values=dict(line.split('=',1) for line in self.target.read_text().splitlines())
        self.assertEqual(values['TELEGRAM_BOT_TOKEN'],'222:test_token')
        self.assertEqual(values['GOOGLE_SHEET_ID'],TEST_SHEET)
        self.assertEqual(values['TELEGRAM_ALLOWED_USER_ID'],'42')
        self.assertEqual(values['TELEGRAM_USER_SHEETS'],'')
        self.assertEqual(self.target.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.production.read_text(),self.original)

    def test_reject_same_bot_even_if_token_rotated_and_never_overwrite_production(self):
        for token,target in [('111:rotated',self.target),('invalid',self.target),('222:test',self.production)]:
            with self.subTest(token=token):
                with self.assertRaises(ValueError):prepare(self.production,target,token)
        self.assertEqual(self.production.read_text(),self.original)
        self.assertFalse(self.target.exists())
