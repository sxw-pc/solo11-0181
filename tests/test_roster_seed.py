import json,tempfile,unittest
from pathlib import Path
from pollinate.staff import import_day

class RosterSeedTests(unittest.TestCase):
    def test_first_available_assignment(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);file=root/'day.json'
            file.write_text(json.dumps({'day_id':'D1','workers':[
                {'id':'I1','skills':['health','spray']},{'id':'I2','skills':['health']}],
                'tasks':[{'id':'J1','skill':'health','start':1,'end':3}]}),encoding='utf8')
            self.assertEqual(import_day(root/'case.db',file)['assignments'][0]['worker'],'I1')
