import json,tempfile,unittest
from pathlib import Path
from pollinate.staff import import_day
from pollinate.store import connect

class PublicationFaultSeedTests(unittest.TestCase):
    def test_first_file_survives_transaction_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);out=root/'export';db_path=root/'case.db'
            def write(day_id,worker,fail=False):
                file=root/f'{day_id}.json'
                file.write_text(json.dumps({'day_id':day_id,'publish_dir':str(out),
                    'fail_after_workers':fail,
                    'workers':[{'id':worker,'skills':['health']}],
                    'tasks':[{'id':'J1','skill':'health','start':1,'end':3}]}),encoding='utf8')
                return file
            import_day(db_path,write('D1','I1'))
            with self.assertRaisesRegex(RuntimeError,'simulated failure'):
                import_day(db_path,write('D2','I2',True))
            self.assertIn('D2,I2,J1',(out/'workers.csv').read_text(encoding='utf8'))
            self.assertIn('D1,J1,I1',(out/'orchard_jobs.csv').read_text(encoding='utf8'))
            db=connect(db_path)
            try:self.assertIsNone(db.execute("SELECT * FROM field_roster WHERE day_id='D2'").fetchone())
            finally:db.close()
