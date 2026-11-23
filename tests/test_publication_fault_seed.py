"""Fault-seed scenario under the crash-safe publication protocol.

The original accident left "driver v2 / gate v1 / database v1" because two
bare CSV overwrites had no shared publication boundary. Under the fixed-name
contract the same fault now produces an explicit, mutually-visible suspension
instead: live files keep serving the complete v1 roster while STOP_WORK is
raised, and no v2 row reaches field_roster until recovery completes.
"""
import json, tempfile, unittest
from pathlib import Path
from pollinate.staff import import_day
from pollinate.store import connect
from pollinate.publish import (
    PublicationSuspended, FAULT_AFTER_WORKERS, FIXED_MODE,
)


class PublicationFaultSeedTests(unittest.TestCase):
    def _write(self, root, out, day_id, worker, fail=False):
        file=root/f'{day_id}.json'
        payload={'day_id': day_id, 'publish_dir': str(out),
                 'publish_mode': FIXED_MODE,
                 'workers': [{'id': worker, 'skills': ['health']}],
                 'tasks': [{'id': 'J1', 'skill': 'health',
                            'start': 1, 'end': 3}]}
        if fail:
            payload['fault']=FAULT_AFTER_WORKERS
            payload['fail_after_workers']=True
        file.write_text(json.dumps(payload), encoding='utf8')
        return file

    def test_fault_after_first_file_suspends_instead_of_split_roster(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); out=root/'export'; db_path=root/'case.db'
            import_day(db_path, self._write(root, out, 'D1', 'I1'))
            with self.assertRaisesRegex(RuntimeError, 'simulated failure'):
                import_day(db_path, self._write(root, out, 'D2', 'I2', True))

            db=connect(db_path)
            try:
                # The SQLite transaction still rolls back: no D2 roster row.
                self.assertIsNone(db.execute(
                    "SELECT * FROM field_roster WHERE day_id='D2'").fetchone())
                self.assertIsNone(db.execute(
                    "SELECT * FROM roster_publish WHERE day_id='D2'").fetchone())
            finally:
                db.close()

            # Fixed-name readers no longer see a half-published shift.
            # The live files still carry the complete v1 roster...
            self.assertIn('D1,I1,J1',
                          (out/'workers.csv').read_text(encoding='utf8'))
            self.assertIn('D1,J1,I1,1,3',
                          (out/'orchard_jobs.csv').read_text(encoding='utf8'))
            self.assertNotIn('D2',
                             (out/'workers.csv').read_text(encoding='utf8'))
            # ...and the cooperative stop-work order tells both parties to
            # pause rather than act on disagreeing lists.
            self.assertTrue((out/'STOP_WORK').exists())
            db=connect(db_path)
            try:
                from pollinate.publish import read_fixed_driver, read_fixed_gate
                for reader in (read_fixed_driver, read_fixed_gate):
                    with self.assertRaises(PublicationSuspended):
                        reader(out, db)
            finally:
                db.close()

            # The failed attempt is still durable on disk: v2 payload exists
            # in the versioned area and its version number is never reused.
            self.assertTrue((out/'.publish/versions/v2/workers.csv').exists())
            self.assertTrue((out/'.publish/journal/v2.json').exists())

            # Recovery rebuilds the row, repoints the live files and, once both
            # parties acknowledge v2, lifts the stop-work order together.
            from pollinate.publish import recover_publish, acknowledge, try_resume
            db=connect(db_path)
            try:
                summary=recover_publish(db, str(out))
                self.assertEqual(summary['rebuilt'], [2])
                self.assertIn(2, [r['version'] for r in db.execute(
                    'SELECT version FROM roster_publish')])
                for party in ('driver', 'gate'):
                    h=db.execute(
                        'SELECT content_hash FROM roster_publish WHERE version=2'
                    ).fetchone()['content_hash']
                    acknowledge(out, party, 2, h, db=db)
                self.assertTrue(try_resume(db, str(out)))
            finally:
                db.close()
            self.assertFalse((out/'STOP_WORK').exists())
            self.assertIn('D2,I2,J1',
                          (out/'workers.csv').read_text(encoding='utf8'))
            self.assertIn('D2,J1,I2,1,3',
                          (out/'orchard_jobs.csv').read_text(encoding='utf8'))
