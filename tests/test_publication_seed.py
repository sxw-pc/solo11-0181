import json,tempfile,unittest
from pathlib import Path
from pollinate.staff import import_day

class PublicationSeedTests(unittest.TestCase):
    def test_assignment_writes_two_roster_files(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);file=root/'day.json';out=root/'export'
            file.write_text(json.dumps({'day_id':'D1','publish_dir':str(out),
                'workers':[{'id':'I1','skills':['health']}],
                'tasks':[{'id':'J1','skill':'health','start':1,'end':3}]}),encoding='utf8')
            import_day(root/'case.db',file)
            self.assertIn('D1,I1,J1',(out/'workers.csv').read_text(encoding='utf8'))
            self.assertIn('D1,J1,I1,1,3',(out/'orchard_jobs.csv').read_text(encoding='utf8'))
