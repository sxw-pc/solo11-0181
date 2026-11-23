"""Capture a day of orchard hive inspections and available staff."""
import json
from pathlib import Path
from .store import connect

def import_day(db_path, file):
    raw=Path(file).read_text(encoding='utf8')
    day=json.loads(raw)
    if not day['workers'] or not day['tasks']:
        raise ValueError('empty field day')
    db=connect(db_path)
    try:
        activation=None
        with db:
            existing=db.execute(
                'SELECT raw_json FROM field_day WHERE id=?',
                (day['day_id'],)).fetchone()
            if existing is None:
                db.execute('INSERT INTO field_day VALUES(?,?)',
                           (day['day_id'], raw))
            elif existing['raw_json'] != raw:
                raise ValueError(
                    f'conflicting field day re-import for {day["day_id"]}')
            from .assignment import assign
            result=assign(db, day['day_id'])
            activation=result.pop('activation')
        # Commit boundary crossed: now flip the externally observable state.
        if activation is not None:
            activation.run(db)
        return result
    finally:
        db.close()
