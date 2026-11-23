import hashlib
import hmac
import json


def sign_ticket(ticket, secret):
    """对秤票规范 JSON 做 HMAC 签认，返回副本（含 signature）。"""
    signed = dict(ticket)
    body = json.dumps({k: v for k, v in sorted(ticket.items()) if k != 'signature'},
                      ensure_ascii=False, sort_keys=True)
    signed['signature'] = hmac.new(secret.encode('utf-8'), body.encode('utf-8'),
                                   hashlib.sha256).hexdigest()
    return signed


def verify_ticket(ticket, secret=None):
    """票据验签：无 secret 时只验结构；有 secret 时必须通过 HMAC。"""
    sig = ticket.get('signature')
    if secret is None:
        return bool(sig)
    body = json.dumps({k: v for k, v in sorted(ticket.items()) if k != 'signature'},
                      ensure_ascii=False, sort_keys=True)
    want = hmac.new(secret.encode('utf-8'), body.encode('utf-8'),
                    hashlib.sha256).hexdigest()
    return bool(sig) and hmac.compare_digest(want, sig)


def import_ticket(db, ticket, secret=None):
    """固定副本路径：原始称重票原样落库（去重/冲突），再走 assess 均分解。
    提供 secret 时强制 HMAC 验签；不提供时保留固定副本的无签兼容。"""
    if secret is not None and not verify_ticket(ticket, secret):
        raise ValueError('ticket signature verification failed')
    db.execute('CREATE TABLE IF NOT EXISTS weigh_ticket (id TEXT PRIMARY KEY, raw TEXT NOT NULL)')
    raw = json.dumps(ticket, ensure_ascii=False, sort_keys=True)
    old = db.execute('SELECT raw FROM weigh_ticket WHERE id=?', (ticket['id'],)).fetchone()
    if old and old[0] != raw:
        raise ValueError('conflicting ticket')
    if not old:
        db.execute('INSERT INTO weigh_ticket VALUES (?,?)', (ticket['id'], raw))
    db.commit()
    from .load import assess
    return assess(db, ticket['id'])


def capture(db, ticket_id, kind, data, event_id, received_at, _seq=None):
    """运输安全审计账采录：同一事件 ID 幂等，内容冲突拒绝，未知票拒绝。"""
    from .load import ensure_schema, project
    ensure_schema(db)
    if not db.execute('SELECT 1 FROM weigh_ticket WHERE id=?', (ticket_id,)).fetchone():
        raise ValueError('unknown ticket')
    body = json.dumps(data, ensure_ascii=False, sort_keys=True)
    old = db.execute('SELECT kind, payload FROM transport_event WHERE event_id=?',
                     (event_id,)).fetchone()
    if old:
        if tuple(old) != (kind, body):
            raise ValueError('conflicting transport event ' + event_id)
        return False
    seq = _seq if _seq is not None else (
        db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM transport_event WHERE ticket_id=?',
                   (ticket_id,)).fetchone()[0])
    try:
        # 先在纯内存状态上试应用新事件做业务校验（任何冲突都整体回滚，不留半截事件），
        # 再落事件并提交；提交后重新投影写版本，避免回滚抹掉旧版本。
        from .load import project, _replay_state, _apply_event
        probe = _replay_state(db, ticket_id)
        _apply_event(probe, {'event_id': event_id, 'kind': kind,
                             'received_at': received_at, 'data': data})
        db.execute('INSERT INTO transport_event VALUES (?,?,?,?,?,?)',
                   (event_id, ticket_id, seq, int(received_at), kind, body))
        db.commit()
    except Exception:
        db.rollback()
        raise
    project(db, ticket_id)
    return True


# 便捷采录器（均以事件方式入账，可回放、可去重）
def record_geometry(db, ticket_id, front_x, rear_x, event_id, received_at):
    return capture(db, ticket_id, 'geometry',
                   {'front_x': front_x, 'rear_x': rear_x}, event_id, received_at)


def record_pallets(db, ticket_id, pallets, event_id, received_at):
    return capture(db, ticket_id, 'pallets', {'pallets': pallets}, event_id, received_at)


def record_brake(db, ticket_id, condition_id, cg_height_m, decel_g, event_id, received_at):
    return capture(db, ticket_id, 'brake',
                   {'condition_id': condition_id, 'cg_height_m': cg_height_m,
                    'decel_g': decel_g}, event_id, received_at)


def revoke_brake(db, ticket_id, condition_id, event_id, received_at):
    return capture(db, ticket_id, 'brake_revoke',
                   {'condition_id': condition_id}, event_id, received_at)


def record_reweigh(db, ticket_id, reweigh_id, gross_kg, tare_kg, event_id, received_at):
    return capture(db, ticket_id, 'reweigh',
                   {'reweigh_id': reweigh_id, 'gross_kg': gross_kg,
                    'tare_kg': tare_kg}, event_id, received_at)


def record_unload(db, ticket_id, pallet_id, kg, x_m, event_id, received_at, vehicle_id=None):
    return capture(db, ticket_id, 'unload',
                   {'pallet_id': pallet_id, 'kg': kg, 'x_m': x_m,
                    'vehicle_id': vehicle_id}, event_id, received_at)


def record_handover(db, ticket_id, handover_id, to_vehicle, pallet_ids, kg,
                    event_id, received_at):
    return capture(db, ticket_id, 'handover',
                   {'handover_id': handover_id, 'to_vehicle': to_vehicle,
                     'pallet_ids': pallet_ids, 'kg': kg}, event_id, received_at)


def record_lashing(db, ticket_id, done, event_id, received_at):
    return capture(db, ticket_id, 'lashing', {'done': done}, event_id, received_at)


def route_a(db, ticket_id, positions, event_id, received_at):
    return capture(db, ticket_id, 'route_a',
                   {'positions': positions}, event_id, received_at)


def route_b(db, ticket_id, pallet_ids, to_vehicle, event_id, received_at):
    return capture(db, ticket_id, 'route_b',
                   {'pallet_ids': pallet_ids, 'to_vehicle': to_vehicle},
                   event_id, received_at)


def set_axle_limit(db, ticket_id, limit_kg, event_id, received_at):
    return capture(db, ticket_id, 'config',
                   {'axle_limit_kg': limit_kg}, event_id, received_at)


def _play(db, ticket_id, events):
    for ev in events:
        capture(db, ticket_id, ev['kind'], ev['data'],
                ev['event_id'], ev['received_at'])


def run_l2_case(fixture_path):
    """沿固定副本 import_ticket->assess->trailer_load 路径采录 L2，并在三条独立库上
    回放 BASE/路线A/路线B，返回各自的常规解、包络、处置与运输安全决定。"""
    import json as _json
    import sqlite3 as _sqlite3
    from .dispatch import transport_decide, gross_only_view
    from .load import latest_envelope, latest_disposition, project
    with open(fixture_path, encoding='utf-8') as fh:
        fixture = _json.load(fh)
    secret = fixture.get('secret')
    ticket = fixture['ticket']
    if not verify_ticket(ticket, secret):
        raise ValueError('L2 fixture ticket signature invalid')

    def fresh_db(extra_events):
        db = _sqlite3.connect(':memory:')
        import_ticket(db, ticket, secret=secret)
        _play(db, ticket['id'], fixture['shared_events'])
        if extra_events:
            _play(db, ticket['id'], extra_events)
        project(db, ticket['id'])
        return db

    base_db = fresh_db([])
    a_db = fresh_db(fixture['route_a_events'])
    b_db = fresh_db(fixture['route_b_events'])
    out = {}
    for name, db in (('BASE', base_db), ('A', a_db), ('B', b_db)):
        basis = name if name != 'BASE' else 'BASE'
        decision = transport_decide(db, ticket['id'])
        out[name] = {
            'gross_only_view': gross_only_view(db, ticket['id']),
            'envelope': latest_envelope(db, ticket['id'], basis),
            'disposition': latest_disposition(db, ticket['id']),
            'transport_decision': decision}
    return out

