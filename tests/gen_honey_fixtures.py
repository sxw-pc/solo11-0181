"""Regenerate the synthetic honey-jar fixtures under fixtures/.

Machine events are HMAC-signed with the demo key {'FILLER-1': 'fill-key-1'}
so pollinate.fill.import_run accepts them. Edit and re-run this file
(python3 tests/gen_honey_fixtures.py) after changing any event; it is not
a test module and unittest discovery ignores it.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pollinate.fill import sign_event

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
KEY = 'fill-key-1'
MACHINE = 'FILLER-1'


class RunBuilder:
    """Builds a signed offline machine log: valve switches, jar scans,
    1000 g pump pulses and final scale weighings."""

    def __init__(self, run_id, blend_id='B1', received_at=100):
        self.doc = {'run_id': run_id, 'blend_id': blend_id, 'machine': MACHINE,
                    'received_at': received_at, 'events': []}

    def _add(self, kind, **fields):
        event = {'seq': len(self.doc['events']) + 1, 'kind': kind, **fields}
        event['sig'] = sign_event(KEY, self.doc['run_id'], event)
        self.doc['events'].append(event)

    def valve(self, source):
        self._add('valve', source=source)

    def jar(self, jar):
        self._add('jar', jar=jar)

    def pulses(self, source, jar, count, grams=1000):
        for _ in range(count):
            self._add('pulse', source=source, jar=jar, grams=grams)

    def weigh(self, jar, grams):
        self._add('weigh', jar=jar, grams=grams)


def alpha_run(run_id='ALPHA'):
    """Production alpha: barrel A (20 ppm) hits J1 first."""
    b = RunBuilder(run_id)
    b.valve('A')
    b.jar('J1')
    b.pulses('A', 'J1', 5)        # J1: 5000 g of pure A -> 20 ppm
    b.jar('J2')
    b.pulses('A', 'J2', 5)        # A exhausted: 10000 g total
    b.valve('B')
    b.pulses('B', 'J2', 90)       # J2: 5 kg A + 90 kg B -> 100/95 ppm
    b.weigh('J1', 5000)
    b.weigh('J2', 95000)
    return b.doc


def beta_run(run_id='BETA'):
    """Production beta: barrel B (0 ppm) hits J1 first."""
    b = RunBuilder(run_id)
    b.valve('B')
    b.jar('J1')
    b.pulses('B', 'J1', 5)        # J1: 5000 g of pure B -> 0 ppm
    b.jar('J2')
    b.valve('A')
    b.pulses('A', 'J2', 10)       # A exhausted: 10000 g total
    b.valve('B')
    b.pulses('B', 'J2', 85)       # J2: 10 kg A + 85 kg B -> 200/95 ppm
    b.weigh('J1', 5000)
    b.weigh('J2', 95000)
    return b.doc


def pulse_loss_run():
    """Alpha, but one B->J2 pulse event is lost offline (seq gap left in
    place): the signed chain no longer accounts for the blend."""
    doc = alpha_run('ALPHA-PULSE-LOSS')
    lost = next(e for e in doc['events'] if e['kind'] == 'pulse' and e['source'] == 'B')
    doc['events'] = [e for e in doc['events'] if e['seq'] != lost['seq']]
    return doc


def weigh_error_run():
    """Alpha, but the scale reads 96000 g for a 95000 g pulsed J2."""
    b = RunBuilder('ALPHA-WEIGH-ERROR')
    b.valve('A')
    b.jar('J1')
    b.pulses('A', 'J1', 5)
    b.jar('J2')
    b.pulses('A', 'J2', 5)
    b.valve('B')
    b.pulses('B', 'J2', 90)
    b.weigh('J1', 5000)
    b.weigh('J2', 96000)          # 1000 g beyond the pulsed mass
    return b.doc


def lab_result(report_id, jar, mass_g, residue_ug, received_at):
    return {'report_id': report_id, 'blend_id': 'B1', 'kind': 'result', 'jar': jar,
            'lab': 'COOP-LAB', 'received_at': received_at,
            'sample': {'sealed': True, 'mass_g': mass_g, 'residue_ug': residue_ug}}


def lab_revocation(report_id, revokes, received_at):
    return {'report_id': report_id, 'blend_id': 'B1', 'kind': 'revocation',
            'revokes': revokes, 'reason': 'sample seal broken in transit',
            'received_at': received_at}


def main():
    out = {
        'blend_b1.json': {'blend_id': 'B1', 'hive_id': 'H1', 'components': [
            {'source': 'A', 'kg': 10, 'ppm': 20}, {'source': 'B', 'kg': 90, 'ppm': 0}]},
        'fill_alpha.json': alpha_run(),
        'fill_beta.json': beta_run(),
        'fill_alpha_pulse_loss.json': pulse_loss_run(),
        'fill_alpha_weigh_error.json': weigh_error_run(),
        'lab_alpha_j1.json': lab_result('LAB-A-J1', 'J1', 250, 5000, 200),   # 20 ppm
        'lab_alpha_j2.json': lab_result('LAB-A-J2', 'J2', 250, 263, 201),    # 1.052 ppm
        'lab_alpha_j1_revoke.json': lab_revocation('LAB-A-J1-REVOKE', 'LAB-A-J1', 210),
        'lab_alpha_j2_revoke.json': lab_revocation('LAB-A-J2-REVOKE', 'LAB-A-J2', 211),
        'lab_beta_j1_conflict.json': lab_result('LAB-B-J1', 'J1', 250, 6250, 200),  # 25 ppm vs flow 0 ppm
    }
    for name, doc in out.items():
        path = FIXTURES / name
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + '\n', encoding='utf8')
        print('wrote', path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
