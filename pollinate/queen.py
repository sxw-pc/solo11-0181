"""Import a beekeeper's candidate queen-drone pair sheet."""
import json
from pathlib import Path
from .store import connect

def import_pair(db_path,file):
    raw=Path(file).read_text(encoding='utf8')
    pair=json.loads(raw)
    if not pair['queen_id'] or not pair['drone_id']:
        raise ValueError('missing breeding identity')
    db=connect(db_path)
    try:
        with db:
            db.execute('INSERT INTO queen_pair VALUES(?,?,?)',(pair['pair_id'],pair['hive_id'],raw))
            from .kinship import assess
            return assess(db,pair['pair_id'])
    finally:db.close()
