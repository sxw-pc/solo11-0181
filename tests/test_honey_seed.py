import json,tempfile,unittest
from pathlib import Path
from pollinate.honey import import_blend

class HoneySeedTests(unittest.TestCase):
    def test_blend_mean_release_is_provisional(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);file=root/'blend.json'
            file.write_text(json.dumps({'blend_id':'B1','hive_id':'H1',
                'components':[{'source':'A','kg':10,'ppm':20},{'source':'B','kg':90,'ppm':0}]}),encoding='utf8')
            self.assertEqual(import_blend(root/'case.db',file),
                {'blend_id':'B1','mean_ppm':2,'status':'release'})
