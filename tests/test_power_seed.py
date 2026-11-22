import json,tempfile,unittest
from pathlib import Path
from pollinate.telemetry import import_pack

class PowerSeedTests(unittest.TestCase):
    def test_open_voltage_is_only_provisional(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);file=root/'pack.json'
            file.write_text(json.dumps({'pack_id':'E1','hive_id':'H1','open_v':12.6}),encoding='utf8')
            self.assertEqual(import_pack(root/'case.db',file),{'pack_id':'E1','status':'ready'})
