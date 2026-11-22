"""Provisional logger availability based on open circuit voltage."""
import json

def assess(db,pack_id):
    row=db.execute('SELECT * FROM logger_pack WHERE id=?',(pack_id,)).fetchone()
    status='ready' if row['open_v']>=12 else 'charge'
    db.execute('INSERT INTO logger_power VALUES(?,?)',(pack_id,status))
    return {'pack_id':pack_id,'status':status}

def provisional_plan(db,pack_id):
    row=db.execute('SELECT raw_json FROM logger_pack WHERE id=?',(pack_id,)).fetchone()
    pack=json.loads(row['raw_json'])
    active=[d['id'] for d in pack['devices'] if pack['open_v']-pack['internal_r']*d['amps']>=d['min_v']]
    db.execute('INSERT INTO logger_plan VALUES(?,?)',(pack_id,json.dumps(active)))
    return active
