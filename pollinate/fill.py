"""Ingest offline filling-machine logs: valve switches, jar numbers, pump
pulses and scale weighings, each event signed by the machine key.

The raw log is preserved verbatim as evidence; a parsed copy with
per-event signature validity feeds the mass-flow solve. Structural
problems raise; semantic problems (lost pulses, scale mismatch, ...)
are recorded as defects and leave the affected jars unverified.
"""
import hashlib
import hmac
import json
from pathlib import Path

from .flowgraph import solve_run

EVENT_KINDS = ('valve', 'jar', 'pulse', 'weigh')


def sign_event(key, run_id, event):
    """HMAC-SHA256 over the canonical event body (everything but 'sig')."""
    body = {k: v for k, v in event.items() if k != 'sig'}
    payload = json.dumps({'run_id': run_id, **body}, sort_keys=True, separators=(',', ':'))
    return hmac.new(key.encode('utf8'), payload.encode('utf8'), hashlib.sha256).hexdigest()


def verify_event(key, run_id, event):
    return hmac.compare_digest(sign_event(key, run_id, event), str(event.get('sig', '')))


def _validate_event(event):
    if not isinstance(event, dict):
        raise ValueError('event must be an object')
    if not isinstance(event.get('seq'), int) or event['seq'] < 1:
        raise ValueError('event needs integer seq >= 1')
    kind = event.get('kind')
    if kind not in EVENT_KINDS:
        raise ValueError('unknown event kind ' + repr(kind))
    if not isinstance(event.get('sig'), str):
        raise ValueError('event missing sig')
    if kind == 'valve' and not event.get('source'):
        raise ValueError('valve event missing source')
    if kind == 'jar' and not event.get('jar'):
        raise ValueError('jar event missing jar')
    if kind == 'pulse':
        if not event.get('source') or not event.get('jar'):
            raise ValueError('pulse event missing source/jar')
        if not isinstance(event.get('grams'), int) or event['grams'] <= 0:
            raise ValueError('pulse event needs integer grams > 0')
    if kind == 'weigh':
        if not event.get('jar'):
            raise ValueError('weigh event missing jar')
        if not isinstance(event.get('grams'), (int, float)) or event['grams'] < 0:
            raise ValueError('weigh event needs grams >= 0')


def import_run(db, file, machine_keys):
    """Store a machine log, verify signatures, solve the flow graph and
    re-decide the blend's jars. machine_keys maps machine id -> HMAC key."""
    raw = Path(file).read_text(encoding='utf8')
    doc = json.loads(raw)
    run_id = doc.get('run_id')
    blend_id = doc.get('blend_id')
    machine = doc.get('machine')
    events = doc.get('events')
    if not run_id or not blend_id or not machine or not isinstance(events, list) or not events:
        raise ValueError('run log needs run_id, blend_id, machine and a non-empty events list')
    if machine not in machine_keys:
        raise ValueError('no key registered for machine ' + repr(machine))
    blend_row = db.execute('SELECT raw_json FROM honey_blend WHERE id=?', (blend_id,)).fetchone()
    if blend_row is None:
        raise ValueError('unknown blend ' + repr(blend_id))
    key = machine_keys[machine]
    parsed = []
    for event in events:
        _validate_event(event)
        parsed.append({
            'seq': event['seq'],
            'kind': event['kind'],
            'valid': verify_event(key, run_id, event),
            'data': {k: v for k, v in event.items() if k not in ('seq', 'kind', 'sig')},
        })
    blend_doc = json.loads(blend_row['raw_json'])
    old = db.execute('SELECT raw_json FROM fill_run WHERE id=?', (run_id,)).fetchone()
    if old is not None:
        if json.loads(old['raw_json']) != doc:
            raise ValueError('conflicting run id ' + run_id)
        return solve_run(blend_doc, run_id, parsed)  # identical re-import: no-op
    summary = solve_run(blend_doc, run_id, parsed)
    from .jargrade import _decide_and_sync
    with db:
        db.execute('INSERT INTO fill_run VALUES (?,?,?,?,?)',
                   (run_id, blend_id, machine, int(doc.get('received_at', 0)), raw))
        for event in parsed:
            db.execute('INSERT INTO fill_event VALUES (?,?,?,?,?)',
                       (run_id, event['seq'], event['kind'], int(event['valid']),
                        json.dumps(event['data'], ensure_ascii=False, sort_keys=True)))
        for jar in summary['jars']:
            db.execute('INSERT OR IGNORE INTO jar_origin VALUES (?,?,?)', (jar, run_id, blend_id))
        _decide_and_sync(db, blend_id)
    return summary
