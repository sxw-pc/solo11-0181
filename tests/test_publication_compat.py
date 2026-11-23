"""Backwards compatibility for the original write_two direct-export entry point."""
import json, tempfile, unittest
from pathlib import Path
from pollinate.store import connect
from pollinate.publish import write_two, FIXED_MODE


class WriteTwoCompatTests(unittest.TestCase):
    def test_write_two_signature_still_produces_live_csvs(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); out=root/'export'; dbp=root/'case.db'
            db=connect(dbp)
            with db:
                db.execute('INSERT INTO field_day VALUES(?,?)',
                           ('D1', json.dumps({
                               'day_id': 'D1', 'publish_dir': str(out),
                               'publish_mode': FIXED_MODE,
                               'workers': [{'id': 'I1', 'skills': ['health']}],
                               'tasks': [{'id': 'J1', 'skill': 'health',
                                          'start': 1, 'end': 3}]})))
                db.execute('INSERT INTO field_roster VALUES(?,?)',
                           ('D1', json.dumps([
                               {'task': 'J1', 'worker': 'I1',
                                'start': 1, 'end': 3}])))
            version=write_two(db, 'D1', out)
            db.close()
            self.assertEqual(version, 1)
            self.assertIn('D1,I1,J1',
                          (out/'workers.csv').read_text('utf8'))
            self.assertIn('D1,J1,I1,1,3',
                          (out/'orchard_jobs.csv').read_text('utf8'))
            self.assertFalse((out/'STOP_WORK').exists())


if __name__ == '__main__':
    unittest.main()
