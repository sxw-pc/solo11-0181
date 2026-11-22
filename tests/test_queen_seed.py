import json,tempfile,unittest
from pathlib import Path
from pollinate.queen import import_pair

class QueenSeedTests(unittest.TestCase):
    def test_direct_parent_screen_is_provisional(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);file=root/'pair.json'
            file.write_text(json.dumps({'pair_id':'Q1-D1','hive_id':'H1','queen_id':'Q1','drone_id':'D1',
                'queen_parents':['M1','F1'],'drone_parents':['M2','F2']}),encoding='utf8')
            self.assertEqual(import_pair(root/'case.db',file)['status'],'allow')
