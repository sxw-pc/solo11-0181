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
    # Versioned reservation ledger.  One row per (subject, surface, version,
    # dispute); the ledger version matches the decision version that produced
    # it so a later fee revision never overwrites an already-settled split.
    db.execute('CREATE TABLE IF NOT EXISTS dispute_share (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, surface TEXT NOT NULL, version INTEGER NOT NULL, dispute_id TEXT NOT NULL, claim INTEGER NOT NULL, entitlement INTEGER NOT NULL, funded INTEGER NOT NULL, secured INTEGER NOT NULL, revoked INTEGER NOT NULL, status TEXT NOT NULL, protocol TEXT, protocol_version TEXT, claim_evidence_id TEXT, release_evidence_id TEXT, created_at INTEGER NOT NULL DEFAULT (strftime(\'%s\',\'now\')))')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS dispute_share_unique ON dispute_share(subject,surface,version,dispute_id)')
    # Payment ledger: every pay attempt is kept (accepted or rejected), with the
    # payable that was visible when it arrived.  Credentials (evidence id) are
    # never rewritten; a late-delivered spray is reflected in a new ledger
    # version and a new overpayment/recoverable row instead.
    db.execute('CREATE TABLE IF NOT EXISTS payment (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, version INTEGER NOT NULL, event_id TEXT NOT NULL, dispute_id TEXT, amount INTEGER NOT NULL, accepted INTEGER NOT NULL, status TEXT NOT NULL, payable_then INTEGER NOT NULL, event_time INTEGER NOT NULL, received_at INTEGER NOT NULL, UNIQUE(subject,version,event_id))')
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
        if old and old['result']==value and old['evidence_ids']==proof:
            version=old['version']
        else:
            version=1 if old is None else old['version']+1
            db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(subject,surface,version,value,proof))
        if isinstance(result,dict) and 'shares' in result and 'protocol_version' in result:
            _save_ledger(db,subject,surface,version,result)
    return


def _save_ledger(db,subject,surface,version,ledger):
    """Persist one immutable ledger version: per-dispute shares + payments.

    Rows are keyed by (subject,surface,version,dispute) and never updated, so a
    later fee revision appends a new version instead of rewriting history.
    """
    for dispute_id,row in ledger['shares'].items():
        db.execute(
            'INSERT OR IGNORE INTO dispute_share (subject,surface,version,dispute_id,claim,entitlement,funded,secured,revoked,status,protocol,protocol_version,claim_evidence_id,release_evidence_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (subject,surface,version,dispute_id,int(row['claim']),int(row['entitlement']),int(row['funded']),int(row['secured']),int(row['revoked']),row['status'],ledger.get('protocol'),ledger.get('protocol_version'),row.get('approval_id'),row.get('last_event_id') if row.get('released') or row.get('withdrawn') else None),
        )
    for payment in ledger.get('payments',[]):
        db.execute(
            'INSERT OR IGNORE INTO payment (subject,version,event_id,dispute_id,amount,accepted,status,payable_then,event_time,received_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (subject,version,payment['event_id'],payment.get('dispute_id'),int(payment['amount']),1 if payment['accepted'] else 0,payment['status'],int(payment['payable_then']),int(payment['event_time']),int(payment['received_at'])),
        )

def report(db,subject):
    rows=db.execute('SELECT d.* FROM decision d JOIN (SELECT surface,MAX(version) v FROM decision WHERE subject=? GROUP BY surface) x ON d.surface=x.surface AND d.version=x.v WHERE d.subject=? ORDER BY d.surface',(subject,subject)).fetchall()
    return {r['surface']:dict(version=r['version'],result=json.loads(r['result']),evidence_ids=json.loads(r['evidence_ids'])) for r in rows}

def ledger_history(db,subject,surface='争议款冻结与释放'):
    """Every persisted ledger version, oldest first, with its shares/payments.

    Revisions append rows; this reader is how the cashier (and the tests) prove
    that a spray-driven re-settlement kept both the old credentials and the old
    version instead of mutating them in place.
    """
    decisions=db.execute('SELECT version,result,evidence_ids FROM decision WHERE subject=? AND surface=? ORDER BY version',(subject,surface)).fetchall()
    out=[]
    for d in decisions:
        version=d['version']
        shares=[dict(r) for r in db.execute('SELECT dispute_id,claim,entitlement,funded,secured,revoked,status,protocol,protocol_version,claim_evidence_id,release_evidence_id FROM dispute_share WHERE subject=? AND surface=? AND version=? ORDER BY dispute_id',(subject,surface,version))]
        payments=[dict(r) for r in db.execute('SELECT event_id,dispute_id,amount,accepted,status,payable_then FROM payment WHERE subject=? AND version=? ORDER BY event_time,event_id',(subject,version))]
        out.append(dict(version=version,result=json.loads(d['result']),evidence_ids=json.loads(d['evidence_ids']),shares=shares,payments=payments))
    return out
