from .common import rows,latest,proof,placement_hours
from .load import TRANSPORT_SURFACE
from pathlib import Path
COMMAND='dispatch';KINDS=['hive','placement','health_check']
NORMAL=str(Path(__file__).resolve().parent.parent/'fixtures/dispatch_ok.json')
FAILURE='fixtures/dispatch_sick.json'
def decide(ev):
    h=latest(ev,'health_check');placements=rows(ev,'placement');sprays=rows(ev,'spray_notice')
    healthy=bool(h and h['data'].get('healthy') and not h['data'].get('dead'))
    status='放行' if healthy else '拒绝'
    raw=sum(max(0,float(p['data'].get('end',0))-float(p['data'].get('start',0))) for p in placements)
    total,zones,ids=placement_hours(ev)
    replacements=[p for p in placements if p['data'].get('replacement')]
    parent=[p['data'].get('replaces') for p in replacements]
    valid=bool(not replacements or (all(parent) and len(parent)==len(set(parent)) and all(x in {p['id'] for p in placements} for x in parent)))
    dead=[x for x in rows(ev,'health_check') if x['data'].get('dead')]
    responsible=any(s['data'].get('delivered') and d['data'].get('death_time',d['event_time'])>=s['data'].get('start',s['event_time']) and d['data'].get('death_time',d['event_time'])<s['data'].get('end',10**12) for d in dead for s in sprays)
    return [('蜂箱检疫/健康放行',status,proof([h])),('驻场时段认定',raw,proof(placements)),('蜂箱替补额度分配','继承' if valid else '拒绝双计',proof(replacements) or ids),('蜂群损失事故归因','果园方' if responsible else '待调查' if dead else '无事故',proof(dead,sprays) or ids)]


def transport_decide(db, ticket_id):
    """运输安全独立审计账：依据逐轴静力/制动包络与处置版本下车辆适载结论。
    与“蜂箱检疫/健康放行”完全分离——健康放行不证明车辆适载。"""
    from .load import (ensure_schema, latest_envelope, latest_disposition,
                       _canonical)
    ensure_schema(db)
    disposition = latest_disposition(db, ticket_id)
    base = latest_envelope(db, ticket_id, 'BASE')
    if disposition is None or base is None:
        return None
    basis = disposition['envelope_basis']
    envelope = latest_envelope(db, ticket_id, basis)
    if basis != 'BASE':
        envelope = dict(envelope)
        envelope['base_envelope_summary'] = {
            'status': base['status'],
            'axle_peaks_kg': base['axle_peaks_kg'],
            'reasons': base['reasons']}
    result = {'surface': TRANSPORT_SURFACE, 'route': disposition['route'],
              'trips': disposition['trips'], 'disposition_status': disposition['status'],
              'envelope_basis': basis, 'envelope': envelope,
              'vehicles': disposition['vehicles'],
              'cargo_lineage': disposition['cargo_lineage'],
              'requirements': disposition['requirements'],
              'satisfied': disposition['satisfied'],
              'health_release_is_not_roadworthiness': True}
    proof_ids = sorted(set(disposition['proof_ids'] + envelope['provenance']['event_ids']))
    body = _canonical(result)
    old = db.execute('SELECT payload, proof_ids FROM transport_decision WHERE ticket_id=? '
                     'ORDER BY version DESC LIMIT 1', (ticket_id,)).fetchone()
    if old and old[0] == body and old[1] == _canonical(proof_ids):
        return {'version': db.execute('SELECT MAX(version) FROM transport_decision '
                                      'WHERE ticket_id=?', (ticket_id,)).fetchone()[0],
                'result': result, 'proof_ids': proof_ids}
    prev = db.execute('SELECT MAX(version) FROM transport_decision WHERE ticket_id=?',
                      (ticket_id,)).fetchone()[0]
    version = 1 if prev is None else prev + 1
    db.execute('INSERT INTO transport_decision VALUES (?,?,?,?)',
               (ticket_id, version, body, _canonical(proof_ids)))
    db.commit()
    return {'version': version, 'result': result, 'proof_ids': proof_ids}


def transport_report(db, ticket_id):
    from .load import ensure_schema
    ensure_schema(db)
    row = db.execute('SELECT version, payload, proof_ids FROM transport_decision '
                     'WHERE ticket_id=? ORDER BY version DESC LIMIT 1', (ticket_id,)).fetchone()
    if not row:
        return None
    import json as _json
    return {'version': row[0], 'result': _json.loads(row[1]),
            'proof_ids': _json.loads(row[2])}


def gross_only_view(db, ticket_id):
    """桥秤总重+均分口径的常规解：只回答总重与 900/900，不含任何几何信息。
    用于显式证明该口径会把 L2 误判为可发。"""
    import json as _json
    raw = db.execute('SELECT raw FROM weigh_ticket WHERE id=?', (ticket_id,)).fetchone()
    if not raw:
        raise ValueError('unknown ticket')
    ticket = _json.loads(raw[0])
    row = db.execute('SELECT net_kg, front_kg, rear_kg FROM trailer_load WHERE ticket_id=?',
                     (ticket_id,)).fetchone()
    net, front, rear = row
    limit_sum = 2400.0
    return {'ticket_id': ticket_id, 'gross_kg': ticket['gross_kg'],
            'tare_kg': ticket['tare_kg'], 'net_kg': net,
            'equal_split_kg': {'front_kg': front, 'rear_kg': rear},
            'combined_axle_capacity_kg': limit_sum,
            'gross_only_verdict': '常规解误放行' if net < limit_sum else '总重超限',
            'missing': ['轴位几何', '托盘重心', '制动工况']}

