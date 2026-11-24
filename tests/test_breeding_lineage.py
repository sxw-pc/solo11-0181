"""End-to-end tests for the physical-individual ancestor graph and the two
independent homology routes.

The provisional direct-parent screen (``queen.import_pair`` /
``kinship.assess``) is kept frozen: it still records ``allow`` for Q1-D1 from
the bare parent labels M1/F1 vs M2/F2.  Every release decision under test
instead consumes the directed ancestor graph built over physical breeding
individuals, whose nodes are joined only by namespaced certificate merge
edges -- never by a bare local short code.
"""
import json
import tempfile
import unittest
from pathlib import Path

from pollinate.store import connect
from pollinate.queen import import_pair
from pollinate.kinship import assess
from pollinate.breeding_store import init_breeding, ingest_many, breeding_events
from pollinate.breeding import evaluate_all, replay
from pollinate.__main__ import run_lineage, main

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / 'fixtures'
SUBJECT = 'case-queen'
PAIR = FX / 'breeding_pair_q1d1.json'


def load(name):
    return json.loads((FX / name).read_text(encoding='utf-8'))


class BreedingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbp = str(Path(self.tmp.name) / 'case.db')

    def tearDown(self):
        self.tmp.cleanup()

    def seed_pair(self, path=PAIR):
        return import_pair(self.dbp, path)

    def lineage(self, *names, subject=SUBJECT):
        out = None
        for name in names:
            out = run_lineage(self.dbp, str(FX / name), subject)
        return out

    def add_events(self, events, subject=SUBJECT):
        db = connect(self.dbp)
        init_breeding(db)
        with db:
            ingest_many(db, events)
        db.close()

    def decision(self, pair_id='Q1-D1'):
        out = evaluate_all(self.dbp, SUBJECT)
        return next(d for d in out['decisions'] if d['pair_id'] == pair_id), out

    def db(self):
        conn = connect(self.dbp)
        init_breeding(conn)
        return conn


class ProvisionalScreenTests(BreedingCase):
    def test_legacy_screen_still_allows_from_direct_parents(self):
        """The frozen enhanced copy only checks non-empty ids + parent overlap."""
        legacy = self.seed_pair()
        self.assertEqual(legacy['status'], 'allow')
        self.assertFalse(legacy['shared_parent'])
        db = self.db()
        row = db.execute('SELECT status,shared_parent FROM queen_pair_decision').fetchone()
        self.assertEqual(row['status'], 'allow')
        self.assertEqual(row['shared_parent'], 0)
        # The raw pair original is preserved verbatim for replay.
        raw = json.loads(db.execute('SELECT raw_json FROM queen_pair').fetchone()['raw_json'])
        self.assertEqual(raw['queen_parents'], ['M1', 'F1'])
        self.assertEqual(raw['drone_parents'], ['M2', 'F2'])
        db.close()

    def test_legacy_screen_detects_a_direct_shared_parent(self):
        pair = load('breeding_pair_q1d1.json')
        pair['drone_parents'] = ['M1', 'F2']
        p = Path(self.tmp.name) / 'shared.json'
        p.write_text(json.dumps(pair), encoding='utf-8')
        self.assertEqual(import_pair(self.dbp, p)['status'], 'hold')


class PaperRouteTests(BreedingCase):
    def test_complete_certificate_chain_merges_g1_and_holds_within_two_generations(self):
        self.seed_pair()
        self.lineage('lineage_baseline.json')
        out = self.lineage('lineage_paper_merge.json')
        decision = out['decisions'][0]
        self.assertEqual(decision['status'], 'hold')
        levels = decision['result']['common_ancestors']
        g1 = next(a for a in levels if a['entity'] == 'ent::RA::C-G1')
        # Both namespaced certificates now resolve to one physical grandparent.
        self.assertEqual(sorted(g1['certificates']), ['RA/C-G1', 'RB/C-G1B'])
        self.assertEqual(g1['queen_distance'], [2, 2])
        self.assertEqual(g1['drone_distance'], [2, 2])
        self.assertEqual(g1['level'], 2)
        # The merge edge is a paper chain, not a short-code match.
        db = self.db()
        merge = db.execute("SELECT * FROM entity_merge WHERE basis='paper_chain'").fetchone()
        self.assertEqual(merge['reversed'], 0)
        db.close()

    def test_baseline_without_merge_keeps_two_physical_g1_and_allows(self):
        self.seed_pair()
        out = self.lineage('lineage_baseline.json')
        self.assertEqual(out['decisions'][0]['status'], 'allow')
        # Two distinct physical entities share the bare short code G1.
        db = self.db()
        ents = {r['local_code'] for r in db.execute(
            "SELECT local_code FROM registry_identity WHERE local_code='G1'")}
        self.assertEqual(ents, {'G1'})
        certs = db.execute(
            "SELECT registry,cert_no FROM registry_identity WHERE local_code='G1'").fetchall()
        self.assertEqual(len(certs), 2)
        db.close()


class GeneticRouteTests(BreedingCase):
    def test_calibrated_panel_proves_two_provenances_and_allows(self):
        self.seed_pair()
        self.lineage('lineage_baseline.json')
        out = self.lineage('lineage_genetic_split.json')
        decision = out['decisions'][0]
        self.assertEqual(decision['status'], 'allow')
        self.assertEqual(decision['result']['genetic_evidence'], ['genetic-g1-split'])
        self.assertEqual(decision['result']['threshold_sources'],
                         ['lab-2026-calibration-bulletin-3'])
        db = self.db()
        panel = db.execute('SELECT * FROM genetic_panel').fetchone()
        self.assertEqual(panel['verdict'], 'different_body')
        self.assertGreaterEqual(panel['coverage'], panel['threshold'])
        self.assertEqual(panel['calibrated'], 1)
        db.close()

    def test_single_similar_locus_cannot_establish_identity(self):
        self.seed_pair()
        self.lineage('lineage_baseline.json')
        out = self.lineage('lineage_weak_locus.json')
        self.assertEqual(out['decisions'][0]['status'], 'allow')
        reasons = {r['reason'] for r in out['rejections']}
        self.assertIn('insufficient genetic panel', reasons)
        db = self.db()
        self.assertEqual(db.execute('SELECT COUNT(*) c FROM genetic_panel').fetchone()['c'], 0)
        # No genetic merge may be created from a weak, uncalibrated panel.
        self.assertEqual(
            db.execute("SELECT COUNT(*) c FROM entity_merge WHERE basis='genetic'").fetchone()['c'],
            0)
        db.close()


class AnomalyGraphTests(BreedingCase):
    def test_missing_page_keeps_unbounded_range_and_pends(self):
        self.seed_pair()
        self.lineage('lineage_baseline.json')
        before = self.db().execute(
            'SELECT MAX(version) v FROM graph_version').fetchone()['v']
        out = self.lineage('lineage_missing_page.json')
        self.assertEqual(out['decisions'][0]['status'], 'pending')
        self.assertTrue(any(r['reason'] == 'missing generation page' for r in out['rejections']))
        db = self.db()
        edge = db.execute(
            "SELECT gap_min,gap_max FROM ancestor_edge WHERE parent_entity='ent::RA::C-G1' "
            "AND graph_version=(SELECT MAX(version) FROM graph_version)").fetchone()
        self.assertEqual(edge['gap_min'], 1)
        self.assertIsNone(edge['gap_max'])  # reachability range stays open
        self.assertGreater(db.execute('SELECT MAX(version) v FROM graph_version').fetchone()['v'],
                           before)
        db.close()

    def test_forged_transfer_seal_is_rejected_and_does_not_merge(self):
        self.seed_pair()
        self.lineage('lineage_baseline.json')
        out = self.lineage('lineage_forged_transfer.json')
        self.assertEqual(out['decisions'][0]['status'], 'allow')
        self.assertTrue(any(r['reason'] == 'forged transfer seal' for r in out['rejections']))
        db = self.db()
        self.assertEqual(db.execute('SELECT COUNT(*) c FROM entity_merge').fetchone()['c'], 0)
        db.close()

    def test_ancestor_cycle_and_self_loop_are_refused_graph_stays_dag(self):
        db = connect(self.dbp)
        init_breeding(db)
        ingest_many(db, load('lineage_cycle.json')['events'])
        db.commit()
        db.close()
        result = evaluate_all(self.dbp, 'case-cycle')
        reasons = {r['reason'] for r in result['rejections']}
        self.assertIn('ancestor cycle', reasons)
        self.assertIn('self loop', reasons)
        db = self.db()
        # Neither the closing cycle edge nor the self-loop edge was persisted.
        self.assertEqual(
            db.execute('SELECT COUNT(*) c FROM ancestor_edge WHERE child_entity=parent_entity'
                       ).fetchone()['c'], 0)
        db.close()

    def test_conflicting_duplicate_registration_is_not_silently_merged(self):
        db = connect(self.dbp)
        init_breeding(db)
        ingest_many(db, load('lineage_duplicate.json')['events'])
        db.commit()
        db.close()
        result = evaluate_all(self.dbp, 'case-dup')
        self.assertTrue(
            any(r['reason'] == 'conflicting duplicate registration' for r in result['rejections']))
        db = self.db()
        self.assertEqual(
            db.execute("SELECT COUNT(*) c FROM entity_merge WHERE basis='duplicate_registration'"
                       ).fetchone()['c'], 0)
        db.close()


class PendingEvidenceTests(BreedingCase):
    def test_no_supplemental_evidence_stays_pending_not_legacy_allow(self):
        """A bare short-code collision without a merge/split receipt cannot be
        treated as proven unrelatedness; the old provisional allow is never
        reused as a final release."""
        pair = dict(load('breeding_pair_q1d1.json'))
        del pair['queen_refs']
        del pair['drone_refs']
        p = Path(self.tmp.name) / 'bare.json'
        p.write_text(json.dumps(pair), encoding='utf-8')
        # Provisional screen still says allow from direct parents only.
        self.assertEqual(import_pair(self.dbp, p)['status'], 'allow')
        out = self.lineage('lineage_baseline.json')
        decision = out['decisions'][0]
        self.assertEqual(decision['status'], 'pending')
        self.assertIn('G1', decision['result']['reason'])
        # A calibrated split receipt is what finally releases it.
        out = self.lineage('lineage_genetic_split.json')
        self.assertEqual(out['decisions'][0]['status'], 'allow')


class ReversalPropagationTests(BreedingCase):
    def _two_pairs(self):
        self.seed_pair()
        pair2 = dict(load('breeding_pair_q1d1.json'), pair_id='Q2-D2')
        p2 = Path(self.tmp.name) / 'pair2.json'
        p2.write_text(json.dumps(pair2), encoding='utf-8')
        import_pair(self.dbp, p2)

    def test_reversing_wrong_merge_revises_every_descendant_pair(self):
        self._two_pairs()
        self.lineage('lineage_baseline.json', 'lineage_paper_merge.json')
        merged, _ = self.decision()
        self.assertEqual(merged['status'], 'hold')
        self.add_events([dict(id='rev-1', kind='merge_reversal', subject=SUBJECT,
                              event_time=300, received_at=301,
                              data=dict(reverses='transfer-g1'))])
        out = evaluate_all(self.dbp, SUBJECT)
        statuses = {d['pair_id']: d['status'] for d in out['decisions']}
        self.assertEqual(statuses, {'Q1-D1': 'allow', 'Q2-D2': 'allow'})
        # Both pairs were revised across every graph version, not just the
        # pair currently under review.
        db = self.db()
        revisions = db.execute(
            'SELECT pair_id,COUNT(*) c FROM breeding_decision GROUP BY pair_id').fetchall()
        self.assertEqual({r['pair_id']: r['c'] for r in revisions},
                         {'Q1-D1': 3, 'Q2-D2': 3})
        merge = db.execute('SELECT reversed,reversal_evidence_id FROM entity_merge').fetchone()
        self.assertEqual(merge['reversed'], 1)
        self.assertEqual(merge['reversal_evidence_id'], 'rev-1')
        db.close()


class ReplayAndStorageTests(BreedingCase):
    def test_replay_recomputes_from_raw_and_matches(self):
        self.seed_pair()
        self.lineage('lineage_baseline.json', 'lineage_paper_merge.json')
        report = replay(self.dbp, SUBJECT)
        self.assertTrue(report['match'])
        self.assertEqual(report['pair_count'], 1)

    def test_replay_detects_tampered_release_decision(self):
        self.seed_pair()
        self.lineage('lineage_baseline.json', 'lineage_paper_merge.json')
        db = self.db()
        db.execute("UPDATE breeding_decision SET status='allow' WHERE pair_id='Q1-D1'")
        db.commit()
        db.close()
        report = replay(self.dbp, SUBJECT)
        self.assertFalse(report['match'])
        self.assertIn('Q1-D1', report['mismatches'])

    def test_replay_does_not_mutate_live_database(self):
        self.seed_pair()
        self.lineage('lineage_baseline.json')
        db = self.db()
        before = db.execute('SELECT COUNT(*) c FROM breeding_decision').fetchone()['c']
        db.close()
        replay(self.dbp, SUBJECT)
        db = self.db()
        after = db.execute('SELECT COUNT(*) c FROM breeding_decision').fetchone()['c']
        db.close()
        self.assertEqual(before, after)

    def test_graph_versions_are_retained_and_idempotent(self):
        self.seed_pair()
        self.lineage('lineage_baseline.json')
        self.lineage('lineage_paper_merge.json')
        self.lineage('lineage_paper_merge.json')  # duplicate evidence
        db = self.db()
        versions = [r['version'] for r in db.execute(
            'SELECT version FROM graph_version ORDER BY version')]
        db.close()
        self.assertEqual(versions, [1, 2])

    def test_raw_evidence_is_replayable_from_sqlite(self):
        self.seed_pair()
        self.lineage('lineage_baseline.json', 'lineage_genetic_split.json')
        db = self.db()
        rows = breeding_events(db, SUBJECT)
        db.close()
        kinds = {e['kind'] for e in rows}
        self.assertIn('paper_record', kinds)
        self.assertIn('seal_domain', kinds)
        self.assertIn('genetic_receipt', kinds)


class CliTests(BreedingCase):
    def test_cli_lineage_report_and_replay(self):
        import io
        from contextlib import redirect_stdout
        self.seed_pair()
        with redirect_stdout(io.StringIO()):
            main(['lineage', '--db', self.dbp, '--fixture', str(FX / 'lineage_baseline.json'),
                  '--subject', SUBJECT])
            main(['lineage', '--db', self.dbp, '--fixture', str(FX / 'lineage_paper_merge.json'),
                  '--subject', SUBJECT])
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(['breeding-report', '--db', self.dbp])
        reported = json.loads(buf.getvalue())
        self.assertEqual(reported['Q1-D1']['status'], 'hold')
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(['breeding-replay', '--db', self.dbp, '--subject', SUBJECT])
        self.assertTrue(json.loads(buf.getvalue())['match'])


if __name__ == '__main__':
    unittest.main()
