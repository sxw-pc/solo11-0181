import json,tempfile,unittest
from pathlib import Path
from pollinate.telemetry import import_pack

class PowerBusSeedTests(unittest.TestCase):
    def test_each_device_checked_without_total_bus_current(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);file=root/'pack.json'
            file.write_text(json.dumps({'pack_id':'E2','hive_id':'H1','open_v':12.6,
                'internal_r':0.2,'devices':[{'id':'H1','amps':8,'min_v':10},
                                           {'id':'H2','amps':8,'min_v':10}]}),encoding='utf8')
            self.assertEqual(import_pack(root/'case.db',file)['plan'],['H1','H2'])
