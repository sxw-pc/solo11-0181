"""Cross-module tests for the jar-level honey packaging subsystem:
fill (machine-log ingestion) -> flowgraph (mass-flow solve) -> jargrade
(per-jar decision) -> lab (sealed receipts) -> inventory (ship/recall/replay).
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pollinate.honey import import_blend
from pollinate.fill import import_run
from pollinate.lab import import_report
from pollinate.jargrade import decide_jars, current_decisions, release_board
from pollinate.inventory import ship_jar, return_jar, replay_inventory, current_inventory
from pollinate.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures'
KEYS = {'FILLER-1': 'fill-key-1'}


class HoneyJarCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'case.db'
        self.provisional = import_blend(self.db_path, FIXTURES / 'blend_b1.json')
        self.db = connect(self.db_path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def grade_row(self):
        return self.db.execute('SELECT mean_ppm, status FROM honey_grade WHERE blend_id=?', ('B1',)).fetchone()

    def write_tmp(self, name, doc):
        path = Path(self.tmp.name) / name
        path.write_text(json.dumps(doc), encoding='utf8')
        return path

    def test_provisional_batch_grade_seeded(self):
        self.assertEqual(self.provisional, {'blend_id': 'B1', 'mean_ppm': 2, 'status': 'release'})

    def test_alpha_flow_gram_by_gram(self):
        summary = import_run(self.db, FIXTURES / 'fill_alpha.json', KEYS)
        self.assertTrue(summary['proven'])
        self.assertEqual(summary['defects'], [])
        j1, j2 = summary['jars']['J1'], summary['jars']['J2']
        self.assertEqual((j1['grams'], j1['residue_ug'], j1['ppm']), (5000, 100000, 20.0))
        self.assertEqual((j2['grams'], j2['residue_ug'], j2['ppm']), (95000, 100000, 1.0526))
        # mass and residue mass conserve across barrels and jars
        self.assertEqual(j1['grams'] + j2['grams'], 100000)
        self.assertEqual(j1['residue_ug'] + j2['residue_ug'], 200000)
        self.assertEqual(summary['barrels']['A'], {'drawn_g': 10000, 'expected_g': 10000})
        self.assertEqual(summary['barrels']['B'], {'drawn_g': 90000, 'expected_g': 90000})
        self.assertTrue(all(e['proven'] for e in summary['edges']))
        edge = next(e for e in summary['edges'] if e['source'] == 'A' and e['jar'] == 'J1')
        self.assertEqual((edge['grams'], edge['residue_ug'], edge['pulses']), (5000, 100000, 5))
        decisions = current_decisions(self.db, 'B1')
        self.assertEqual(decisions['J1']['status'], 'hold')
        self.assertEqual(decisions['J1']['basis'], 'flow')
        self.assertEqual(decisions['J2']['status'], 'release')
        # the batch-wide 2 ppm mean is never pushed onto a jar
        self.assertNotEqual(decisions['J1']['ppm'], 2)
        board = release_board(self.db, 'B1')
        self.assertEqual(board['saleable'], ['J2'])
        self.assertEqual(board['batch_provisional'], {'mean_ppm': 2, 'status': 'release'})
        j1_board = next(j for j in board['jars'] if j['jar'] == 'J1')
        self.assertEqual(j1_board['sources'],
                         [{'run_id': 'ALPHA', 'source': 'A', 'grams': 5000, 'residue_ug': 100000, 'proven': True}])
        inv = current_inventory(self.db)
        self.assertEqual(inv['J1']['state'], 'held')
        self.assertEqual(inv['J2']['state'], 'saleable')

    def test_beta_flow_gram_by_gram(self):
        summary = import_run(self.db, FIXTURES / 'fill_beta.json', KEYS)
        self.assertTrue(summary['proven'])
        j1, j2 = summary['jars']['J1'], summary['jars']['J2']
        self.assertEqual((j1['grams'], j1['residue_ug'], j1['ppm']), (5000, 0, 0.0))
        self.assertEqual((j2['grams'], j2['residue_ug'], j2['ppm']), (95000, 200000, 2.1053))
        self.assertEqual(j1['grams'] + j2['grams'], 100000)
        self.assertEqual(j1['residue_ug'] + j2['residue_ug'], 200000)
        decisions = current_decisions(self.db, 'B1')
        self.assertEqual(decisions['J1']['status'], 'release')
        self.assertEqual(decisions['J2']['status'], 'release')
        self.assertEqual(release_board(self.db, 'B1')['saleable'], ['J1', 'J2'])

    def test_same_batch_mean_opposite_j1_fates(self):
        alpha = import_run(self.db, FIXTURES / 'fill_alpha.json', KEYS)
        with tempfile.TemporaryDirectory() as other:
            other_path = Path(other) / 'case.db'
            provisional2 = import_blend(other_path, FIXTURES / 'blend_b1.json')
            db2 = connect(other_path)
            try:
                beta = import_run(db2, FIXTURES / 'fill_beta.json', KEYS)
                d2 = current_decisions(db2, 'B1')
                grade2 = db2.execute('SELECT mean_ppm, status FROM honey_grade WHERE blend_id=?', ('B1',)).fetchone()
            finally:
                db2.close()
        d1 = current_decisions(self.db, 'B1')
        # identical worksheet and identical provisional grade in both productions ...
        self.assertEqual(provisional2, self.provisional)
        self.assertEqual((self.grade_row()['mean_ppm'], self.grade_row()['status']), (2, 'release'))
        self.assertEqual((grade2['mean_ppm'], grade2['status']), (2, 'release'))
        # ... but the same jar number J1 carries 20 ppm in alpha and 0 ppm in beta
        self.assertEqual(alpha['jars']['J1']['ppm'], 20.0)
        self.assertEqual(beta['jars']['J1']['ppm'], 0.0)
        self.assertEqual(d1['J1']['status'], 'hold')
        self.assertEqual(d2['J1']['status'], 'release')

    def test_pulse_loss_marks_run_unverified(self):
        summary = import_run(self.db, FIXTURES / 'fill_alpha_pulse_loss.json', KEYS)
        self.assertFalse(summary['proven'])
        self.assertIn('pulse_gap', summary['defects'])
        self.assertIn('barrel_mismatch', summary['defects'])
        self.assertFalse(any(e['proven'] for e in summary['edges']))
        decisions = current_decisions(self.db, 'B1')
        for jar in ('J1', 'J2'):
            self.assertEqual(decisions[jar]['status'], 'unverified')
            self.assertIn('pulse_gap', decisions[jar]['defects'])
        self.assertEqual(release_board(self.db, 'B1')['saleable'], [])
        inv = current_inventory(self.db)
        self.assertEqual(inv['J1']['state'], 'frozen')
        self.assertEqual(inv['J2']['state'], 'frozen')
        self.assertEqual((self.grade_row()['mean_ppm'], self.grade_row()['status']), (2, 'release'))

    def test_weigh_error_marks_single_jar_unverified(self):
        summary = import_run(self.db, FIXTURES / 'fill_alpha_weigh_error.json', KEYS)
        self.assertTrue(summary['proven'])  # pulse chain and conservation intact
        self.assertEqual(summary['jars']['J2']['defects'], ['weigh_mismatch'])
        self.assertEqual(summary['jars']['J2']['weighed_g'], 96000)
        decisions = current_decisions(self.db, 'B1')
        self.assertEqual(decisions['J1']['status'], 'hold')  # J1 still proven by the flow
        self.assertEqual(decisions['J2']['status'], 'unverified')
        self.assertIn('weigh_mismatch', decisions['J2']['defects'])
        self.assertEqual(release_board(self.db, 'B1')['saleable'], [])

    def test_duplicate_jar_numbers_invalidate_both_runs(self):
        import_run(self.db, FIXTURES / 'fill_alpha.json', KEYS)
        first = current_decisions(self.db, 'B1')
        self.assertEqual(first['J1']['status'], 'hold')
        self.assertEqual(first['J1']['version'], 1)
        import_run(self.db, FIXTURES / 'fill_beta.json', KEYS)  # reuses jar numbers J1/J2
        decisions = current_decisions(self.db, 'B1')
        for jar in ('J1', 'J2'):
            self.assertEqual(decisions[jar]['status'], 'unverified')
            self.assertIn('duplicate_jar', decisions[jar]['defects'])
            self.assertEqual(decisions[jar]['version'], 2)
        self.assertEqual(release_board(self.db, 'B1')['saleable'], [])

    def test_bad_signature_breaks_chain_of_custody(self):
        doc = json.loads((FIXTURES / 'fill_alpha.json').read_text(encoding='utf8'))
        doc['events'][3]['grams'] = 999  # tamper with one pulse after signing
        summary = import_run(self.db, self.write_tmp('tampered.json', doc), KEYS)
        self.assertIn('bad_signature', summary['defects'])
        self.assertFalse(summary['proven'])
        decisions = current_decisions(self.db, 'B1')
        self.assertEqual(decisions['J1']['status'], 'unverified')
        self.assertIn('bad_signature', decisions['J1']['defects'])

    def test_duplicate_seq_event_marks_unverified(self):
        doc = json.loads((FIXTURES / 'fill_alpha.json').read_text(encoding='utf8'))
        # replay one signed pulse verbatim: valid signature, duplicated seq
        doc['events'].append(dict(doc['events'][4]))
        summary = import_run(self.db, self.write_tmp('dup_seq.json', doc), KEYS)
        self.assertIn('seq_duplicate', summary['defects'])
        self.assertFalse(summary['proven'])
        self.assertEqual(current_decisions(self.db, 'B1')['J1']['status'], 'unverified')

    def test_unknown_machine_key_rejected(self):
        with self.assertRaises(ValueError):
            import_run(self.db, FIXTURES / 'fill_alpha.json', {'OTHER-MACHINE': 'x'})

    def test_lab_report_for_unknown_blend_rejected(self):
        doc = {'report_id': 'LAB-ORPHAN', 'blend_id': 'B9', 'kind': 'result', 'jar': 'J1',
               'sample': {'sealed': True, 'mass_g': 250, 'residue_ug': 10}, 'received_at': 200}
        with self.assertRaises(ValueError):
            import_report(self.db, self.write_tmp('orphan.json', doc))

    def test_lab_path_decides_jars_when_log_unusable(self):
        summary = import_run(self.db, FIXTURES / 'fill_alpha_pulse_loss.json', KEYS)
        self.assertFalse(any(e['proven'] for e in summary['edges']))
        import_report(self.db, FIXTURES / 'lab_alpha_j1.json')
        import_report(self.db, FIXTURES / 'lab_alpha_j2.json')
        decisions = current_decisions(self.db, 'B1')
        self.assertEqual(decisions['J1']['status'], 'hold')
        self.assertEqual(decisions['J1']['basis'], 'lab')
        self.assertEqual(decisions['J1']['ppm'], 20.0)
        self.assertEqual(decisions['J2']['status'], 'release')
        self.assertEqual(decisions['J2']['basis'], 'lab')
        self.assertEqual(release_board(self.db, 'B1')['saleable'], ['J2'])
        # lab sampling proves nothing about the pump-stroke flow
        self.assertFalse(any(e['proven'] for e in summary['edges']))

    def test_lab_result_never_extrapolates_to_other_jars(self):
        import_run(self.db, FIXTURES / 'fill_alpha_pulse_loss.json', KEYS)
        import_report(self.db, FIXTURES / 'lab_alpha_j2.json')  # only J2 sampled
        decisions = current_decisions(self.db, 'B1')
        self.assertEqual(decisions['J2']['status'], 'release')
        self.assertEqual(decisions['J1']['status'], 'unverified')  # same truck, no inference
        self.assertEqual(release_board(self.db, 'B1')['saleable'], ['J2'])

    def test_flow_and_lab_agree_gives_joint_basis(self):
        import_run(self.db, FIXTURES / 'fill_alpha.json', KEYS)
        import_report(self.db, FIXTURES / 'lab_alpha_j1.json')
        import_report(self.db, FIXTURES / 'lab_alpha_j2.json')
        decisions = current_decisions(self.db, 'B1')
        self.assertEqual(decisions['J1']['basis'], 'flow+lab')
        self.assertEqual(decisions['J1']['status'], 'hold')
        self.assertEqual(decisions['J2']['basis'], 'flow+lab')
        self.assertEqual(decisions['J2']['status'], 'release')

    def test_lab_revocation_reverts_and_recalls_by_jar(self):
        import_run(self.db, FIXTURES / 'fill_alpha_pulse_loss.json', KEYS)
        import_report(self.db, FIXTURES / 'lab_alpha_j1.json')
        import_report(self.db, FIXTURES / 'lab_alpha_j2.json')
        self.assertEqual(ship_jar(self.db, 'J2', at=300), 'sold')
        import_report(self.db, FIXTURES / 'lab_alpha_j2_revoke.json')
        decisions = current_decisions(self.db, 'B1')
        self.assertEqual(decisions['J2']['status'], 'unverified')
        self.assertIn('no_valid_lab_report', decisions['J2']['defects'])
        self.assertEqual(decisions['J2']['version'], 3)  # unverified -> release -> unverified
        inv = current_inventory(self.db)
        self.assertEqual(inv['J2']['state'], 'recalled')  # shipped jar is recalled
        self.assertEqual(inv['J2']['version'], 4)         # frozen -> saleable -> sold -> recalled
        self.assertEqual(inv['J1']['state'], 'held')      # J1 keeps its own valid report
        import_report(self.db, FIXTURES / 'lab_alpha_j1_revoke.json')
        decisions = current_decisions(self.db, 'B1')
        self.assertEqual(decisions['J1']['status'], 'unverified')
        self.assertEqual(decisions['J1']['version'], 3)   # unverified -> hold -> unverified
        inv = current_inventory(self.db)
        self.assertEqual(inv['J1']['state'], 'frozen')    # in-stock jar is frozen
        self.assertEqual((self.grade_row()['mean_ppm'], self.grade_row()['status']), (2, 'release'))

    def test_partial_sale_recall_targets_jar_not_blend(self):
        import_run(self.db, FIXTURES / 'fill_beta.json', KEYS)
        self.assertEqual(release_board(self.db, 'B1')['saleable'], ['J1', 'J2'])
        self.assertEqual(ship_jar(self.db, 'J1', at=300), 'sold')
        import_report(self.db, FIXTURES / 'lab_beta_j1_conflict.json')  # lab claims 25 ppm on J1
        decisions = current_decisions(self.db, 'B1')
        self.assertEqual(decisions['J1']['status'], 'unverified')
        self.assertIn('flow_lab_conflict', decisions['J1']['defects'])
        inv = current_inventory(self.db)
        self.assertEqual(inv['J1']['state'], 'recalled')   # only the shipped jar is recalled
        self.assertEqual(inv['J1']['version'], 3)          # saleable -> sold -> recalled
        self.assertEqual(inv['J2']['state'], 'saleable')   # the rest of the truck still sells
        grade = self.grade_row()
        self.assertEqual((grade['mean_ppm'], grade['status']), (2, 'release'))  # B1 total untouched

    def test_ship_requires_saleable(self):
        import_run(self.db, FIXTURES / 'fill_alpha.json', KEYS)
        with self.assertRaises(ValueError):
            ship_jar(self.db, 'J1', at=300)  # J1 is held
        self.assertEqual(ship_jar(self.db, 'J2', at=300), 'sold')
        with self.assertRaises(ValueError):
            ship_jar(self.db, 'J2', at=301)  # already shipped

    def test_return_restores_saleable(self):
        import_run(self.db, FIXTURES / 'fill_beta.json', KEYS)
        ship_jar(self.db, 'J1', at=300)
        self.assertEqual(current_inventory(self.db)['J1']['state'], 'sold')
        self.assertEqual(return_jar(self.db, 'J1', at=301), 'saleable')
        with self.assertRaises(ValueError):
            return_jar(self.db, 'J1', at=302)  # not shipped any more

    def test_unsealed_sample_is_not_a_release_basis(self):
        import_run(self.db, FIXTURES / 'fill_alpha_pulse_loss.json', KEYS)
        doc = {'report_id': 'LAB-UNSEALED', 'blend_id': 'B1', 'kind': 'result', 'jar': 'J2',
               'sample': {'sealed': False, 'mass_g': 250, 'residue_ug': 10}, 'received_at': 200}
        import_report(self.db, self.write_tmp('unsealed.json', doc))
        decisions = current_decisions(self.db, 'B1')
        self.assertEqual(decisions['J2']['status'], 'unverified')
        self.assertIn('no_valid_lab_report', decisions['J2']['defects'])

    def test_run_and_report_reimport_is_idempotent(self):
        import_run(self.db, FIXTURES / 'fill_alpha.json', KEYS)
        before = current_decisions(self.db, 'B1')
        import_run(self.db, FIXTURES / 'fill_alpha.json', KEYS)
        import_report(self.db, FIXTURES / 'lab_alpha_j1.json')
        again = import_report(self.db, FIXTURES / 'lab_alpha_j1.json')
        self.assertTrue(again['duplicate'])
        after = current_decisions(self.db, 'B1')
        self.assertEqual(after['J1']['version'], 2)  # one bump from the lab report, not two
        self.assertEqual(before['J2'], after['J2'])
        conflict = json.loads((FIXTURES / 'fill_alpha.json').read_text(encoding='utf8'))
        conflict['received_at'] = 999
        with self.assertRaises(ValueError):
            import_run(self.db, self.write_tmp('conflict_run.json', conflict), KEYS)

    def test_redecide_is_idempotent(self):
        import_run(self.db, FIXTURES / 'fill_alpha.json', KEYS)
        before = current_decisions(self.db, 'B1')
        decide_jars(self.db, 'B1')
        self.assertEqual(current_decisions(self.db, 'B1'), before)

    def test_inventory_replay_matches_and_detects_decision_tamper(self):
        import_run(self.db, FIXTURES / 'fill_alpha.json', KEYS)
        ship_jar(self.db, 'J2', at=300)
        result = replay_inventory(self.db, 'B1')
        self.assertTrue(result['match'], result)
        with self.db:
            self.db.execute("UPDATE jar_decision SET status='release' WHERE jar='J1' AND version=1")
        result = replay_inventory(self.db, 'B1')
        self.assertFalse(result['match'])
        self.assertIn('J1', result['decision_mismatches'])
        self.assertEqual(result['decision_mismatches']['J1']['recomputed']['status'], 'hold')

    def test_inventory_replay_detects_state_tamper(self):
        import_run(self.db, FIXTURES / 'fill_alpha.json', KEYS)
        self.assertTrue(replay_inventory(self.db, 'B1')['match'])
        with self.db:
            self.db.execute("UPDATE jar_inventory SET state='saleable' WHERE jar='J1' AND version=1")
        result = replay_inventory(self.db, 'B1')
        self.assertFalse(result['match'])
        self.assertIn('J1', result['inventory_mismatches'])
        self.assertEqual(result['inventory_mismatches']['J1']['recomputed'], 'held')

    def test_blend_worksheet_survives_reports_and_decisions(self):
        original = json.loads((FIXTURES / 'blend_b1.json').read_text(encoding='utf8'))
        import_run(self.db, FIXTURES / 'fill_alpha.json', KEYS)
        import_report(self.db, FIXTURES / 'lab_alpha_j1.json')
        import_report(self.db, FIXTURES / 'lab_alpha_j1_revoke.json')
        row = self.db.execute('SELECT raw_json FROM honey_blend WHERE id=?', ('B1',)).fetchone()
        self.assertEqual(json.loads(row['raw_json']), original)
        self.assertEqual((self.grade_row()['mean_ppm'], self.grade_row()['status']), (2, 'release'))
        # a changed worksheet for the same blend id is refused, never overwritten
        altered = dict(original, components=[{'source': 'A', 'kg': 50, 'ppm': 1},
                                             {'source': 'B', 'kg': 50, 'ppm': 1}])
        with self.assertRaises(sqlite3.IntegrityError):
            import_blend(self.db_path, self.write_tmp('altered_blend.json', altered))
        row = self.db.execute('SELECT raw_json FROM honey_blend WHERE id=?', ('B1',)).fetchone()
        self.assertEqual(json.loads(row['raw_json']), original)


if __name__ == '__main__':
    unittest.main()
