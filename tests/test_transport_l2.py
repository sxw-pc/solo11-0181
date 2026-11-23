import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pollinate.weigh import (import_ticket, capture, sign_ticket, verify_ticket,
                             record_geometry, record_pallets, record_brake,
                             revoke_brake, record_reweigh, record_unload,
                             record_handover, record_lashing, route_a, route_b,
                             set_axle_limit, run_l2_case)
from pollinate.load import (assess, composite_cg, static_reactions,
                            brake_transfer, build_envelope, project,
                            latest_envelope, latest_disposition,
                            envelope_versions, replay_transport)
from pollinate.dispatch import (decide, transport_decide, transport_report,
                                gross_only_view, TRANSPORT_SURFACE)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'l2_transport.json'
TICKET = {'id': 'W1', 'gross_kg': 4000, 'tare_kg': 2200}
SECRET = 'l2-hmac-secret'
LIMIT = 1200.0


def signed_ticket():
    return sign_ticket(dict(TICKET), SECRET)


def base_db(events=True, brake=True):
    db = sqlite3.connect(':memory:')
    import_ticket(db, signed_ticket(), secret=SECRET)
    if not events:
        return db
    set_axle_limit(db, 'W1', LIMIT, 'L2-config-1', 20)
    record_geometry(db, 'W1', 0.0, 3.0, 'L2-geo-1', 21)
    record_pallets(db, 'W1', [
        {'id': 'P1', 'kg': 600, 'x_m': 2.4, 'boxes': 20},
        {'id': 'P2', 'kg': 600, 'x_m': 2.7, 'boxes': 20},
        {'id': 'P3', 'kg': 600, 'x_m': 3.0, 'boxes': 20}], 'L2-pallets-1', 22)
    if brake:
        record_brake(db, 'W1', 'B1', 1.2, 0.35, 'L2-brake-1', 23)
    return db


class L2FrozenConventionalPathTests(unittest.TestCase):
    """固定副本：W1 旧结果保留，总重+均分常规解在 L2 上被证明错误放行。"""

    def test_w1_frozen_1800_and_equal_split_900_900(self):
        db = base_db()
        self.assertEqual(assess(db, 'W1'), 1800)
        row = db.execute('SELECT net_kg, front_kg, rear_kg FROM trailer_load').fetchone()
        self.assertEqual(tuple(row), (1800.0, 900.0, 900.0))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM weigh_ticket').fetchone()[0], 1)

    def test_repeated_import_is_one_ticket_and_still_900_900(self):
        db = sqlite3.connect(':memory:')
        t = signed_ticket()
        self.assertEqual(import_ticket(db, t, secret=SECRET), 1800)
        self.assertEqual(import_ticket(db, t, secret=SECRET), 1800)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM weigh_ticket').fetchone()[0], 1)
        self.assertEqual(tuple(db.execute('SELECT front_kg,rear_kg FROM trailer_load').fetchone()),
                         (900.0, 900.0))

    def test_signature_gate_rejects_unsigned_and_forged_ticket(self):
        db = sqlite3.connect(':memory:')
        with self.assertRaises(ValueError):
            import_ticket(db, dict(TICKET), secret=SECRET)
        forged = sign_ticket(dict(TICKET), SECRET)
        forged['gross_kg'] = 4001
        self.assertFalse(verify_ticket(forged, SECRET))
        with self.assertRaises(ValueError):
            import_ticket(db, forged, secret=SECRET)

    def test_gross_only_verdict_wrongly_passes_l2(self):
        db = base_db()
        view = gross_only_view(db, 'W1')
        self.assertEqual(view['net_kg'], 1800)
        self.assertEqual(view['equal_split_kg'], {'front_kg': 900.0, 'rear_kg': 900.0})
        self.assertLess(view['net_kg'], view['combined_axle_capacity_kg'])
        self.assertEqual(view['gross_only_verdict'], '常规解误放行')
        self.assertEqual(view['missing'], ['轴位几何', '托盘重心', '制动工况'])


class L2PhysicalEnvelopeTests(unittest.TestCase):
    """L2 核心断言：逐轴静力 180/1620、制动 432/1368、转移 252、超限 420/168。"""

    def setUp(self):
        self.db = base_db()
        self.env = latest_envelope(self.db, 'W1', 'BASE')

    def test_composite_cg_at_2_7(self):
        total, cg, lo, hi = composite_cg([
            {'id': 'P1', 'kg': 600, 'x_m': 2.4},
            {'id': 'P2', 'kg': 600, 'x_m': 2.7},
            {'id': 'P3', 'kg': 600, 'x_m': 3.0}])
        self.assertEqual((total, cg, lo, hi), (1800.0, 2.7, 2.7, 2.7))

    def test_static_reactions_180_1620_and_overload_420(self):
        front, rear, wheelbase = static_reactions(1800, 2.7, 0.0, 3.0)
        self.assertEqual((front, rear, wheelbase), (180.0, 1620.0, 3.0))
        self.assertEqual(rear - LIMIT, 420.0)
        self.assertEqual(self.env['static'],
                         {'front_kg': 180.0, 'rear_kg': 1620.0,
                          'front_worst_kg': 180.0, 'rear_worst_kg': 1620.0})

    def test_brake_envelope_432_1368_transfer_252_still_over_168(self):
        self.assertEqual(brake_transfer(1800, 0.35, 1.2, 3.0), 252.0)
        br = self.env['braking']
        self.assertEqual((br['transfer_kg'], br['front_kg'], br['rear_kg']),
                         (252.0, 432.0, 1368.0))
        self.assertEqual(br['rear_kg'] - LIMIT, 168.0)
        self.assertEqual(self.env['axle_peaks_kg'], {'front_kg': 432.0, 'rear_kg': 1620.0})
        self.assertEqual(self.env['status'], '超限')
        self.assertTrue(any('后轴' in r and '1620.0' in r for r in self.env['reasons']))

    def test_envelope_carries_geometry_pallet_sources_and_condition_range(self):
        self.assertEqual(self.env['geometry'],
                         {'front_x_m': 0.0, 'rear_x_m': 3.0,
                          'wheelbase_m': 3.0, 'source': 'L2-geo-1'})
        self.assertEqual([p['id'] for p in self.env['cargo']['pallets']], ['P1', 'P2', 'P3'])
        self.assertEqual(sum(p['boxes'] for p in self.env['cargo']['pallets']), 60)
        self.assertEqual(self.env['braking']['condition_id'], 'B1')
        self.assertEqual(self.env['braking']['decel_g'], 0.35)
        self.assertEqual(self.env['braking']['cg_height_m'], 1.2)
        self.assertIn('L2-pallets-1', self.env['provenance']['event_ids'])

    def test_base_disposition_blocks_dispatch(self):
        disp = latest_disposition(self.db, 'W1')
        self.assertEqual(disp['status'], '不可发-超限')
        self.assertEqual(disp['trips'], 1)
        self.assertTrue(disp['reconciliation']['closed'])


class RouteATests(unittest.TestCase):
    """路线 A：重排至总重心 x=1.5，静态 900/900、急刹 1152/648，需绑扎+复称。"""

    def test_route_a_full_flow_passes_with_relash_and_reweigh(self):
        db = base_db()
        route_a(db, 'W1', {'P1': 0.5, 'P2': 1.5, 'P3': 2.5}, 'L2-A-route-1', 30)
        pending = latest_disposition(db, 'W1')
        self.assertEqual(pending['status'], '待办-不得发运')
        self.assertEqual(set(pending['reasons']),
                         {'缺 重新绑扎签认', '缺 复称票'})
        record_lashing(db, 'W1', True, 'L2-A-lash-1', 31)
        record_reweigh(db, 'W1', 'RW1', 4000, 2200, 'L2-A-reweigh-1', 32)
        env = latest_envelope(db, 'W1', 'A')
        self.assertEqual(env['status'], '通过')
        self.assertEqual((env['static']['front_kg'], env['static']['rear_kg']), (900.0, 900.0))
        self.assertEqual((env['braking']['front_kg'], env['braking']['rear_kg']),
                         (1152.0, 648.0))
        self.assertEqual(env['axle_peaks_kg'], {'front_kg': 1152.0, 'rear_kg': 900.0})
        disp = latest_disposition(db, 'W1')
        self.assertEqual(disp['status'], '可发')
        self.assertEqual(disp['trips'], 1)
        self.assertEqual(disp['route'], 'A')
        self.assertTrue(disp['satisfied']['relash'] and disp['satisfied']['reweigh'])
        # BASE 超限结论保留，未被路线 A 覆盖
        self.assertEqual(latest_envelope(db, 'W1', 'BASE')['status'], '超限')

    def test_route_a_pallet_geometry_cg_moves_to_1_5(self):
        db = base_db()
        route_a(db, 'W1', {'P1': 0.5, 'P2': 1.5, 'P3': 2.5}, 'L2-A-route-1', 30)
        env = latest_envelope(db, 'W1', 'A')
        self.assertEqual(env['cargo']['cg_x_m'], 1.5)
        self.assertEqual(env['cargo']['pallet_total_kg'], 1800.0)


class RouteBTests(unittest.TestCase):
    """路线 B：后端 600kg 转 V2，V1 余 1200，静态 180/1020、急刹 348/852。"""

    def test_route_b_requires_handover_and_keeps_two_vehicle_lineage(self):
        db = base_db()
        route_b(db, 'W1', ['P3'], 'V2', 'L2-B-route-1', 40)
        no_handover = latest_disposition(db, 'W1')
        self.assertEqual(no_handover['status'], '暂停')
        self.assertFalse(no_handover['reconciliation']['closed'])
        self.assertTrue(any('交接凭据' in r for r in no_handover['reasons']))
        record_handover(db, 'W1', 'H1', 'V2', ['P3'], 600, 'L2-B-handover-1', 41)
        env = latest_envelope(db, 'W1', 'B')
        self.assertEqual(env['status'], '通过')
        self.assertEqual((env['static']['front_kg'], env['static']['rear_kg']), (180.0, 1020.0))
        self.assertEqual(brake_transfer(1200, 0.35, 1.2, 3.0), 168.0)
        self.assertEqual((env['braking']['front_kg'], env['braking']['rear_kg']),
                         (348.0, 852.0))
        disp = latest_disposition(db, 'W1')
        self.assertEqual(disp['status'], '可发')
        self.assertEqual(disp['trips'], 2)
        ids = {v['vehicle_id']: v for v in disp['vehicles']}
        self.assertEqual(ids['V1']['kg'], 1200.0)
        self.assertEqual(ids['V1']['pallet_ids'], ['P1', 'P2'])
        self.assertEqual(ids['V2']['kg'], 600.0)
        self.assertEqual(ids['V2']['handover_id'], 'H1')
        self.assertTrue(disp['reconciliation']['closed'])

    def test_route_a_safety_cannot_cover_route_b_missing_handover(self):
        db_a = base_db()
        route_a(db_a, 'W1', {'P1': 0.5, 'P2': 1.5, 'P3': 2.5}, 'A1', 30)
        record_lashing(db_a, 'W1', True, 'A2', 31)
        record_reweigh(db_a, 'W1', 'RW1', 4000, 2200, 'A3', 32)
        self.assertEqual(latest_disposition(db_a, 'W1')['status'], '可发')
        db_b = base_db()
        route_b(db_b, 'W1', ['P3'], 'V2', 'B1', 40)
        self.assertEqual(latest_disposition(db_b, 'W1')['status'], '暂停')


class EdgeScenarioTests(unittest.TestCase):
    """覆盖：轴距更正、托盘重复、半途卸货、复称票晚到、坐标误差跨限、
    制动工况撤销、跨车转移不双计。"""

    def test_wheelbase_correction_recomputes_all_envelope_versions(self):
        db = base_db()
        record_geometry(db, 'W1', 0.0, 3.2, 'L2-geo-fix', 50)
        env = latest_envelope(db, 'W1', 'BASE')
        self.assertEqual(env['geometry']['wheelbase_m'], 3.2)
        front, rear, _ = static_reactions(1800, 2.7, 0.0, 3.2)
        self.assertEqual((env['static']['front_kg'], env['static']['rear_kg']),
                         (front, rear))
        versions = envelope_versions(db, 'W1', 'BASE')
        self.assertGreaterEqual(len(versions), 2)
        self.assertNotEqual(versions[0][2]['geometry'], versions[-1][2]['geometry'])

    def test_duplicate_pallet_event_is_idempotent_conflict_rejected(self):
        db = base_db()
        same = [{'id': 'P1', 'kg': 600, 'x_m': 2.4, 'boxes': 20},
                {'id': 'P2', 'kg': 600, 'x_m': 2.7, 'boxes': 20},
                {'id': 'P3', 'kg': 600, 'x_m': 3.0, 'boxes': 20}]
        self.assertFalse(capture(db, 'W1', 'pallets', {'pallets': same},
                                 'L2-pallets-1', 22))
        before = envelope_versions(db, 'W1', 'BASE')
        with self.assertRaises(ValueError):
            record_pallets(db, 'W1',
                           [{'id': 'P3', 'kg': 601, 'x_m': 3.0, 'boxes': 20}],
                           'L2-pallets-bad', 60)
        self.assertEqual(envelope_versions(db, 'W1', 'BASE'), before)

    def test_midway_unload_reduces_cargo_and_records_offload_lineage(self):
        db = base_db()
        record_unload(db, 'W1', 'P3', 600, 3.0, 'U1', 70)
        with self.assertRaises(ValueError):
            record_unload(db, 'W1', 'P3', 600, 3.0, 'U2', 71)
        env = latest_envelope(db, 'W1', 'BASE')
        self.assertEqual(env['cargo']['pallet_total_kg'], 1200.0)
        disp = latest_disposition(db, 'W1')
        offload = [x for x in disp['cargo_lineage']
                   if x.get('unload_event')]
        self.assertEqual(len(offload), 1)
        self.assertEqual(offload[0]['pallet_ids'], ['P3'])
        self.assertEqual(offload[0]['unload_event'], 'U1')
        self.assertTrue(any(v['vehicle_id'] == 'OFFLOAD' for v in disp['vehicles']))
        self.assertTrue(disp['reconciliation']['closed'])

    def test_late_reweigh_ticket_overrides_net_and_versions_envelope(self):
        db = base_db()
        record_reweigh(db, 'W1', 'RW-late', 3950, 2200, 'RW-late-1', 80)
        env = latest_envelope(db, 'W1', 'BASE')
        self.assertEqual(env['provenance']['reweigh_ids'], ['RW-late'])
        # 复称 1750 与托盘 1800 不符：包络暂停并保留物理反力，处置层暂停闭合
        self.assertEqual(env['status'], '暂停')
        self.assertTrue(any('1750.0' in r for r in env['reasons']))
        self.assertIsNotNone(env['static'])
        disp = latest_disposition(db, 'W1')
        self.assertEqual(disp['reconciliation']['ticket_net_kg'], 1750.0)
        self.assertFalse(disp['reconciliation']['closed'])
        self.assertEqual(disp['status'], '暂停')

    def test_coordinate_tolerance_worst_case_crosses_limit(self):
        db = sqlite3.connect(':memory:')
        import_ticket(db, signed_ticket(), secret=SECRET)
        set_axle_limit(db, 'W1', LIMIT, 'C1', 1)
        record_geometry(db, 'W1', 0.0, 3.0, 'G1', 2)
        # 名义重心 1.9（后轴 1140，未超），误差 +0.10 -> 2.0 -> 后轴 1200 临界，
        # 再取 +0.15 -> 2.05 -> 后轴 1230，最坏值跨限
        record_pallets(db, 'W1', [
            {'id': 'Q1', 'kg': 1800, 'x_m': 1.9, 'x_tol_m': 0.15, 'boxes': 60}],
            'M1', 3)
        record_brake(db, 'W1', 'B1', 1.2, 0.35, 'BR1', 4)
        env = latest_envelope(db, 'W1', 'BASE')
        self.assertEqual(env['cargo']['cg_x_m'], 1.9)
        self.assertEqual(env['cargo']['cg_x_max_m'], 2.05)
        self.assertLess(env['static']['rear_kg'], LIMIT)
        self.assertGreater(env['static']['rear_worst_kg'], LIMIT)
        self.assertEqual(env['status'], '超限')
        self.assertTrue(any('坐标误差最坏值跨限' in r for r in env['reasons']))

    def test_brake_revocation_suspends_roadworthiness_not_silently_passes(self):
        db = base_db()
        self.assertEqual(latest_envelope(db, 'W1', 'BASE')['braking']['condition_id'], 'B1')
        revoke_brake(db, 'W1', 'B1', 'RV1', 90)
        with self.assertRaises(ValueError):
            revoke_brake(db, 'W1', 'B1', 'RV2', 91)
        env = latest_envelope(db, 'W1', 'BASE')
        self.assertIsNone(env['braking'])
        self.assertEqual(env['status'], '暂停')
        self.assertTrue(any('急刹工况依据' in r for r in env['reasons']))
        self.assertEqual(latest_disposition(db, 'W1')['status'], '暂停')

    def test_cross_vehicle_transfer_is_not_double_counted(self):
        db = base_db()
        route_b(db, 'W1', ['P3'], 'V2', 'B1', 40)
        record_handover(db, 'W1', 'H1', 'V2', ['P3'], 600, 'H1E', 41)
        with self.assertRaises(ValueError):
            record_handover(db, 'W1', 'H2', 'V2', ['P3'], 600, 'H2E', 42)
        disp = latest_disposition(db, 'W1')
        all_pallets = [pid for v in disp['vehicles'] for pid in v['pallet_ids']]
        self.assertEqual(sorted(all_pallets), ['P1', 'P2', 'P3'])
        self.assertEqual(len(all_pallets), len(set(all_pallets)))
        self.assertEqual(sum(v['kg'] for v in disp['vehicles']), 1800.0)
        self.assertTrue(disp['reconciliation']['closed'])

    def test_missing_geometry_or_brake_means_suspend_not_pass(self):
        db = sqlite3.connect(':memory:')
        import_ticket(db, signed_ticket(), secret=SECRET)
        record_pallets(db, 'W1', [{'id': 'P1', 'kg': 1800, 'x_m': 2.7}], 'M1', 1)
        project(db, 'W1')
        env = latest_envelope(db, 'W1', 'BASE')
        self.assertEqual(env['status'], '暂停')
        self.assertTrue(any('轴位几何' in r for r in env['reasons']))
        record_geometry(db, 'W1', 0.0, 3.0, 'G1', 2)
        env = latest_envelope(db, 'W1', 'BASE')
        self.assertEqual(env['status'], '暂停')
        self.assertTrue(any('急刹工况依据' in r for r in env['reasons']))


class AuditAndReplayTests(unittest.TestCase):
    """SQLite 可回放账、运输安全独立审计账、健康放行与适载分离。"""

    def test_replay_rebuilds_identical_ledger_from_raw_events(self):
        db = base_db()
        route_a(db, 'W1', {'P1': 0.5, 'P2': 1.5, 'P3': 2.5}, 'A1', 30)
        record_lashing(db, 'W1', True, 'A2', 31)
        record_reweigh(db, 'W1', 'RW1', 4000, 2200, 'A3', 32)
        transport_decide(db, 'W1')
        result = replay_transport(db, 'W1')
        self.assertTrue(result['match'], result['mismatches'])
        self.assertEqual(result['event_count'], 7)

    def test_tampered_envelope_is_detected_by_replay(self):
        db = base_db()
        self.assertTrue(replay_transport(db, 'W1')['match'])
        with db:
            db.execute("UPDATE axle_envelope SET payload='{\"status\":\"通过\"}' "
                       "WHERE ticket_id='W1' AND basis='BASE' AND version="
                       "(SELECT MAX(version) FROM axle_envelope WHERE ticket_id='W1' AND basis='BASE')")
        self.assertFalse(replay_transport(db, 'W1')['match'])

    def test_transport_decision_has_own_audit_surface_and_versions(self):
        db = base_db()
        d1 = transport_decide(db, 'W1')
        self.assertEqual(d1['result']['surface'], TRANSPORT_SURFACE)
        self.assertEqual(d1['result']['disposition_status'], '不可发-超限')
        d1_again = transport_decide(db, 'W1')
        self.assertEqual(d1_again['version'], d1['version'])
        route_b(db, 'W1', ['P3'], 'V2', 'B1', 40)
        record_handover(db, 'W1', 'H1', 'V2', ['P3'], 600, 'H1E', 41)
        d2 = transport_decide(db, 'W1')
        self.assertGreater(d2['version'], d1['version'])
        self.assertEqual(d2['result']['trips'], 2)
        self.assertTrue(d2['result']['health_release_is_not_roadworthiness'])
        self.assertEqual(transport_report(db, 'W1')['version'], d2['version'])

    def test_health_release_does_not_grant_roadworthiness(self):
        health_events = json.loads((ROOT / 'fixtures' / 'dispatch_ok.json').read_text(encoding='utf-8'))['events']
        health = [e for e in health_events if e['kind'] == 'health_check'][0]
        verdict = decide(health_events)
        names = {item[0]: item[1] for item in verdict}
        self.assertEqual(names['蜂箱检疫/健康放行'], '放行')
        db = base_db()
        td = transport_decide(db, 'W1')
        self.assertEqual(td['result']['disposition_status'], '不可发-超限')
        self.assertNotIn('蜂箱检疫/健康放行', td['proof_ids'])

    def test_synthetic_fixture_runs_three_branches(self):
        out = run_l2_case(str(FIXTURE))
        self.assertEqual(set(out), {'BASE', 'A', 'B'})
        self.assertEqual(out['BASE']['envelope']['static']['rear_kg'], 1620.0)
        self.assertEqual(out['A']['envelope']['braking']['front_kg'], 1152.0)
        self.assertEqual(out['B']['envelope']['braking']['rear_kg'], 852.0)
        self.assertEqual(out['B']['disposition']['trips'], 2)
        self.assertEqual(out['BASE']['gross_only_view']['gross_only_verdict'], '常规解误放行')

    def test_persistent_file_db_replay_roundtrip(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / 'transport.db'
        db = sqlite3.connect(path)
        import_ticket(db, signed_ticket(), secret=SECRET)
        set_axle_limit(db, 'W1', LIMIT, 'C1', 20)
        record_geometry(db, 'W1', 0.0, 3.0, 'G1', 21)
        record_pallets(db, 'W1', [
            {'id': 'P1', 'kg': 600, 'x_m': 2.4},
            {'id': 'P2', 'kg': 600, 'x_m': 2.7},
            {'id': 'P3', 'kg': 600, 'x_m': 3.0}], 'M1', 22)
        record_brake(db, 'W1', 'B1', 1.2, 0.35, 'BR1', 23)
        transport_decide(db, 'W1')
        db.close()
        db2 = sqlite3.connect(path)
        self.assertTrue(replay_transport(db2, 'W1')['match'])
        self.assertEqual(transport_report(db2, 'W1')['result']['envelope']['status'], '超限')
        db2.close()
        tmp.cleanup()


if __name__ == '__main__':
    unittest.main()
