"""Crash between SQLite commit and the post-commit activation boundary.

The staging transaction can commit (so the roster_publish row exists as
``staged``) while the process dies before the pointer flip or live-file swap.
Readers must keep serving the previous complete version; recovery finishes
the activation without ever exposing a half boundary.
"""
import unittest
from tests.publication_support import (
    PublicationHarness, POINTER_MODE, FIXED_MODE,
)
from pollinate.publish import STATE_STAGED, STATE_ACTIVE


class StagedCrashTests(unittest.TestCase):
    def _force_staged_then_crash(self, h, day_id, worker):
        # Drive staging directly, commit it, but never run the activation.
        db=h.open_db()
        try:
            from pollinate.assignment import assign
            raw=(h.root/f'{day_id}.json').read_text('utf8')
            with db:
                db.execute('INSERT OR IGNORE INTO field_day VALUES(?,?)',
                           (day_id, raw))
                result=assign(db, day_id)
                activation=result.pop('activation')
            # Process dies here: row is staged, pointer/live files untouched.
        finally:
            db.close()
        return activation.version

    def test_pointer_mode_staged_crash_keeps_v1_until_recovery(self):
        with PublicationHarness(mode=POINTER_MODE) as h:
            h.publish('D1', worker='I1')
            h.write_day('D2', worker='I2')
            version=self._force_staged_then_crash(h, 'D2', 'I2')
            self.assertEqual(version, 2)
            # Staged row exists, but readers still resolve the old pointer v1.
            rows={r['version']: r['state'] for r in h.publish_rows()}
            self.assertEqual(rows, {1: STATE_ACTIVE, 2: STATE_STAGED})
            self.assertEqual(h.read_driver()['version'], 1)
            self.assertEqual(h.read_gate()['version'], 1)
            summary=h.restart_and_recover()
            self.assertEqual(h.read_driver()['version'], 2)
            self.assertEqual(h.read_gate()['version'], 2)
            self.assertTrue(
                all(r['state'] == STATE_ACTIVE for r in h.publish_rows()))

    def test_fixed_mode_staged_crash_keeps_v1_live_and_pauses(self):
        with PublicationHarness(mode=FIXED_MODE) as h:
            h.publish('D1', worker='I1')
            h.write_day('D2', worker='I2')
            self._force_staged_then_crash(h, 'D2', 'I2')
            # Pause was raised during staging; live files still hash to v1.
            self.assertTrue(h.suspended()['suspended'])
            self.assertIn('D1,I1,J1',
                          (h.export/'workers.csv').read_text('utf8'))
            summary=h.restart_and_recover()
            self.assertEqual(summary['rebuilt'], [2])
            h.ack_both(2)
            self.assertTrue(h.try_resume())
            self.assertEqual(h.read_driver()['version'], 2)
            self.assertIn('D2,I2,J1',
                          (h.export/'workers.csv').read_text('utf8'))


if __name__ == '__main__':
    unittest.main()
