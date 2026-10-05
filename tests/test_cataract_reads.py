"""Local switching stays bounded, current and isolated without hospital requests."""
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vghks_sdk.models import BinaryAsset

from vghks_bot.bot import BotApplication
from vghks_bot.selftest_bot import BotSyntheticSDK
from vghks_bot.settings import Settings


def seed_patients(work, count=9, order_count=3):
    mrns = [f'TEST{i:03}' for i in range(1, count + 1)]
    analysis, store = work.analysis, work.analysis.store
    cohort = analysis.save_cohort({'source': 'manual', 'account_id': work.account_id,
                                   'mrns': ' '.join(mrns), 'name': 'Synthetic switching'})
    for index, mrn in enumerate(mrns, 1):
        record = {'id': 'soap-' + mrn, 'mrn': mrn, 'name': '合成病人' + str(index),
                  'date': '2026-09-11', 'section': '眼科', 'section_code': '70',
                  'case_no': 'CASE' + str(index), 'soap': 'A+P：合成 SOAP ' + str(index),
                  'soap_structure': {'assessment_plan': '合成 SOAP ' + str(index),
                                     'objective': '合成檢查 ' + str(index), 'subjective': '合成主訴 ' + str(index)}}
        store.library.save_record(record, work.username, 'synthetic')
        store.save_step(mrn, 'cataract-soap', 'cataract_soap',
                        {'status': 'ready', 'record_id': record['id']}, work.username)
        store.save_step(mrn, 'numeric-history', 'numeric', {'mrn': mrn, 'tables': [
            {'title': 'Va', 'headers': ['日期', 'OD', 'OS'],
             'rows': [['2026-09-11', str(index / 10), str(index / 10 + .1)]]}
        ]}, work.username)
        store.save_step(mrn, 'visits', 'visits', [
            {'mrn': mrn, 'case_no': f'CASE{number}', 'case_type': 'O',
             'visit_date': f'2026-09-{number + 1:02}', 'section_code': '70', 'section_name': '眼科'}
            for number in range(order_count)], work.username)
        asset = store.save_asset(mrn, 'synthetic-asset',
                                 BinaryAsset(b'%PDF-1.4\n' + mrn.encode(), 'application/pdf'), work.username)
        orders = []
        for number in range(order_count):
            order = {'mrn': mrn, 'case_no': f'CASE{number}', 'case_type': 'O', 'name': 'DBR, free charge',
                     'order_date': f'2026-09-{number + 1:02}', 'execution_date': f'2026-09-{number + 1:02}'}
            identity = work.review.history._order_key(order)
            store.save_step(mrn, 'collected-order:' + identity, 'order_report',
                            {'name': order['name'], 'status': 'complete', 'assets': [asset]}, work.username)
            orders.append(order)
        store.save_step(mrn, 'orders-history:*', 'order_index', orders, work.username)
        store.save_step(mrn, 'orders-history:OR', 'order_index', orders, work.username)
    return cohort


class CataractReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = BotApplication(Settings(), Path(self.temp.name), BotSyntheticSDK)
        self.account = self.app.login({'username': 'TEST', 'password': 'synthetic'})['account']['id']
        self.work = self.app.workspace(self.account)
        self.analysis = self.work.analysis
        self.cohort = seed_patients(self.work)
        self.values = {'cohort_id': self.cohort['id'], 'mrn': 'TEST001', 'module': 'cataract'}
        self.calls = list(BotSyntheticSDK.calls)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def status(self):
        return self.analysis.cataract_status(self.values)

    def test_nine_patient_status_uses_two_connections_and_no_soap_versions(self):
        with patch('vghks_bot.library.sqlite3.connect', wraps=sqlite3.connect) as connections, \
                patch.object(self.work.store.library, 'get_record', side_effect=AssertionError('summary loaded SOAP')):
            value = self.status()
        self.assertTrue(value['ready'])
        self.assertEqual(len(value['members']), 9)
        self.assertLessEqual(connections.call_count, 2)
        self.assertTrue(all(row['orders_complete'] == 3 for row in value['members']))
        self.assertEqual(BotSyntheticSDK.calls, self.calls)

    def test_results_use_one_connection_and_same_revision_as_summary(self):
        with patch('vghks_bot.library.sqlite3.connect', wraps=sqlite3.connect) as connections, \
                patch.object(self.work.store.library, 'get_record', wraps=self.work.store.library.get_record) as record:
            result = self.analysis.results(self.values)
        self.assertEqual(connections.call_count, 1)
        record.assert_called_once_with('soap-TEST001', include_versions=False)
        self.assertEqual(result['data_revision'], self.status()['members'][0]['data_revision'])
        self.assertEqual(result['latest_soap']['record']['mrn'], 'TEST001')
        self.assertEqual(BotSyntheticSDK.calls, self.calls)

    def test_revision_changes_for_numeric_and_soap_only_on_affected_patient(self):
        before = self.status()['members']
        self.analysis.store.save_step('TEST001', 'numeric-history', 'numeric',
                                      {'mrn': 'TEST001', 'tables': []}, self.work.username)
        numeric = self.status()['members']
        self.assertNotEqual(before[0]['data_revision'], numeric[0]['data_revision'])
        self.assertEqual(before[1]['data_revision'], numeric[1]['data_revision'])
        record = self.work.store.library.get_record('soap-TEST001')
        record['soap'] = 'A+P：updated synthetic SOAP'
        record.pop('updated_at')
        self.work.store.library.save_record(record, self.work.username, 'updated-synthetic')
        after = self.status()['members']
        self.assertNotEqual(numeric[0]['data_revision'], after[0]['data_revision'])
        self.assertEqual(before[1]['data_revision'], after[1]['data_revision'])

    def test_missing_attachment_invalidates_completion_and_revision(self):
        before = self.status()['members'][0]
        asset = self.analysis.store.step('TEST001', 'synthetic-asset')['payload']
        self.analysis.store.asset(asset['digest'])[0].unlink()
        after = self.status()['members'][0]
        self.assertFalse(after['ready'])
        self.assertEqual(after['orders_complete'], 0)
        self.assertNotEqual(before['data_revision'], after['data_revision'])
        self.assertFalse(self.analysis.results(self.values)['orders'][0]['attachments'][0]['available'])

    def test_deleted_and_wrong_patient_soap_cannot_be_ready(self):
        self.analysis.store.save_step('TEST001', 'cataract-soap', 'cataract_soap',
                                      {'status': 'ready', 'record_id': 'soap-TEST002'}, self.work.username)
        self.assertFalse(self.status()['members'][0]['ready'])
        self.assertIsNone(self.analysis.results(self.values)['latest_soap']['record'])
        self.analysis.store.save_step('TEST001', 'cataract-soap', 'cataract_soap',
                                      {'status': 'ready', 'record_id': 'soap-TEST001'}, self.work.username)
        with self.work.store.library.connect() as db:
            db.execute('INSERT INTO pending_deletions VALUES(?)', ('soap-TEST001',))
        self.assertFalse(self.status()['members'][0]['ready'])
        self.assertIsNone(self.analysis.results(self.values)['latest_soap']['record'])

    def test_offline_readonly_results_and_account_isolation(self):
        self.app.logout(self.account)
        self.app.read_only = True
        self.assertTrue(self.status()['ready'])
        self.assertEqual(len(self.analysis.results(self.values)['numeric']), 1)
        self.assertEqual(BotSyntheticSDK.calls, self.calls)
        self.app.read_only = False
        other = self.app.login({'username': 'SECOND', 'password': 'synthetic'})['account']['id']
        with self.assertRaises(ValueError):
            self.app.workspace(other).analysis.results(self.values)

    def test_local_batch_rolls_back_on_failure_and_leaves_no_shared_connection(self):
        library = self.work.store.library
        with self.assertRaisesRegex(RuntimeError, 'synthetic abort'):
            with library.batch():
                self.analysis.store.save_step('TEST001', 'batch-test', 'coverage', {}, self.work.username)
                with library.batch():
                    self.assertIsNotNone(self.analysis.store.step('TEST001', 'batch-test'))
                raise RuntimeError('synthetic abort')
        self.assertIsNone(self.analysis.store.step('TEST001', 'batch-test'))
        self.assertTrue(self.status()['ready'])

    def test_client_cache_coalescing_and_stale_context_responses(self):
        root = Path(__file__).resolve().parents[1]
        subprocess.run(['node', 'tests/cataract_read_ui.js'], cwd=root, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
