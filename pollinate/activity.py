"""Provisional flight activity from acoustic level alone.

This module keeps the original contract: the latest clip's RMS >= 0.4 writes
``active``.  That projection is only a loudness statement -- it cannot tell a
hive from the irrigation pump sharing the pole -- so every change is also kept
in a versioned log and the enhanced proof lives in :mod:`pollinate.flight_proof`.
"""
from .store import log_activity

ACTIVE_RMS=0.4

def assess(db,mic_id):
    row=db.execute('SELECT * FROM hive_audio WHERE mic_id=? ORDER BY at DESC LIMIT 1',(mic_id,)).fetchone()
    if row is None: raise ValueError('no_audio')
    status='active' if row['rms']>=ACTIVE_RMS else 'quiet'
    db.execute('INSERT INTO hive_activity VALUES(?,?,?) ON CONFLICT(mic_id) DO UPDATE SET status=excluded.status,rms=excluded.rms',(mic_id,status,row['rms']))
    version=log_activity(db,mic_id,status,row['rms'],row['id'],row['at'])
    return {'mic_id':mic_id,'status':status,'rms':row['rms'],'provisional':True,'basis':'rms>=0.4','activity_version':version}
