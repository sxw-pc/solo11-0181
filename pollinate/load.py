import json
import sqlite3

TRANSPORT_SURFACE = '车辆运输安全适载'
DEFAULT_AXLE_LIMIT = 1200.0


def assess(db, ticket_id):
    # 固定副本：净重 = 毛重 - 皮重，前后轴均分。该均分算式不掌握任何托盘几何，
    # 仅代表桥秤总重口径的常规解，不得作为车辆逐轴适载结论。
    db.execute('CREATE TABLE IF NOT EXISTS trailer_load (ticket_id TEXT PRIMARY KEY, net_kg REAL NOT NULL, front_kg REAL NOT NULL, rear_kg REAL NOT NULL)')
    raw = db.execute('SELECT raw FROM weigh_ticket WHERE id=?', (ticket_id,)).fetchone()
    if not raw:
        raise ValueError('unknown ticket')
    ticket = json.loads(raw[0])
    net = float(ticket['gross_kg']) - float(ticket['tare_kg'])
    db.execute('INSERT OR REPLACE INTO trailer_load VALUES (?,?,?,?)', (ticket_id, net, net / 2, net / 2))
    db.commit()
    return net


# --------------------------------------------------------------------------
# 运输安全审计账 schema：事件流 + 版本化包络/处置/决定，全部可回放
# --------------------------------------------------------------------------
def ensure_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS transport_event ('
               'event_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, seq INTEGER NOT NULL,'
               ' received_at INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS axle_envelope ('
               'ticket_id TEXT NOT NULL, basis TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,'
               ' PRIMARY KEY(ticket_id, basis, version))')
    db.execute('CREATE TABLE IF NOT EXISTS load_disposition ('
               'ticket_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,'
               ' PRIMARY KEY(ticket_id, version))')
    db.execute('CREATE TABLE IF NOT EXISTS transport_decision ('
               'ticket_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL, proof_ids TEXT NOT NULL,'
               ' PRIMARY KEY(ticket_id, version))')
    db.commit()


def _r3(x):
    v = round(float(x), 3)
    return 0.0 if v == 0 else v


def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------
# 纯物理：托盘重心合成、静力反力、制动负荷转移
# --------------------------------------------------------------------------
def composite_cg(pallets):
    """返回 (总重, 名义重心 x, 误差下界, 误差上界)。坐标相对前轴。"""
    w = sum(float(p['kg']) for p in pallets)
    if w <= 0:
        return 0.0, None, None, None
    x = sum(float(p['kg']) * float(p['x_m']) for p in pallets) / w
    tol = sum(float(p['kg']) * float(p.get('x_tol_m', 0.0)) for p in pallets) / w
    return _r3(w), _r3(x), _r3(x - tol), _r3(x + tol)


def static_reactions(total_kg, cg_x, front_x, rear_x):
    """静力平衡：后轴 W*(x-xf)/(xr-xf)，前轴为余量。"""
    wheelbase = float(rear_x) - float(front_x)
    if wheelbase <= 0:
        raise ValueError('wheelbase must be positive')
    rear = float(total_kg) * (float(cg_x) - float(front_x)) / wheelbase
    front = float(total_kg) - rear
    return _r3(front), _r3(rear), _r3(wheelbase)


def brake_transfer(total_kg, decel_g, cg_height_m, wheelbase):
    """急刹负荷转移量 Δ = W*a*h/L，全部转向前轴。"""
    return _r3(float(total_kg) * float(decel_g) * float(cg_height_m) / float(wheelbase))


def build_envelope(basis, ticket_id, net_kg, pallets, geometry, brake, limit_kg,
                   manifest_source=None, reweigh_ids=None, event_ids=None,
                   ticket_net_kg=None):
    """带几何、托盘来源、工况范围的逐轴负荷包络。缺依据时暂停而不是放行。
    net_kg 是本 basis 本车货物基准重量；ticket_net_kg 是原秤票净重（仅作溯源）。"""
    event_ids = list(event_ids or [])
    provenance = {'ticket_id': ticket_id, 'event_ids': event_ids,
                  'manifest_source': manifest_source, 'reweigh_ids': list(reweigh_ids or []),
                  'ticket_net_kg': _r3(ticket_net_kg) if ticket_net_kg is not None else None}
    env = {'basis': basis, 'limit_kg': _r3(limit_kg), 'geometry': None,
           'cargo': None, 'static': None, 'braking': None,
           'axle_peaks_kg': None, 'status': '暂停', 'reasons': [], 'provenance': provenance}

    if geometry is None:
        env['reasons'].append('缺轴位几何依据，无法计算逐轴反力')
        return env
    front_x, rear_x = float(geometry['front_x']), float(geometry['rear_x'])
    wheelbase = rear_x - front_x
    env['geometry'] = {'front_x_m': _r3(front_x), 'rear_x_m': _r3(rear_x),
                       'wheelbase_m': _r3(wheelbase), 'source': geometry.get('source')}

    if not pallets:
        env['reasons'].append('缺托盘重心清单，桥秤总重不能替代逐轴物理')
        return env

    total, cg, cg_lo, cg_hi = composite_cg(pallets)
    mismatch = abs(total - float(net_kg)) > 0.01
    if mismatch:
        env['reasons'].append(f'托盘合计 {total}kg 与本车基准净重 {_r3(net_kg)}kg 不符')
        env['status'] = '暂停'
        env['cargo'] = {'net_kg': _r3(net_kg), 'pallet_total_kg': total,
                        'cg_x_m': cg, 'pallets': _pallet_view(pallets)}
        # 物理反力仍照算并落账，供追溯，但暂停状态优先于超限/通过
        f_nom, r_nom, _ = static_reactions(total, cg, front_x, rear_x)
        f_lo, r_hi, _ = static_reactions(total, cg_hi, front_x, rear_x)
        f_hi, r_lo, _ = static_reactions(total, cg_lo, front_x, rear_x)
        env['static'] = {'front_kg': f_nom, 'rear_kg': r_nom,
                         'front_worst_kg': _r3(max(f_nom, f_lo, f_hi)),
                         'rear_worst_kg': _r3(max(r_nom, r_lo, r_hi))}
        return env

    f_nom, r_nom, _ = static_reactions(total, cg, front_x, rear_x)
    f_lo, r_hi, _ = static_reactions(total, cg_hi, front_x, rear_x)
    f_hi, r_lo, _ = static_reactions(total, cg_lo, front_x, rear_x)
    env['cargo'] = {'net_kg': _r3(net_kg), 'pallet_total_kg': total,
                    'cg_x_m': cg, 'cg_x_min_m': cg_lo, 'cg_x_max_m': cg_hi,
                    'pallets': _pallet_view(pallets)}
    env['static'] = {'front_kg': f_nom, 'rear_kg': r_nom,
                     'front_worst_kg': _r3(max(f_nom, f_lo, f_hi)),
                     'rear_worst_kg': _r3(max(r_nom, r_lo, r_hi))}

    status, reasons = '通过', []
    axle_label = {'front': '前轴', 'rear': '后轴'}
    for axle, worst in (('front', env['static']['front_worst_kg']),
                        ('rear', env['static']['rear_worst_kg'])):
        if worst > float(limit_kg) + 1e-9:
            status = '超限'
            nom = env['static'][f'{axle}_kg']
            reasons.append(f'{axle_label[axle]}静态峰值 {worst}kg 超过上限 {_r3(limit_kg)}kg'
                           + (f'（名义 {nom}kg，坐标误差最坏值跨限）' if worst != nom else ''))

    active_brake = next((b for b in brake.values() if b.get('active')), None)
    if active_brake is None:
        # 工况依据缺失：暂停适载结论，而不是按不完整包络放行或只报超限
        status = '暂停'
        reasons.append('缺有效急刹工况依据（无记录或已撤销），制动包络空缺')
    else:
        delta = brake_transfer(total, active_brake['decel_g'],
                               active_brake['cg_height_m'], wheelbase)
        bf, br = _r3(f_nom + delta), _r3(r_nom - delta)
        bf_worst = _r3(env['static']['front_worst_kg'] + delta)
        br_worst = _r3(env['static']['rear_worst_kg'] - delta)
        env['braking'] = {'condition_id': active_brake['condition_id'],
                          'decel_g': active_brake['decel_g'],
                          'cg_height_m': active_brake['cg_height_m'],
                          'transfer_kg': delta,
                          'front_kg': bf, 'rear_kg': br,
                          'front_worst_kg': bf_worst, 'rear_worst_kg': br_worst}
        for axle, peak in (('front', bf_worst), ('rear', max(br_worst, env['static']['rear_worst_kg']))):
            if peak > float(limit_kg) + 1e-9:
                status = '超限'
                reasons.append(f'{axle_label[axle]}制动包络峰值 {peak}kg 超过上限 {_r3(limit_kg)}kg')

    env['axle_peaks_kg'] = {
        'front_kg': _r3(max(env['static']['front_worst_kg'],
                            env['braking']['front_worst_kg'] if env['braking'] else -1e18)),
        'rear_kg': _r3(max(env['static']['rear_worst_kg'],
                           env['braking']['rear_worst_kg'] if env['braking'] else -1e18))}
    env['status'] = status
    env['reasons'] = reasons
    return env


def _pallet_view(pallets):
    return [{'id': p['id'], 'kg': _r3(p['kg']), 'x_m': _r3(p['x_m']),
             'x_tol_m': _r3(p.get('x_tol_m', 0.0)),
             'boxes': int(p.get('boxes', 0))} for p in pallets]


# --------------------------------------------------------------------------
# 事件投影
# --------------------------------------------------------------------------
def _new_state():
    return {'limit': DEFAULT_AXLE_LIMIT, 'pallets': {}, 'geometry': None,
            'brakes': {}, 'reweighs': [], 'unloads': {}, 'handovers': {},
            'lashing': None, 'route': None, 'event_ids': []}


def _apply_event(state, ev):
    kind, data = ev['kind'], ev['data']
    state['event_ids'].append(ev['event_id'])
    if kind == 'config':
        state['limit'] = float(data.get('axle_limit_kg', DEFAULT_AXLE_LIMIT))
    elif kind == 'geometry':
        state['geometry'] = {'front_x': float(data['front_x']),
                             'rear_x': float(data['rear_x']), 'source': ev['event_id']}
    elif kind == 'pallets':
        for p in data['pallets']:
            old = state['pallets'].get(p['id'])
            view = {k: p[k] for k in ('id', 'kg', 'x_m')}
            view['x_tol_m'] = p.get('x_tol_m', 0.0)
            if 'boxes' in p:
                view['boxes'] = p['boxes']
            if old and any(float(old[k]) != float(view[k]) for k in ('kg', 'x_m', 'x_tol_m')) \
                    or (old and old.get('boxes') != view.get('boxes')):
                raise ValueError(f'conflicting pallet {p["id"]}')
            state['pallets'][p['id']] = view
    elif kind == 'brake':
        cid = data['condition_id']
        state['brakes'][cid] = {'condition_id': cid,
                                'cg_height_m': float(data['cg_height_m']),
                                'decel_g': float(data['decel_g']), 'active': True,
                                'source': ev['event_id']}
    elif kind == 'brake_revoke':
        cid = data['condition_id']
        if cid not in state['brakes']:
            raise ValueError('unknown brake condition ' + cid)
        if not state['brakes'][cid]['active']:
            raise ValueError('brake condition already revoked ' + cid)
        state['brakes'][cid]['active'] = False
        state['brakes'][cid]['revoke_source'] = ev['event_id']
    elif kind == 'reweigh':
        state['reweighs'].append({'reweigh_id': data['reweigh_id'],
                                  'net_kg': float(data['gross_kg']) - float(data['tare_kg']),
                                  'source': ev['event_id']})
    elif kind == 'unload':
        pid = data['pallet_id']
        if pid in state['unloads']:
            raise ValueError('pallet already unloaded ' + pid)
        state['unloads'][pid] = {'pallet_id': pid, 'kg': float(data['kg']),
                                 'x_m': float(data['x_m']),
                                 'vehicle_id': data.get('vehicle_id'),
                                 'source': ev['event_id']}
    elif kind == 'handover':
        for existing in state['handovers'].values():
            if set(existing['pallet_ids']) & set(data['pallet_ids']):
                raise ValueError('pallet already handed over to another vehicle: '
                                 + ','.join(sorted(set(existing['pallet_ids'])
                                                   & set(data['pallet_ids']))))
        state['handovers'][data['handover_id']] = {
            'handover_id': data['handover_id'], 'to_vehicle': data['to_vehicle'],
            'pallet_ids': list(data['pallet_ids']), 'kg': float(data['kg']),
            'source': ev['event_id']}
    elif kind == 'lashing':
        state['lashing'] = {'done': bool(data['done']), 'source': ev['event_id']}
    elif kind == 'route_a':
        state['route'] = {'route': 'A', 'positions': dict(data['positions']),
                          'source': ev['event_id']}
    elif kind == 'route_b':
        state['route'] = {'route': 'B', 'transfer_pallets': list(data['pallet_ids']),
                          'to_vehicle': data['to_vehicle'], 'source': ev['event_id']}
    else:
        raise ValueError('unknown transport event kind ' + kind)


def _ticket_net_kg(db, ticket_id):
    raw = db.execute('SELECT raw FROM weigh_ticket WHERE id=?', (ticket_id,)).fetchone()
    ticket = json.loads(raw[0])
    return float(ticket['gross_kg']) - float(ticket['tare_kg'])


def _net_kg(db, ticket_id, state):
    """当前有效净重：复称票晚到则以最新复称为准，否则用原票。"""
    if state['reweighs']:
        return state['reweighs'][-1]['net_kg']
    return _ticket_net_kg(db, ticket_id)


def _append_version(db, table, ticket_id, basis_cols, basis, payload):
    ensure_schema(db)
    old = db.execute(f'SELECT payload FROM {table} WHERE ticket_id=? AND {basis_cols}=? '
                     f'ORDER BY version DESC LIMIT 1', (ticket_id, basis)).fetchone()
    body = _canonical(payload)
    if old and old[0] == body:
        return None
    prev = db.execute(f'SELECT MAX(version) FROM {table} WHERE ticket_id=? AND {basis_cols}=?',
                      (ticket_id, basis)).fetchone()[0]
    version = 1 if prev is None else prev + 1
    db.execute(f'INSERT INTO {table} VALUES (?,?,?,?)', (ticket_id, basis, version, body))
    return version


def _replay_state(db, ticket_id):
    """从事件流重建纯内存投影状态（不写任何版本账）。"""
    state = _new_state()
    rows = db.execute('SELECT event_id, received_at, kind, payload FROM transport_event '
                      'WHERE ticket_id=? ORDER BY seq', (ticket_id,)).fetchall()
    for event_id, received_at, kind, payload in rows:
        _apply_event(state, {'event_id': event_id, 'received_at': received_at,
                             'kind': kind, 'data': json.loads(payload)})
    return state


def project(db, ticket_id):
    """重放本票全部运输事件，追加变化的包络/处置版本。返回当前投影。"""
    ensure_schema(db)
    state = _new_state()
    envelopes = {}
    disposition = None
    rows = db.execute('SELECT event_id, received_at, kind, payload FROM transport_event '
                      'WHERE ticket_id=? ORDER BY seq', (ticket_id,)).fetchall()
    for event_id, received_at, kind, payload in rows:
        ev = {'event_id': event_id, 'received_at': received_at,
              'kind': kind, 'data': json.loads(payload)}
        _apply_event(state, ev)
        ticket_net = _ticket_net_kg(db, ticket_id)
        current_net = _net_kg(db, ticket_id, state)
        cargo_left = bool(state['unloads']) or (state['route'] is not None
                                                 and state['route']['route'] == 'B')
        for basis, pallets in _basis_cargo(state):
            # 包络基准：有托盘离开本车时按在库托盘合计；否则跟当前有效净重（复称晚到即复称）
            basis_net = (sum(float(p['kg']) for p in pallets)
                         if cargo_left else current_net)
            env = build_envelope(basis, ticket_id, basis_net, pallets, state['geometry'],
                                 state['brakes'], state['limit'],
                                 reweigh_ids=[r['reweigh_id'] for r in state['reweighs']],
                                 event_ids=state['event_ids'],
                                 ticket_net_kg=current_net)
            _append_version(db, 'axle_envelope', ticket_id, 'basis', basis, env)
            envelopes[basis] = env
        disposition = _build_disposition(ticket_id, state, envelopes, current_net,
                                         original_ticket_net=ticket_net)
        _append_disposition(db, ticket_id, disposition)
    db.commit()
    return {'state': state, 'envelopes': envelopes, 'disposition': disposition}


def _append_disposition(db, ticket_id, disposition):
    ensure_schema(db)
    body = _canonical(disposition)
    old = db.execute('SELECT payload FROM load_disposition WHERE ticket_id=? '
                     'ORDER BY version DESC LIMIT 1', (ticket_id,)).fetchone()
    if old and old[0] == body:
        return None
    prev = db.execute('SELECT MAX(version) FROM load_disposition WHERE ticket_id=?',
                      (ticket_id,)).fetchone()[0]
    version = 1 if prev is None else prev + 1
    db.execute('INSERT INTO load_disposition VALUES (?,?,?)', (ticket_id, version, body))
    return version


def _basis_cargo(state):
    """返回 (basis, 该 basis 下本车 active 托盘) 列表。"""
    removed = set(state['unloads'])
    route = state['route']
    if route and route['route'] == 'B':
        removed |= set(route['transfer_pallets'])
    base_pallets = [p for pid, p in sorted(state['pallets'].items()) if pid not in removed]
    if route and route['route'] == 'A':
        moved = []
        for p in base_pallets:
            q = dict(p)
            if p['id'] in route['positions']:
                q['x_m'] = route['positions'][p['id']]
                q['x_tol_m'] = 0.0
            moved.append(q)
        return [('BASE', base_pallets), ('A', moved)]
    if route and route['route'] == 'B':
        return [('BASE', base_pallets), ('B', base_pallets)]
    return [('BASE', base_pallets)]


def _build_disposition(ticket_id, state, envelopes, net_kg, original_ticket_net=None):
    """net_kg：当前有效净重（复称晚到则为复称值）；original_ticket_net：原秤票净重。"""
    route = state['route']
    route_name = route['route'] if route else 'BASE'
    active_basis = route_name if route_name in envelopes else 'BASE'
    env = envelopes.get(active_basis) or envelopes.get('BASE')
    removed = set(state['unloads'])
    if route and route['route'] == 'B':
        removed |= set(route['transfer_pallets'])
    active_pallets = [p for pid, p in sorted(state['pallets'].items()) if pid not in removed]
    active_kg = sum(float(p['kg']) for p in active_pallets)

    requirements = {'relash_required': route_name == 'A',
                    'reweigh_required': route_name == 'A',
                    'handover_required': route_name == 'B'}
    satisfied = {'relash': bool(state['lashing'] and state['lashing']['done']),
                 'reweigh': bool(state['reweighs']),
                 'handover': False}

    vehicles = [{'vehicle_id': 'V1', 'kg': _r3(active_kg),
                 'pallet_ids': [p['id'] for p in active_pallets]}]
    lineage = [{'vehicle_id': 'V1', 'ticket_id': ticket_id,
                'pallet_ids': [p['id'] for p in active_pallets], 'kg': _r3(active_kg)}]

    # ---- 总量对账：原货物去向必须闭合，跨车转移不双计 ----
    original_kg = sum(float(p['kg']) for p in state['pallets'].values())
    reconciliation = {'ticket_net_kg': _r3(net_kg),
                      'original_ticket_net_kg': _r3(original_ticket_net)
                      if original_ticket_net is not None else None,
                      'original_pallet_kg': _r3(original_kg),
                      'accounted_kg': _r3(original_kg), 'closed': False, 'reasons': []}
    recon_reasons = []
    # 闭合基准：有托盘离开本车（半途卸货/跨车转移）时对原票净重闭合；
    # 全部托盘仍在本车时，对当前有效净重闭合（复称晚到即以复称为准）。
    cargo_left = bool(state['unloads']) or (route is not None and route['route'] == 'B')
    baseline_net = float(original_ticket_net) if cargo_left else float(net_kg)
    if abs(original_kg - baseline_net) > 0.01:
        note = (f'（原票 {_r3(original_ticket_net)}kg）'
                if abs(float(net_kg) - float(original_ticket_net)) > 0.01 else '')
        recon_reasons.append(f'托盘总量 {_r3(original_kg)}kg 与闭合基准净重 '
                             f'{_r3(baseline_net)}kg 不符{note}')

    if route_name == 'B':
        transferred = list(route['transfer_pallets'])
        transfer_kg = sum(float(state['pallets'][pid]['kg']) for pid in transferred)
        handover = next((h for h in state['handovers'].values()
                         if set(h['pallet_ids']) == set(transferred)), None)
        if handover is None:
            recon_reasons.append('跨车托盘缺交接凭据，不能用本车安全结论覆盖第二车交接')
        elif abs(float(handover['kg']) - transfer_kg) > 0.01:
            recon_reasons.append('交接凭据重量与转移托盘重量不符')
        if handover:
            satisfied['handover'] = True
        vehicles.append({'vehicle_id': route['to_vehicle'], 'kg': _r3(transfer_kg),
                         'pallet_ids': transferred,
                         'handover_id': handover['handover_id'] if handover else None})
        lineage.append({'vehicle_id': route['to_vehicle'], 'ticket_id': ticket_id,
                        'pallet_ids': transferred, 'kg': _r3(transfer_kg),
                        'handover_id': handover['handover_id'] if handover else None,
                        'source_vehicle': 'V1'})

    # 半途卸货：离开本车的托盘带卸货凭据进入 OFFLOAD 去向
    for pid, u in sorted(state['unloads'].items()):
        vehicles.append({'vehicle_id': 'OFFLOAD', 'kg': _r3(u['kg']),
                         'pallet_ids': [pid], 'unload_event': u['source']})
        lineage.append({'vehicle_id': 'OFFLOAD', 'ticket_id': ticket_id,
                        'pallet_ids': [pid], 'kg': _r3(u['kg']),
                        'unload_event': u['source'], 'source_vehicle': 'V1'})
    # 闭合校验：各车/去向托盘集合互不相交且合计等于原始总量
    seen = [pid for v in vehicles for pid in v['pallet_ids']]
    accounted = sum(float(v['kg']) for v in vehicles)
    if len(seen) != len(set(seen)):
        recon_reasons.append('同一托盘被重复计入多个车辆（双计）')
    if abs(accounted - original_kg) > 0.01:
        recon_reasons.append(f'去向合计 {_r3(accounted)}kg 与原始托盘总量 {_r3(original_kg)}kg 不符')
    reconciliation['closed'] = not recon_reasons
    reconciliation['reasons'] = recon_reasons

    status, reasons = _disposition_status(route_name, env, requirements, satisfied,
                                          recon_reasons)
    proof = sorted(set(state['event_ids']
                       + [ticket_id]
                       + [p['id'] for p in active_pallets]
                       + [h['handover_id'] for h in state['handovers'].values()]))
    return {'ticket_id': ticket_id, 'route': route_name, 'trips': len(vehicles),
            'status': status, 'reasons': reasons,
            'requirements': requirements, 'satisfied': satisfied,
            'vehicles': vehicles, 'cargo_lineage': lineage,
            'reconciliation': reconciliation,
            'envelope_basis': active_basis, 'proof_ids': proof}


def _disposition_status(route_name, env, requirements, satisfied, recon_reasons=()):
    if env is None:
        return '暂停', ['尚无包络结论']
    if env['status'] == '暂停':
        return '暂停', list(env['reasons']) + list(recon_reasons)
    if env['status'] == '超限':
        return '不可发-超限', list(env['reasons'])
    if recon_reasons:
        return '暂停', list(recon_reasons)
    pending = [k.replace('_required', '') for k, needed in requirements.items()
               if needed and not satisfied[k.replace('_required', '')]]
    if pending:
        labels = {'relash': '重新绑扎签认', 'reweigh': '复称票', 'handover': '跨车交接凭据'}
        return '待办-不得发运', [f'缺 {labels[k]}' for k in pending]
    return '可发', []


def latest_envelope(db, ticket_id, basis='BASE'):
    ensure_schema(db)
    row = db.execute('SELECT payload FROM axle_envelope WHERE ticket_id=? AND basis=? '
                     'ORDER BY version DESC LIMIT 1', (ticket_id, basis)).fetchone()
    return json.loads(row[0]) if row else None


def latest_disposition(db, ticket_id):
    ensure_schema(db)
    row = db.execute('SELECT payload FROM load_disposition WHERE ticket_id=? '
                     'ORDER BY version DESC LIMIT 1', (ticket_id,)).fetchone()
    return json.loads(row[0]) if row else None


def envelope_versions(db, ticket_id, basis=None):
    ensure_schema(db)
    if basis:
        rows = db.execute('SELECT basis, version, payload FROM axle_envelope '
                          'WHERE ticket_id=? AND basis=? ORDER BY version', (ticket_id, basis)).fetchall()
    else:
        rows = db.execute('SELECT basis, version, payload FROM axle_envelope '
                          'WHERE ticket_id=? ORDER BY basis, version', (ticket_id,)).fetchall()
    return [(b, v, json.loads(p)) for b, v, p in rows]


def replay_transport(db, ticket_id):
    """从原始秤票与运输事件流在空库中重建全部账册，逐表比对。"""
    ensure_schema(db)
    raw = db.execute('SELECT raw FROM weigh_ticket WHERE id=?', (ticket_id,)).fetchone()
    if not raw:
        raise ValueError('unknown ticket')
    events = db.execute('SELECT event_id, ticket_id, seq, received_at, kind, payload '
                        'FROM transport_event WHERE ticket_id=? ORDER BY seq', (ticket_id,)).fetchall()
    tmp = sqlite3.connect(':memory:')
    from .weigh import import_ticket, capture
    from .dispatch import transport_decide
    import_ticket(tmp, json.loads(raw[0]))
    for event_id, _, seq, received_at, kind, payload in events:
        capture(tmp, ticket_id, kind, json.loads(payload), event_id, received_at, _seq=seq)
    # 若原账已有运输安全决定，则在重建账上重放同一决定，保证可回放
    if db.execute('SELECT COUNT(*) FROM transport_decision WHERE ticket_id=?',
                  (ticket_id,)).fetchone()[0]:
        transport_decide(tmp, ticket_id)

    def snapshot(conn):
        # 以 JSON 反标准化后再比对，消除 1200/1200.0 之类的类型差异
        out = {}
        for table in ('weigh_ticket', 'trailer_load', 'transport_event',
                      'axle_envelope', 'load_disposition', 'transport_decision'):
            try:
                out[table] = sorted(
                    tuple(json.loads(c) if isinstance(c, str) and c[:1] in '{[' else c
                          for c in row)
                    for row in conn.execute(f'SELECT * FROM {table}').fetchall())
            except sqlite3.OperationalError:
                out[table] = []
        return out

    want, got = snapshot(db), snapshot(tmp)
    mismatches = {t: {'stored': want[t], 'replayed': got[t]} for t in want if want[t] != got[t]}
    return {'match': not mismatches, 'mismatches': mismatches, 'event_count': len(events)}
