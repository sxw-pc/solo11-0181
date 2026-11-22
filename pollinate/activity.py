"""Provisional flight activity from acoustic level alone."""

def assess(db,mic_id):
    row=db.execute('SELECT * FROM hive_audio WHERE mic_id=? ORDER BY at DESC LIMIT 1',(mic_id,)).fetchone()
    status='active' if row['rms']>=0.4 else 'quiet'
    db.execute('INSERT INTO hive_activity VALUES(?,?,?) ON CONFLICT(mic_id) DO UPDATE SET status=excluded.status,rms=excluded.rms',(mic_id,status,row['rms']))
    return {'mic_id':mic_id,'status':status}
