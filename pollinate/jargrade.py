"""Per-jar release determination.

Combines the solved mass-flow graph (machine path) with per-jar sealed
lab receipts (lab path) into versioned jar decisions. The blend-wide
provisional grade is never pushed down onto jars, a lab result never
extends past the sampled jar, and neither path rewrites the blend
worksheet.
"""
import json

from .flowgraph import solve_run, status_for_ppm
from . import inventory


def _blend_doc(db, blend_id):
    row = db.execute('SELECT raw_json FROM honey_blend WHERE id=?', (blend_id,)).fetchone()
    if row is None:
        raise ValueError('unknown blend ' + repr(blend_id))
    return json.loads(row['raw_json'])


def _solve_runs(db, blend_id):
    blend_doc = _blend_doc(db, blend_id)
    solved = {}
    for run in db.execute('SELECT id FROM fill_run WHERE blend_id=? ORDER BY id', (blend_id,)):
        events = [
            {'seq': r['seq'], 'kind': r['kind'], 'valid': bool(r['valid']), 'data': json.loads(r['data'])}
            for r in db.execute('SELECT seq, kind, valid, data FROM fill_event WHERE run_id=? ORDER BY seq', (run['id'],))
        ]
        solved[run['id']] = solve_run(blend_doc, run['id'], events)
    return solved


def _lab_reports(db, blend_id):
    """Split lab receipts into the latest valid report per jar and the
    ids that were voided (revoked) or never valid (unsealed sample)."""
    results, revoked = {}, set()
    for row in db.execute('SELECT id, jar, kind, received_at, raw_json FROM lab_report WHERE blend_id=? ORDER BY received_at, id', (blend_id,)):
        doc = json.loads(row['raw_json'])
        if row['kind'] == 'revocation':
            revoked.add(doc['revokes'])
        else:
            results[row['id']] = {'id': row['id'], 'jar': row['jar'], 'received_at': row['received_at'], 'doc': doc}
    valid, had = {}, set()
    for rid, rep in results.items():
        had.add(rep['jar'])
        if rid in revoked:
            continue
        sample = rep['doc'].get('sample') or {}
        if not sample.get('sealed'):
            continue
        rep['ppm'] = round(sample['residue_ug'] / sample['mass_g'], 4)
        if rep['jar'] not in valid or (rep['received_at'], rid) > (valid[rep['jar']]['received_at'], valid[rep['jar']]['id']):
            valid[rep['jar']] = rep
    return valid, had


def compute_decisions(db, blend_id):
    """Recompute the would-be current decision for every jar of the blend."""
    solved = _solve_runs(db, blend_id)
    valid_lab, had_lab = _lab_reports(db, blend_id)
    origin = {}
    for row in db.execute('SELECT jar, run_id FROM jar_origin'):
        origin.setdefault(row['jar'], set()).add(row['run_id'])
    jar_names = set(valid_lab) | set(had_lab)
    for summary in solved.values():
        jar_names |= set(summary['jars'])
    out = {}
    for jar in sorted(jar_names):
        runs_with = [rid for rid in sorted(solved) if jar in solved[rid]['jars']]
        defects = []
        flow_status = flow_ppm = None
        if len(origin.get(jar, ())) > 1:
            defects.append('duplicate_jar')
        if len(runs_with) == 1:
            summary = solved[runs_with[0]]
            jar_flow = summary['jars'][jar]
            defects.extend(summary['defects'])
            defects.extend(jar_flow['defects'])
            if jar_flow['proven'] and not defects:
                flow_ppm = jar_flow['ppm']
                flow_status = status_for_ppm(flow_ppm)
        lab = valid_lab.get(jar)
        lab_status = status_for_ppm(lab['ppm']) if lab else None
        if lab is None and jar in had_lab:
            defects.append('no_valid_lab_report')
        if flow_status and lab_status:
            if flow_status == lab_status:
                status, basis, ppm = flow_status, 'flow+lab', flow_ppm
            else:
                status, basis, ppm = 'unverified', 'conflict', flow_ppm
                defects.append('flow_lab_conflict')
        elif flow_status:
            status, basis, ppm = flow_status, 'flow', flow_ppm
        elif lab_status:
            status, basis, ppm = lab_status, 'lab', lab['ppm']
        else:
            status, basis, ppm = 'unverified', 'none', None
        out[jar] = {
            'jar': jar, 'blend_id': blend_id, 'status': status, 'ppm': ppm,
            'basis': basis, 'defects': sorted(set(defects)),
            'evidence': {'runs': runs_with, 'labs': [lab['id']] if lab else []},
        }
    return out


def _persist_decisions(db, blend_id, computed):
    for jar, c in computed.items():
        packed_defects = json.dumps(c['defects'], sort_keys=True)
        packed_evidence = json.dumps(c['evidence'], sort_keys=True)
        old = db.execute('SELECT version, status, ppm, basis, defects, evidence FROM jar_decision WHERE jar=? ORDER BY version DESC LIMIT 1', (jar,)).fetchone()
        if old and (old['status'], old['ppm'], old['basis'], old['defects'], old['evidence']) == (c['status'], c['ppm'], c['basis'], packed_defects, packed_evidence):
            continue
        version = 1 if old is None else old['version'] + 1
        db.execute('INSERT INTO jar_decision VALUES (?,?,?,?,?,?,?,?)',
                   (jar, version, blend_id, c['status'], c['ppm'], c['basis'], packed_defects, packed_evidence))


def _decide_and_sync(db, blend_id):
    """Recompute, persist new decision versions and derive inventory states.
    Caller owns the transaction."""
    computed = compute_decisions(db, blend_id)
    _persist_decisions(db, blend_id, computed)
    for jar, c in computed.items():
        inventory._sync_one(db, jar, quality=c['status'])
    return computed


def decide_jars(db, blend_id):
    with db:
        return _decide_and_sync(db, blend_id)


def current_decisions(db, blend_id):
    rows = db.execute(
        'SELECT d.* FROM jar_decision d JOIN (SELECT jar, MAX(version) v FROM jar_decision GROUP BY jar) x '
        'ON d.jar=x.jar AND d.version=x.v WHERE d.blend_id=? ORDER BY d.jar', (blend_id,)).fetchall()
    return {r['jar']: {'jar': r['jar'], 'version': r['version'], 'status': r['status'],
                       'ppm': r['ppm'], 'basis': r['basis'],
                       'defects': json.loads(r['defects']), 'evidence': json.loads(r['evidence'])}
            for r in rows}


def _provenance(db, blend_id):
    out = {}
    for run_id, summary in _solve_runs(db, blend_id).items():
        for edge in summary['edges']:
            out.setdefault(edge['jar'], []).append(
                {'run_id': run_id, 'source': edge['source'], 'grams': edge['grams'],
                 'residue_ug': edge['residue_ug'], 'proven': edge['proven']})
    return out


def release_board(db, blend_id):
    """Market-facing board: the untouched batch provisional plus the
    per-jar release list with provenance and inventory state."""
    grade = db.execute('SELECT mean_ppm, status FROM honey_grade WHERE blend_id=?', (blend_id,)).fetchone()
    decisions = current_decisions(db, blend_id)
    inv = inventory.current_inventory(db)
    sources = _provenance(db, blend_id)
    jars, saleable = [], []
    for jar, d in sorted(decisions.items()):
        state = inv.get(jar, {}).get('state', 'frozen')
        if state == 'saleable':
            saleable.append(jar)
        jars.append({'jar': jar, 'status': d['status'], 'ppm': d['ppm'], 'basis': d['basis'],
                     'defects': d['defects'], 'version': d['version'], 'inventory': state,
                     'sources': sources.get(jar, []), 'saleable': state == 'saleable'})
    return {
        'blend_id': blend_id,
        'batch_provisional': {'mean_ppm': grade['mean_ppm'], 'status': grade['status']} if grade else None,
        'jars': jars,
        'saleable': saleable,
    }
