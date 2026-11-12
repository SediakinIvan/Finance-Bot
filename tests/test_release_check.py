import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from finance_bot.release_check import main, failure_reason
from finance_bot.category_schema import SchemaError


class ReleaseCheckTests(unittest.TestCase):
    def test_schema_cause_is_reported_without_outer_response(self):
        error = RuntimeError('secret-token-or-api-body')
        error.__cause__ = SchemaError('Workbook is not ready; finish migration or recovery.')
        self.assertEqual(failure_reason(error), str(error.__cause__))
        self.assertNotIn('secret',failure_reason(RuntimeError('secret')))

    def test_missing_journal_fails_before_google_read(self):
        settings = SimpleNamespace(category_change_state_dir=Path('/missing-financebot-test-path'))
        with patch('finance_bot.release_check.Settings.from_environment',return_value=settings), patch('finance_bot.release_check.V2GoogleSheetsStore') as store, patch('builtins.print') as output:
            with self.assertRaises(SystemExit):main()
            store.assert_not_called()
            self.assertIn('persistent journal',output.call_args.args[0])

    def test_each_configured_workbook_is_read_without_polling(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=SimpleNamespace(category_change_state_dir=Path(directory),allowed_telegram_user_id=1,authorized_telegram_user_ids={1,2},spreadsheet_id_for_user=lambda user:f'book-{user}',google_credentials_file=Path('key'),transactions_sheet='Траты',categories_sheet='Категории',analytics_sheet='Аналитика')
            with patch('finance_bot.release_check.Settings.from_environment',return_value=settings), patch('finance_bot.release_check.V2GoogleSheetsStore') as store, patch('builtins.print'):
                store.return_value.category_change_status.return_value=None
                main()
                self.assertEqual(store.return_value.category_catalog.call_count,2)
                self.assertEqual([c.kwargs['spreadsheet_id'] for c in store.call_args_list],['book-1','book-2'])

    def test_pending_release_requires_supported_journal_without_reading_partial_registry(self):
        for supported in (True,False):
            with tempfile.TemporaryDirectory() as directory:
                settings=SimpleNamespace(category_change_state_dir=Path(directory),allowed_telegram_user_id=1,authorized_telegram_user_ids={1},spreadsheet_id_for_user=lambda _: 'copy-id',google_credentials_file=Path('key'),transactions_sheet='Траты',categories_sheet='Категории',analytics_sheet='Аналитика')
                with patch('finance_bot.release_check.Settings.from_environment',return_value=settings),patch('finance_bot.release_check.V2GoogleSheetsStore') as factory,patch('builtins.print'):
                    store=factory.return_value
                    store.category_change_status.return_value=SimpleNamespace(status='rolling_back')
                    store.category_recovery_ready.return_value=supported
                    if supported:main()
                    else:
                        with self.assertRaises(SystemExit):main()
                    store.category_catalog.assert_not_called();store.load_taxonomy.assert_not_called()
                    store.category_recovery_ready.assert_called_once_with(1)
