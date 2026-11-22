"""Import a hive logger battery summary."""
import json
from pathlib import Path
from .store import connect

def import_pack(db_path,file):
    raw=Path(file).read_text(encoding='utf8')
    pack=json.loads(raw)
    if pack['open_v']<0:
        raise ValueError('invalid open circuit voltage')
    db=connect(db_path)
    try:
        with db:
            db.execute('INSERT INTO logger_pack VALUES(?,?,?,?)',
                (pack['pack_id'],pack['hive_id'],pack['open_v'],raw))
            from .power import assess
            result=assess(db,pack['pack_id'])
            if pack.get('devices'):
                from .power import provisional_plan
                result['plan']=provisional_plan(db,pack['pack_id'])
            return result
    finally:db.close()
