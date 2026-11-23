"""Cross-module integration through the real staff -> assignment -> publish chain.

Covers replay of the original D1 double-list export, v2 reassignment, late
revocation, duplicate calls and post-restart compensation — asserting that
field tasks are never duplicated and acknowledgement history is never erased.
"""
import json
import unittest
from tests.publication_support import (
    PublicationHarness, POINTER_MODE, FIXED_MODE,
    FAULT_AFTER_WORKERS, FAULT_BEFORE_COMMIT,
)
from pollinate.store import connect
from pollinate.publish import recover_publish, sync_receipts


class CrossModuleIntegrationTests(unittest.TestCase):
    def _assert_d1_double_list(self, h):
        # The original normal D1 export remains directly replayable.
        result=h.publish('D1', worker='I1')
        self.assertEqual(result['assignments'],
                         [{'task': 'J1', 'worker': 'I1',
                           'start': 1, 'end': 3}])
        if h.mode == FIXED_MODE:
            self.assertIn('D1,I1,J1',
                          (h.export/'workers.csv').read_text('utf8'))
            self.assertIn('D1,J1,I1,1,3',
                          (h.export/'orchard_jobs.csv').read_text('utf8'))
        else:
            view=h.read_driver()
            self.assertIn('D1,I1,J1', view['workers_csv'])
            self.assertEqual(view['version'], 1)

    def test_d1_replay_pointer_mode(self):
        with PublicationHarness(mode=POINTER_MODE) as h:
            self._assert_d1_double_list(h)

    def test_d1_replay_fixed_mode(self):
        with PublicationHarness(mode=FIXED_MODE) as h:
            self._assert_d1_double_list(h)

    def test_duplicate_import_is_idempotent_and_keeps_one_task(self):
        for mode in (POINTER_MODE, FIXED_MODE):
            with self.subTest(mode=mode), PublicationHarness(mode=mode) as h:
                first=h.publish('D1', worker='I1')
                again=h.import_day(h.write_day('D1', worker='I1'))
                self.assertEqual(again['assignments'], first['assignments'])
                self.assertEqual(len(h.roster_day_ids()), 1)
                rows=h.publish_rows()
                # Identical content reuses the active version — no v2 minted.
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['state'], 'active')

    def test_conflicting_reimport_is_rejected(self):
        with PublicationHarness() as h:
            h.publish('D1', worker='I1')
            path=h.write_day('D1', worker='I9')
            with self.assertRaises(ValueError):
                h.import_day(path)

    def test_v2_reassignment_then_restart_compensates_once(self):
        for mode in (POINTER_MODE, FIXED_MODE):
            with self.subTest(mode=mode), PublicationHarness(mode=mode) as h:
                h.publish('D1', worker='I1')
                with self.assertRaises(RuntimeError):
                    h.publish('D2', worker='I2', fault=FAULT_AFTER_WORKERS)
                # Restart #1: the durable journal rebuilds the publication
                # layer even though the business rows rolled back.
                s1=h.restart_and_recover()
                self.assertEqual(s1['rebuilt'], [2])
                # Replaying the same D2 fixture restores its business rows and
                # dedupes against the journal instead of minting a new version.
                h.import_day(h.write_day('D2', worker='I2'))
                if mode == FIXED_MODE:
                    # Fixed route still waits for field receipts before work.
                    self.assertTrue(h.suspended()['suspended'])
                    h.ack_both(2)
                    self.assertTrue(h.try_resume())
                # Restart #2: nothing is rebuilt or duplicated.
                s2=h.restart_and_recover()
                self.assertEqual(s2['rebuilt'], [])
                db=h.open_db()
                try:
                    roster=[r['day_id'] for r in db.execute(
                        'SELECT day_id FROM field_roster ORDER BY day_id')]
                finally:
                    db.close()
                self.assertEqual(roster, ['D1', 'D2'])
                # Exactly one assignment row per task — no duplicated field work.
                self.assertEqual(
                    len(json.loads(h.publish_rows()[-1]['assignments_json'])), 1)

    def test_receipts_survive_database_rebuild_and_are_reconciled(self):
        with PublicationHarness(mode=POINTER_MODE) as h:
            h.publish('D1', worker='I1')
            h.ack_both(1)
            # Wipe the SQLite-side receipt table to simulate loss/rebuild.
            db=h.open_db()
            try:
                db.execute('DELETE FROM roster_receipt')
                db.commit()
                self.assertEqual(sync_receipts(db, str(h.export)), 2)
                rows=[r['party'] for r in db.execute(
                    'SELECT party FROM roster_receipt ORDER BY party')]
            finally:
                db.close()
            self.assertEqual(rows, ['driver', 'gate'])
            self.assertEqual(len(h.disk_receipts()), 2)

    def test_restart_with_no_journal_is_a_clean_noop(self):
        with PublicationHarness() as h:
            summary=h.restart_and_recover()
            self.assertEqual(summary, {
                'rebuilt': [], 'pointer_advanced': None, 'resumed': False,
                'suspended_version': None, 'receipts_synced': 0})

    def test_full_chain_versions_and_hashes_agree_across_stores(self):
        with PublicationHarness(mode=POINTER_MODE) as h:
            h.publish('D1', worker='I1')
            h.publish('D2', worker='I2')
            rows=h.publish_rows()
            self.assertEqual([r['version'] for r in rows], [1, 2])
            driver=h.read_driver()
            gate=h.read_gate()
            self.assertEqual(driver['content_hash'], rows[-1]['content_hash'])
            self.assertEqual(gate['content_hash'], rows[-1]['content_hash'])
            # Journal, DB and versioned payload carry the same hashes.
            journal=json.loads(
                (h.export/'.publish/journal/v2.json').read_text('utf8'))
            self.assertEqual(journal['content_hash'],
                             rows[-1]['content_hash'])
            manifest=json.loads(
                (h.export/'.publish/versions/v2/manifest.json')
                .read_text('utf8'))
            self.assertEqual(manifest['files']['workers.csv'],
                             rows[-1]['workers_hash'])
            self.assertEqual(manifest['files']['orchard_jobs.csv'],
                             rows[-1]['jobs_hash'])

    def test_failed_version_number_is_never_reused(self):
        with PublicationHarness() as h:
            h.publish('D1', worker='I1')
            with self.assertRaises(RuntimeError):
                h.publish('D2', worker='I2', fault=FAULT_BEFORE_COMMIT)
            # A later successful publication takes v3, not v2.
            h.publish('D3', worker='I3')
            self.assertEqual(h.versions_on_disk(), [1, 2, 3])
            self.assertEqual(
                [r['version'] for r in h.publish_rows()
                 if r['state'] == 'active'], [1, 3])

    def test_recovery_without_prior_import_rebuilds_from_journal(self):
        # The durable journal is enough for recovery even if the operator only
        # has the export directory and a fresh database file.
        with PublicationHarness(mode=POINTER_MODE) as h:
            h.publish('D1', worker='I1')
            with self.assertRaises(RuntimeError):
                h.publish('D2', worker='I2', fault=FAULT_AFTER_WORKERS)
            fresh_db=h.root/'fresh.db'
            db=connect(fresh_db)
            try:
                summary=recover_publish(db, str(h.export))
            finally:
                db.close()
            self.assertEqual(summary['rebuilt'], [1, 2])
            db=connect(fresh_db)
            try:
                versions=[r['version'] for r in db.execute(
                    'SELECT version FROM roster_publish ORDER BY version')]
            finally:
                db.close()
            self.assertEqual(versions, [1, 2])


if __name__ == '__main__':
    unittest.main()
