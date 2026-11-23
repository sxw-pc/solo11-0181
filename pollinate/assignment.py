"""First-available field inspection assignment and publication staging."""
import json
from .publish import publish_roster, FIXED_MODE

def _load_day(db, day_id):
    row=db.execute('SELECT * FROM field_day WHERE id=?', (day_id,)).fetchone()
    if row is None:
        raise KeyError(f'unknown field day {day_id}')
    return json.loads(row['raw_json'])

def compute_assignments(day):
    """Pure first-available matching; reusable by tests and recovery."""
    assigned=[]
    revoked=set(day.get('revoked_tasks') or [])
    for task in day['tasks']:
        if task['id'] in revoked:
            continue
        worker=next((w for w in day['workers'] if task['skill'] in w['skills'] and
            not any(a['worker']==w['id'] and a['start']<task['end']
                    and task['start']<a['end'] for a in assigned)), None)
        assigned.append({'task': task['id'], 'worker': worker['id'] if worker else None,
                         'start': task['start'], 'end': task['end']})
    return assigned, sorted(revoked)

def assign(db, day_id):
    """Compute the roster, persist it and stage publication.

    The returned ``activation`` is either None (no publish_dir) or an object
    whose ``run(db)`` must be invoked *after* the surrounding SQLite
    transaction has committed. All staging writes participate in that
    transaction, so a pre-commit fault rolls the database back while the
    durable on-disk journal keeps the version number from being reused.
    """
    day=_load_day(db, day_id)
    assigned, revoked_ids=compute_assignments(day)
    roster_json=json.dumps(assigned)
    existing=db.execute(
        'SELECT assignments_json FROM field_roster WHERE day_id=?',
        (day_id,)).fetchone()
    if existing is None:
        db.execute('INSERT INTO field_roster VALUES(?,?)', (day_id, roster_json))
    elif existing['assignments_json'] != roster_json:
        raise ValueError(
            f'field_roster for {day_id} already exists with different content')
    result={'day_id': day_id, 'assignments': assigned, 'activation': None}
    if day.get('publish_dir'):
        from .publish import FAULT_AFTER_WORKERS
        fault=day.get('fault')
        if day.get('fail_after_workers') and not fault:
            fault=FAULT_AFTER_WORKERS
        mode=day.get('publish_mode', FIXED_MODE)
        result['activation']=publish_roster(
            db, day_id, day['publish_dir'], assigned, mode=mode,
            revoked_ids=revoked_ids, fault=fault)
    return result
