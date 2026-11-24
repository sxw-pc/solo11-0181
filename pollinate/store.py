ENTITIES=['hive', 'placement', 'health_check', 'orchard', 'bloom_observation', 'spray_notice', 'contract', 'settlement', 'incident']
import json, sqlite3
from pathlib import Path

def connect(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, event_time INTEGER NOT NULL, received_at INTEGER NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS decision (subject TEXT NOT NULL, surface TEXT NOT NULL, version INTEGER NOT NULL, result TEXT NOT NULL, evidence_ids TEXT NOT NULL, PRIMARY KEY(subject,surface,version))')
    db.execute('CREATE TABLE IF NOT EXISTS rejection (id TEXT PRIMARY KEY, reason TEXT NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS hive_mic (id TEXT PRIMARY KEY, hive_id TEXT NOT NULL, sample_hz INTEGER NOT NULL, raw TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS hive_audio (id TEXT PRIMARY KEY, mic_id TEXT NOT NULL REFERENCES hive_mic(id), at INTEGER NOT NULL, rms REAL NOT NULL, raw TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS hive_activity (mic_id TEXT PRIMARY KEY, status TEXT NOT NULL, rms REAL NOT NULL)')
    # Versioned provisional loudness projection.  hive_activity stays the
    # latest projection for the original contract; the log keeps every change.
    db.execute('CREATE TABLE IF NOT EXISTS hive_activity_log (mic_id TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL, rms REAL NOT NULL, audio_id TEXT, at REAL NOT NULL, PRIMARY KEY(mic_id,version))')
    # One power-on clock epoch per mic.  Every clip resolves through this single
    # mapping; clips may never be phase-shifted one at a time.
    db.execute('CREATE TABLE IF NOT EXISTS hive_clock (epoch_id TEXT PRIMARY KEY, mic_id TEXT NOT NULL, sample_hz INTEGER NOT NULL, ref_time REAL NOT NULL, offset_samples INTEGER NOT NULL, ppm REAL NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS hive_capture (
        id TEXT PRIMARY KEY,
        mic_id TEXT NOT NULL REFERENCES hive_mic(id),
        hive_id TEXT NOT NULL,
        zone TEXT NOT NULL,
        kind TEXT NOT NULL,
        at REAL NOT NULL,
        coverage_start REAL NOT NULL,
        coverage_end REAL NOT NULL,
        sample_hz INTEGER NOT NULL,
        seq_start INTEGER NOT NULL,
        seq_end INTEGER NOT NULL,
        n_samples INTEGER NOT NULL,
        missing TEXT NOT NULL,
        antialias_state TEXT NOT NULL,
        antialias_cutoff_hz REAL,
        clock_epoch TEXT NOT NULL,
        clock_ok INTEGER NOT NULL,
        time_closed INTEGER NOT NULL,
        pump_state TEXT,
        rms REAL NOT NULL,
        alias_suspect INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        raw TEXT NOT NULL,
        pcm16 TEXT NOT NULL)''')
    db.execute('CREATE TABLE IF NOT EXISTS pump_event (id TEXT PRIMARY KEY, zone TEXT NOT NULL, at REAL NOT NULL, state TEXT NOT NULL, sha256 TEXT NOT NULL, raw TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS infrared_counter (id TEXT PRIMARY KEY, hive_id TEXT NOT NULL, coverage_start REAL NOT NULL, coverage_end REAL NOT NULL, sha256 TEXT NOT NULL, raw TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS infrared_pulse (id TEXT PRIMARY KEY, counter_id TEXT NOT NULL REFERENCES infrared_counter(id), seq INTEGER NOT NULL, at REAL NOT NULL, direction TEXT NOT NULL, raw TEXT NOT NULL)')
    # Versioned flight assessment.  Nothing here is ever rewritten or deleted.
    db.execute('CREATE TABLE IF NOT EXISTS flight_assessment (hive_id TEXT NOT NULL, version INTEGER NOT NULL, result TEXT NOT NULL, evidence_ids TEXT NOT NULL, PRIMARY KEY(hive_id,version))')
    for kind in ENTITIES:
        db.execute(f'CREATE TABLE IF NOT EXISTS {kind} (id TEXT PRIMARY KEY REFERENCES evidence(id), subject TEXT NOT NULL, event_time INTEGER NOT NULL, payload TEXT NOT NULL)')
    db.commit()
    return db

def ingest(db, event):
    for key in ('id','kind','subject','event_time','received_at'):
        if key not in event: raise ValueError('missing '+key)
    if event['kind'] not in ENTITIES: raise ValueError('unknown kind')
    payload=json.dumps(event.get('data',{}),ensure_ascii=False,sort_keys=True)
    old=db.execute('SELECT kind,subject,event_time,received_at,payload FROM evidence WHERE id=?',(event['id'],)).fetchone()
    current=(event['kind'],event['subject'],int(event['event_time']),int(event['received_at']),payload)
    if old:
        if tuple(old)==current: return False
        raise ValueError('conflicting evidence id '+event['id'])
    db.execute('INSERT INTO evidence VALUES (?,?,?,?,?,?)',(event['id'],*current))
    db.execute(f'INSERT INTO {event["kind"]} VALUES (?,?,?,?)',(event['id'],event['subject'],int(event['event_time']),payload))
    return True

def evidence(db,subject):
    return [dict(id=r['id'],kind=r['kind'],subject=r['subject'],event_time=r['event_time'],received_at=r['received_at'],data=json.loads(r['payload'])) for r in db.execute('SELECT * FROM evidence WHERE subject=? ORDER BY event_time,id',(subject,))]

def save_decisions(db,subject,results):
    for surface,result,ids in results:
        value=json.dumps(result,ensure_ascii=False,sort_keys=True)
        proof=json.dumps(sorted(set(ids)),ensure_ascii=False)
        old=db.execute('SELECT version,result,evidence_ids FROM decision WHERE subject=? AND surface=? ORDER BY version DESC LIMIT 1',(subject,surface)).fetchone()
        if old and old['result']==value and old['evidence_ids']==proof: continue
        version=1 if old is None else old['version']+1
        db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(subject,surface,version,value,proof))

def report(db,subject):
    rows=db.execute('SELECT d.* FROM decision d JOIN (SELECT surface,MAX(version) v FROM decision WHERE subject=? GROUP BY surface) x ON d.surface=x.surface AND d.version=x.v WHERE d.subject=? ORDER BY d.surface',(subject,subject)).fetchall()
    return {r['surface']:dict(version=r['version'],result=json.loads(r['result']),evidence_ids=json.loads(r['evidence_ids'])) for r in rows}


def log_activity(db, mic_id, status, rms, audio_id, at):
    """Append the provisional projection when it actually changes."""
    old=db.execute('SELECT version,status,rms,audio_id FROM hive_activity_log WHERE mic_id=? ORDER BY version DESC LIMIT 1',(mic_id,)).fetchone()
    if old and old['status']==status and old['rms']==rms and old['audio_id']==audio_id:
        return old['version']
    version=1 if old is None else old['version']+1
    db.execute('INSERT INTO hive_activity_log VALUES(?,?,?,?,?,?)',(mic_id,version,status,rms,audio_id,at))
    db.commit()
    return version


def activity_history(db, mic_id):
    return [dict(r) for r in db.execute('SELECT version,status,rms,audio_id,at FROM hive_activity_log WHERE mic_id=? ORDER BY version',(mic_id,))]


def save_flight_assessment(db, hive_id, result, evidence_ids):
    """Store a new version only when the conclusion or its proof changes."""
    value=json.dumps(result,ensure_ascii=False,sort_keys=True)
    proof=json.dumps(sorted(set(evidence_ids)),ensure_ascii=False)
    old=db.execute('SELECT version,result,evidence_ids FROM flight_assessment WHERE hive_id=? ORDER BY version DESC LIMIT 1',(hive_id,)).fetchone()
    if old and old['result']==value and old['evidence_ids']==proof:
        return old['version']
    version=1 if old is None else old['version']+1
    db.execute('INSERT INTO flight_assessment VALUES(?,?,?,?)',(hive_id,version,value,proof))
    db.commit()
    return version


def flight_history(db, hive_id):
    return [dict(version=r['version'],result=json.loads(r['result']),evidence_ids=json.loads(r['evidence_ids'])) for r in db.execute('SELECT version,result,evidence_ids FROM flight_assessment WHERE hive_id=? ORDER BY version',(hive_id,))]
