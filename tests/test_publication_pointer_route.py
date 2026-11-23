"""Route 1 — versioned delivery pointer for new-generation parties.

Every read must resolve to one complete version through current.json. A fault
at any point keeps both consumers on the previous complete version; recovery
advances the pointer once, atomically, to the new version.
"""
import unittest
from tests.publication_support import (
    PublicationHarness, POINTER_MODE,
    FAULT_AFTER_WORKERS, FAULT_AFTER_JOBS, FAULT_BEFORE_COMMIT,
    FAULT_ONE_RECEIPT,
)
from pollinate.publish import PublicationMissing, VersionRollbackError


class PointerRouteTests(unittest.TestCase):
    def setUp(self):
        self.h=PublicationHarness(mode=POINTER_MODE)

    def tearDown(self):
        self.h.cleanup()

    def test_v1_publishes_whole_version_to_both_parties(self):
        self.h.publish('D1', worker='I1')
        driver=self.h.read_driver()
        gate=self.h.read_gate()
        self.assertEqual(driver['version'], 1)
        self.assertEqual(gate['version'], 1)
        self.assertEqual(driver['content_hash'], gate['content_hash'])
        self.assertIn('D1,I1,J1', driver['workers_csv'])
        self.assertIn('D1,J1,I1,1,3', gate['jobs_csv'])
        # No fixed-name exports and no stop order in pointer mode.
        self.assertFalse((self.h.export/'STOP_WORK').exists())
        self.assertFalse((self.h.export/'workers.csv').exists())

    def _v1_then_faulty_v2(self, fault):
        self.h.publish('D1', worker='I1')
        with self.assertRaisesRegex(RuntimeError, 'simulated failure'):
            self.h.publish('D2', worker='I2', fault=fault)
        # Both readers keep observing the complete v1 throughout.
        self.assertEqual(self.h.read_driver()['version'], 1)
        self.assertEqual(self.h.read_gate()['version'], 1)
        self.assertIn('D1,I1,J1', self.h.read_driver()['workers_csv'])
        self.assertIn('D1,J1,I1', self.h.read_gate()['jobs_csv'])
        # No v2 roster row survived the rollback.
        self.assertNotIn('D2', self.h.roster_day_ids())

    def test_fault_after_workers_keeps_v1_visible(self):
        self._v1_then_faulty_v2(FAULT_AFTER_WORKERS)
        self.assertEqual(self.h.versions_on_disk(), [1, 2])

    def test_fault_after_jobs_keeps_v1_visible(self):
        self._v1_then_faulty_v2(FAULT_AFTER_JOBS)

    def test_fault_before_commit_keeps_v1_visible(self):
        self._v1_then_faulty_v2(FAULT_BEFORE_COMMIT)

    def test_restart_recovery_advances_pointer_to_v2_once(self):
        self.h.publish('D1', worker='I1')
        with self.assertRaises(RuntimeError):
            self.h.publish('D2', worker='I2', fault=FAULT_BEFORE_COMMIT)
        # Fresh process: only the durable journal knows about v2.
        summary=self.h.restart_and_recover()
        self.assertEqual(summary['rebuilt'], [2])
        self.assertEqual(summary['pointer_advanced'], 2)
        self.assertEqual(self.h.read_driver()['version'], 2)
        self.assertEqual(self.h.read_gate()['version'], 2)
        self.assertIn('D2,I2,J1', self.h.read_driver()['workers_csv'])
        self.assertIn('D2,J1,I2', self.h.read_gate()['jobs_csv'])
        # Recovery is idempotent: a second restart changes nothing.
        again=self.h.restart_and_recover()
        self.assertEqual(again['rebuilt'], [])
        self.assertIsNone(again['pointer_advanced'])
        self.assertEqual(self.h.read_driver()['version'], 2)

    def test_recovered_v2_then_reimport_completes_without_version_gap(self):
        self.h.publish('D1', worker='I1')
        with self.assertRaises(RuntimeError):
            self.h.publish('D2', worker='I2', fault=FAULT_AFTER_JOBS)
        self.h.restart_and_recover()
        # Re-running the same D2 fixture must not duplicate tasks or mint v3
        # for identical content.
        result=self.h.import_day(self.h.write_day('D2', worker='I2'))
        self.assertEqual(len(result['assignments']), 1)
        rows=self.h.publish_rows()
        self.assertEqual([r['version'] for r in rows], [1, 2])
        self.assertTrue(all(r['state'] == 'active' for r in rows))

    def test_one_receipt_fault_does_not_rewrite_observed_version(self):
        self.h.publish('D1', worker='I1')
        self.h.publish('D2', worker='I2')
        # Driver observes v2 and acknowledges; the simulated fault prevents
        # the gate's acknowledgement from landing. The receipt itself is
        # committed before the fault, so the observation is durable.
        with self.assertRaisesRegex(RuntimeError, 'simulated failure'):
            self.h.acknowledge('driver', 2, fault=FAULT_ONE_RECEIPT)
        self.assertEqual(self.h.disk_receipts(),
                         ['v2.driver.receipt.json'])
        # Both readers still resolve whole versions through the pointer;
        # nothing has been silently back-filled.
        self.assertEqual(self.h.read_driver()['version'], 2)
        self.assertEqual(self.h.read_gate()['version'], 2)
        with self.assertRaises(VersionRollbackError):
            self.h.acknowledge('driver', 1, self._v1_hash())
        # Gate catches up after recovery; receipts are append-only.
        self.h.acknowledge('gate', 2)
        self.assertEqual(len(self.h.receipt_rows()), 2)

    def _v1_hash(self):
        rows=self.h.publish_rows()
        return next(r['content_hash'] for r in rows if r['version'] == 1)

    def test_never_backfills_v1_after_v2_was_observed(self):
        self.h.publish('D1', worker='I1')
        self.h.publish('D2', worker='I2')
        self.h.ack_both(2)
        # Tamper the pointer downward as an attacker/operator might.
        pointer=self.h.export/'.publish/current.json'
        pointer.write_text(pointer.read_text(encoding='utf8').replace('"version": 2',
                                                                      '"version": 1'),
                           encoding='utf8')
        with self.assertRaises(VersionRollbackError):
            self.h.read_driver()
        with self.assertRaises(VersionRollbackError):
            self.h.read_gate()

    def test_pointer_to_missing_payload_is_an_error_not_a_silent_fallback(self):
        self.h.publish('D1', worker='I1')
        import shutil
        shutil.rmtree(self.h.export/'.publish/versions/v1')
        with self.assertRaises(PublicationMissing):
            self.h.read_driver()

    def test_late_revocation_publishes_new_version_keeping_history(self):
        import json
        self.h.publish('D1', worker='I1', task='J1')
        self.h.publish('D3', worker='I1', task='J1', revoked_tasks=['J1'])
        driver=self.h.read_driver()
        self.assertEqual(driver['version'], 2)
        self.assertEqual(driver['workers_csv'].strip().splitlines(),
                         ['day_id,worker,task'])
        rows=self.h.publish_rows()
        self.assertEqual([r['version'] for r in rows], [1, 2])
        # The v1 payload and its history remain untouched and readable.
        self.assertIn(
            'D1,I1,J1',
            (self.h.export/'.publish/versions/v1/workers.csv').read_text())
        self.assertEqual(json.loads(rows[1]['revoked_ids']), ['J1'])


if __name__ == '__main__':
    unittest.main()
