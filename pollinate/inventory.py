"""Jar-level inventory: shipments and returns are operator movements;
hold/freeze/recall are derived per finished jar from the current quality
decision -- never by flipping the blend-wide provisional grade.

States: saleable / sold / held / frozen / recalled. jar_inventory is an
append-only versioned materialization; replay_inventory recomputes both
decisions and states from the raw evidence and compares.
"""

LOCATION = {'ship': 'shipped', 'return': 'in_stock'}
STATE = {
    ('in_stock', 'release'): 'saleable',
    ('in_stock', 'hold'): 'held',
    ('in_stock', 'unverified'): 'frozen',
    ('shipped', 'release'): 'sold',
    ('shipped', 'hold'): 'recalled',
    ('shipped', 'unverified'): 'recalled',
}


def _location(db, jar):
    location = 'in_stock'
    for row in db.execute('SELECT action FROM jar_movement WHERE jar=? ORDER BY seq', (jar,)):
        location = LOCATION.get(row['action'], location)
    return location


def _latest_quality(db, jar):
    row = db.execute('SELECT status FROM jar_decision WHERE jar=? ORDER BY version DESC LIMIT 1', (jar,)).fetchone()
    return row['status'] if row else 'unverified'


def _latest_inventory(db, jar):
    return db.execute('SELECT * FROM jar_inventory WHERE jar=? ORDER BY version DESC LIMIT 1', (jar,)).fetchone()


def _sync_one(db, jar, quality=None):
    """Materialize the derived state for one jar; bump the version on change."""
    location = _location(db, jar)
    quality = quality if quality is not None else _latest_quality(db, jar)
    state = STATE[(location, quality)]
    old = _latest_inventory(db, jar)
    if old and (old['state'], old['location'], old['quality']) == (state, location, quality):
        return state
    version = 1 if old is None else old['version'] + 1
    db.execute('INSERT INTO jar_inventory VALUES (?,?,?,?,?)', (jar, version, state, location, quality))
    return state


def current_inventory(db):
    rows = db.execute(
        'SELECT i.* FROM jar_inventory i JOIN (SELECT jar, MAX(version) v FROM jar_inventory GROUP BY jar) x '
        'ON i.jar=x.jar AND i.version=x.v ORDER BY i.jar').fetchall()
    return {r['jar']: {'state': r['state'], 'location': r['location'],
                       'quality': r['quality'], 'version': r['version']} for r in rows}


def _move(db, jar, action, at, note):
    seq_row = db.execute('SELECT MAX(seq) s FROM jar_movement WHERE jar=?', (jar,)).fetchone()
    seq = 1 if seq_row['s'] is None else seq_row['s'] + 1
    db.execute('INSERT INTO jar_movement VALUES (?,?,?,?,?)', (jar, seq, action, int(at), note))
    return _sync_one(db, jar)


def ship_jar(db, jar, at, note=None):
    """Record a sale. Only a currently saleable jar may leave the warehouse."""
    with db:
        cur = _latest_inventory(db, jar)
        if cur is None or cur['state'] != 'saleable':
            raise ValueError('jar %s is not saleable (state %s)' % (jar, cur['state'] if cur else 'unknown'))
        return _move(db, jar, 'ship', at, note)


def return_jar(db, jar, at, note=None):
    """Record a customer/warehouse return of a shipped jar."""
    with db:
        cur = _latest_inventory(db, jar)
        if cur is None or cur['location'] != 'shipped':
            raise ValueError('jar %s is not shipped' % jar)
        return _move(db, jar, 'return', at, note)


def replay_inventory(db, blend_id):
    """Recompute jar decisions from the raw logs/receipts and inventory
    states from the movement log; compare with the persisted versions."""
    from .jargrade import compute_decisions
    import json
    computed = compute_decisions(db, blend_id)
    decision_mismatches = {}
    inventory_mismatches = {}
    for jar, c in computed.items():
        old = db.execute('SELECT * FROM jar_decision WHERE jar=? ORDER BY version DESC LIMIT 1', (jar,)).fetchone()
        stored = None if old is None else {
            'status': old['status'], 'ppm': old['ppm'], 'basis': old['basis'],
            'defects': json.loads(old['defects']), 'evidence': json.loads(old['evidence'])}
        want = {'status': c['status'], 'ppm': c['ppm'], 'basis': c['basis'],
                'defects': c['defects'], 'evidence': c['evidence']}
        if stored != want:
            decision_mismatches[jar] = {'stored': stored, 'recomputed': want}
        want_state = STATE[(_location(db, jar), c['status'])]
        cur = _latest_inventory(db, jar)
        stored_state = cur['state'] if cur else None
        if stored_state != want_state:
            inventory_mismatches[jar] = {'stored': stored_state, 'recomputed': want_state}
    return {'match': not decision_mismatches and not inventory_mismatches,
            'decision_mismatches': decision_mismatches,
            'inventory_mismatches': inventory_mismatches}
