"""Route 2 — fixed CSV filenames plus a cooperative stop-work order.

Legacy parties can only read workers.csv / orchard_jobs.csv. During any
uncertain boundary the publisher raises STOP_WORK, which both readers are
required to honour as an explicit "roster suspended" state. Work resumes only
after the two live files, the manifest and both party receipts agree.
"""
import json
import unittest
from tests.publication_support import (
    PublicationHarness, FIXED_MODE,
    FAULT_AFTER_WORKERS, FAULT_AFTER_JOBS, FAULT_BEFORE_COMMIT,
    FAULT_ONE_RECEIPT,
)
from pollinate.publish import (
    PublicationSuspended, VersionRollbackError, PublicationInconsistent,
)


class FixedRouteTests(unittest.TestCase):
    def setUp(self):
        self.h=PublicationHarness(mode=FIXED_MODE)

    def tearDown(self):
        self.h.cleanup()

    def test_healthy_publish_serves_one_version_and_clears_pause(self):
        self.h.publish('D1', worker='I1')
        self.assertFalse(self.h.suspended()['suspended'])
        driver=self.h.read_driver()
        gate=self.h.read_gate()
        self.assertEqual((driver['version'], gate['version']), (1, 1))
        self.assertIn('D1,I1,J1', driver['workers_csv'])
        self.assertIn('D1,J1,I1,1,3', gate['jobs_csv'])

    def _check_paused_on_v1(self):
        status=self.h.suspended()
        self.assertTrue(status['suspended'])
        # Every direct read is an explicit suspension — never a half roster.
        with self.assertRaises(PublicationSuspended):
            self.h.read_driver()
        with self.assertRaises(PublicationSuspended):
            self.h.read_gate()
        # The last *complete* live content is still v1 underneath.
        self.assertIn('D1,I1,J1',
                      (self.h.export/'workers.csv').read_text('utf8'))
        self.assertIn('D1,J1,I1',
                      (self.h.export/'orchard_jobs.csv').read_text('utf8'))
        self.assertNotIn('D2',
                         (self.h.export/'workers.csv').read_text('utf8'))

    def test_fault_after_workers_pauses_both_parties(self):
        self.h.publish('D1', worker='I1')
        with self.assertRaisesRegex(RuntimeError, 'simulated failure'):
            self.h.publish('D2', worker='I2', fault=FAULT_AFTER_WORKERS)
        self._check_paused_on_v1()
        self.assertNotIn('D2', self.h.roster_day_ids())

    def test_fault_after_jobs_pauses_both_parties(self):
        self.h.publish('D1', worker='I1')
        with self.assertRaisesRegex(RuntimeError, 'simulated failure'):
            self.h.publish('D2', worker='I2', fault=FAULT_AFTER_JOBS)
        self._check_paused_on_v1()

    def test_fault_before_commit_pauses_both_parties(self):
        self.h.publish('D1', worker='I1')
        with self.assertRaisesRegex(RuntimeError, 'simulated failure'):
            self.h.publish('D2', worker='I2', fault=FAULT_BEFORE_COMMIT)
        self._check_paused_on_v1()

    def test_restart_rebuilds_and_waits_for_both_receipts_to_resume(self):
        self.h.publish('D1', worker='I1')
        with self.assertRaises(RuntimeError):
            self.h.publish('D2', worker='I2', fault=FAULT_AFTER_WORKERS)
        summary=self.h.restart_and_recover()
        self.assertEqual(summary['rebuilt'], [2])
        # Recovery cannot lift the pause until both field parties confirm v2.
        self.assertTrue(self.h.suspended()['suspended'])
        self.h.acknowledge('driver', 2)
        self.assertFalse(self.h.try_resume())
        self.assertTrue(self.h.suspended()['suspended'])
        self.h.acknowledge('gate', 2)
        self.assertTrue(self.h.try_resume())
        self.assertFalse(self.h.suspended()['suspended'])
        driver=self.h.read_driver()
        gate=self.h.read_gate()
        self.assertEqual((driver['version'], gate['version']), (2, 2))
        self.assertIn('D2,I2,J1', driver['workers_csv'])
        self.assertIn('D2,J1,I2,1,3', gate['jobs_csv'])

    def test_one_receipt_fault_leaves_explicit_pause_until_second_ack(self):
        self.h.publish('D1', worker='I1')
        self.h.publish('D2', worker='I2')
        self.h.ack_both(2)  # healthy baseline now on v2, receipts present
        self.h.publish('D3', worker='I3')
        # A crash after the driver's v3 receipt: raise the stop order again to
        # model the field state when only one party has observed the new list.
        from pollinate.publish import _raise_stop
        _raise_stop(str(self.h.export), 3, 'awaiting second party receipt')
        with self.assertRaisesRegex(RuntimeError, 'simulated failure'):
            self.h.acknowledge('driver', 3, fault=FAULT_ONE_RECEIPT)
        self.assertEqual(self.h.disk_receipts().count(
            'v3.driver.receipt.json'), 1)
        # The live files are v3 but receipts disagree, so work stays paused.
        with self.assertRaises(PublicationSuspended):
            self.h.read_gate()
        self.assertFalse(self.h.try_resume())
        self.h.acknowledge('gate', 3)
        self.assertTrue(self.h.try_resume())
        self.assertEqual(self.h.read_driver()['version'], 3)

    def test_torn_live_files_after_crash_are_detected_not_served(self):
        self.h.publish('D1', worker='I1')
        self.h.publish('D2', worker='I2')
        self.h.ack_both(2)
        # Simulate a crash between the two atomic live-file replacements:
        # workers.csv already v3 content, orchard_jobs.csv still v2.
        self.h.publish('D3', worker='I3')
        v3_workers=(self.h.export/'.publish/versions/v3/workers.csv').read_bytes()
        from pollinate.publish import atomic_write, STOP_ORDER
        atomic_write(self.h.export/'orchard_jobs.csv',
                     (self.h.export/'.publish/versions/v2/orchard_jobs.csv')
                     .read_bytes())
        atomic_write(self.h.export/'STOP_WORK',
                     json.dumps({'version': 3, 'reason': 'torn swap'}).encode())
        with self.assertRaises(PublicationInconsistent):
            self._read_live_anyway('driver')
        with self.assertRaises(PublicationInconsistent):
            self._read_live_anyway('gate')
        # Recovery re-promotes the immutable v3 payload over the torn live
        # files; with both v3 receipts already present the pause lifts too.
        self.h.ack_both(3)
        summary=self.h.restart_and_recover()
        self.assertTrue(summary['resumed'])
        self.assertFalse(self.h.suspended()['suspended'])
        self.assertEqual(self.h.read_gate()['version'], 3)

    def _read_live_anyway(self, party):
        from pollinate.publish import read_fixed_driver, read_fixed_gate
        db=self.h.open_db()
        try:
            reader=read_fixed_driver if party == 'driver' else read_fixed_gate
            return reader(self.h.export, db, allow_suspended=True)
        finally:
            db.close()

    def test_no_downgrade_after_v2_was_seen_in_field(self):
        self.h.publish('D1', worker='I1')
        self.h.publish('D2', worker='I2')
        self.h.ack_both(2)
        rows=self.h.publish_rows()
        v1_hash=next(r['content_hash'] for r in rows if r['version'] == 1)
        # A forged attempt to acknowledge old v1 content is rejected outright.
        with self.assertRaises(VersionRollbackError):
            self._forge_v1_ack(v1_hash)

    def _forge_v1_ack(self, v1_hash):
        from pollinate.publish import acknowledge
        db=self.h.open_db()
        try:
            acknowledge(self.h.export, 'driver', 1, v1_hash, db=db)
        finally:
            db.close()

    def test_revocation_under_pause_resumes_without_erasing_history(self):
        self.h.publish('D1', worker='I1', task='J1')
        with self.assertRaises(RuntimeError):
            self.h.publish('D2', worker='I1', task='J1',
                           revoked_tasks=['J1'], fault=FAULT_AFTER_JOBS)
        self.h.restart_and_recover()
        self.h.ack_both(2)
        self.assertTrue(self.h.try_resume())
        view=self.h.read_driver()
        self.assertEqual(view['version'], 2)
        self.assertEqual(view['workers_csv'].strip().splitlines(),
                         ['day_id,worker,task'])
        # v1 receipt history and v1 payload are still intact.
        receipts=self.h.receipt_rows()
        self.assertTrue(all(r['version'] in (1, 2) for r in receipts))
        self.assertIn(
            'D1,I1,J1',
            (self.h.export/'.publish/versions/v1/workers.csv').read_text())


if __name__ == '__main__':
    unittest.main()
