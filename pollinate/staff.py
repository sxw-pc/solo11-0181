"""Capture a day of orchard hive inspections and available staff."""
import json
from pathlib import Path
from .store import connect

def import_day(db_path,file):
    raw=Path(file).read_text(encoding='utf8')
    day=json.loads(raw)
    if not day['workers'] or not day['tasks']:
        raise ValueError('empty field day')
    db=connect(db_path)
    try:
        with db:
            db.execute('INSERT INTO field_day VALUES(?,?)',(day['day_id'],raw))
            from .assignment import assign
            return assign(db,day['day_id'])
    finally:db.close()
