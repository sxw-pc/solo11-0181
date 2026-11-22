"""Capture microphone summaries near an orchard hive."""
import json
from pathlib import Path
from .store import connect

def import_mic(db_path,file):
    raw=Path(file).read_text(encoding='utf8');doc=json.loads(raw)
    db=connect(db_path)
    try:
        with db:
            if not doc['mic_id'] or doc['sample_hz']<=0:raise ValueError('invalid_mic')
            db.execute('INSERT INTO hive_mic VALUES(?,?,?,?)',(doc['mic_id'],doc['hive_id'],doc['sample_hz'],raw))
        return {'mic_id':doc['mic_id']}
    finally:db.close()

def import_audio(db_path,file):
    raw=Path(file).read_text(encoding='utf8');doc=json.loads(raw)
    db=connect(db_path)
    try:
        with db:
            if doc['rms']<0:raise ValueError('invalid_rms')
            db.execute('INSERT INTO hive_audio VALUES(?,?,?,?,?)',(doc['audio_id'],doc['mic_id'],doc['at'],doc['rms'],raw))
            from .activity import assess
            return assess(db,doc['mic_id'])
    finally:db.close()
