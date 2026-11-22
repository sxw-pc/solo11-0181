import json,tempfile,unittest
from pathlib import Path
from pollinate.audio import import_mic,import_audio
from pollinate.store import connect

class AudioSeedTests(unittest.TestCase):
    def test_rms_activity_is_provisional(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);db=root/'case.db'
            mic=root/'mic.json';mic.write_text(json.dumps({'mic_id':'M1','hive_id':'H1','sample_hz':200}),encoding='utf8')
            sample=root/'sample.json';sample.write_text(json.dumps({'audio_id':'A1','mic_id':'M1','at':10,'rms':0.5}),encoding='utf8')
            import_mic(db,mic)
            self.assertEqual(import_audio(db,sample)['status'],'active')
            c=connect(db)
            try:self.assertEqual(c.execute('SELECT status FROM hive_activity').fetchone()[0],'active')
            finally:c.close()
