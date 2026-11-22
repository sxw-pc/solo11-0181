"""Capture a coarse honey-blend worksheet for a pollination hive."""
import json
from pathlib import Path
from .store import connect

def import_blend(db_path,file):
    raw=Path(file).read_text(encoding='utf8')
    blend=json.loads(raw)
    if not blend['components'] or any(c['kg']<=0 or c['ppm']<0 for c in blend['components']):
        raise ValueError('invalid blend')
    db=connect(db_path)
    try:
        with db:
            db.execute('INSERT INTO honey_blend VALUES(?,?,?)',(blend['blend_id'],blend['hive_id'],raw))
            from .grade import assess
            return assess(db,blend['blend_id'])
    finally:db.close()
