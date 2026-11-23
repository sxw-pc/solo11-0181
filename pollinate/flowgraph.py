"""Directed mass-flow solve: source barrels -> pump pulses -> finished jars.

Every signed pulse is an edge in the flow graph. Mass (grams) and residue
(micrograms) are conserved exactly with rational arithmetic, so a jar's
concentration is derived from the barrel concentrations and the pulse
ledger alone -- never from the blend-wide mean. A jar is proven only when
the signed pulse chain is intact, the scale reading matches the pulsed
mass, and the run conserves mass and residue against the blend recipe.
"""
from fractions import Fraction

RELEASE_THRESHOLD_PPM = 3  # same limit as pollinate.grade.assess
WEIGH_TOLERANCE_REL = Fraction(1, 200)  # 0.5% of the pulsed jar mass
WEIGH_TOLERANCE_MIN_G = 10

RUN_DEFECT_ORDER = (
    'bad_signature', 'seq_duplicate', 'pulse_gap', 'pulse_before_state',
    'state_mismatch', 'unknown_source',
    'barrel_mismatch', 'total_mismatch', 'residue_imbalance',
)
JAR_DEFECT_ORDER = ('jar_reopened', 'weigh_missing', 'weigh_mismatch')


def status_for_ppm(ppm):
    return 'release' if ppm <= RELEASE_THRESHOLD_PPM else 'hold'


def barrels_of(blend_doc):
    """Source barrels with exact residue content. 1 ppm == 1 ug/g."""
    barrels = {}
    for comp in blend_doc['components']:
        grams = int(round(comp['kg'] * 1000))
        ug_per_g = Fraction(str(comp['ppm']))
        barrels[comp['source']] = {
            'grams': grams,
            'ug_per_g': ug_per_g,
            'residue_ug': ug_per_g * grams,
        }
    return barrels


def _order(defects, order):
    return sorted(defects, key=lambda d: (order.index(d) if d in order else len(order), d))


def _mass(value):
    """Render an exact Fraction as int when integral, else float."""
    return int(value) if value.denominator == 1 else float(value)


def solve_run(blend_doc, run_id, events):
    """Replay one machine log against the blend recipe.

    events: [{'seq': int, 'kind': str, 'valid': bool, 'data': dict}] with
    kind in valve|jar|pulse|weigh. Returns the flow graph (edges
    barrel->jar via pulses), per-jar masses, and the proven scope.
    """
    barrels = barrels_of(blend_doc)
    run_defects = set()
    seqs = [e['seq'] for e in events]
    if len(set(seqs)) != len(seqs):
        run_defects.add('seq_duplicate')
    if not seqs or set(range(1, max(seqs) + 1)) - set(seqs):
        run_defects.add('pulse_gap')
    if any(not e['valid'] for e in events):
        run_defects.add('bad_signature')

    jars = {}
    drawn = {}
    edges = {}
    valve = jar = None
    closed = set()

    def jar_rec(name):
        return jars.setdefault(name, {'grams': 0, 'residue': Fraction(0),
                                      'weighed': None, 'defects': set(), 'pulses': 0})

    for event in sorted(events, key=lambda e: e['seq']):
        if not event['valid']:
            continue  # untrusted content: flagged via bad_signature, not applied
        data = event['data']
        kind = event['kind']
        if kind == 'valve':
            valve = data['source']
        elif kind == 'jar':
            if jar is not None:
                closed.add(jar)
            jar = data['jar']
            rec = jar_rec(jar)
            if jar in closed:
                rec['defects'].add('jar_reopened')
        elif kind == 'pulse':
            if valve is None or jar is None:
                run_defects.add('pulse_before_state')
                continue
            source, target, grams = data['source'], data['jar'], data['grams']
            if source != valve or target != jar:
                run_defects.add('state_mismatch')
            if source not in barrels:
                run_defects.add('unknown_source')
            ug_per_g = barrels.get(source, {}).get('ug_per_g', Fraction(0))
            rec = jar_rec(target)
            rec['grams'] += grams
            rec['residue'] += ug_per_g * grams
            rec['pulses'] += 1
            drawn[source] = drawn.get(source, 0) + grams
            edge = edges.setdefault((source, target), {'grams': 0, 'residue': Fraction(0), 'pulses': 0})
            edge['grams'] += grams
            edge['residue'] += ug_per_g * grams
            edge['pulses'] += 1
        elif kind == 'weigh':
            jar_rec(data['jar'])['weighed'] = data['grams']

    for source, barrel in barrels.items():
        if drawn.get(source, 0) != barrel['grams']:
            run_defects.add('barrel_mismatch')
    if sum(drawn.values()) != sum(b['grams'] for b in barrels.values()):
        run_defects.add('total_mismatch')
    jar_residue = sum((rec['residue'] for rec in jars.values()), Fraction(0))
    barrel_residue = sum((b['residue_ug'] for b in barrels.values()), Fraction(0))
    if jar_residue != barrel_residue:
        run_defects.add('residue_imbalance')

    run_defects = _order(run_defects, RUN_DEFECT_ORDER)
    run_proven = not run_defects
    out_jars = {}
    for name, rec in jars.items():
        defects = set(rec['defects'])
        if rec['weighed'] is None:
            defects.add('weigh_missing')
        else:
            tolerance = max(Fraction(WEIGH_TOLERANCE_MIN_G), rec['grams'] * WEIGH_TOLERANCE_REL)
            if abs(rec['weighed'] - rec['grams']) > tolerance:
                defects.add('weigh_mismatch')
        out_jars[name] = {
            'jar': name,
            'grams': rec['grams'],
            'weighed_g': rec['weighed'],
            'residue_ug': _mass(rec['residue']),
            'ppm': round(float(rec['residue'] / rec['grams']), 4) if rec['grams'] else None,
            'pulses': rec['pulses'],
            'defects': _order(defects, JAR_DEFECT_ORDER),
            'proven': run_proven and not defects,
        }
    out_edges = [
        {'source': source, 'jar': jar, 'grams': edge['grams'],
         'residue_ug': _mass(edge['residue']), 'pulses': edge['pulses'],
         'proven': run_proven}
        for (source, jar), edge in sorted(edges.items())
    ]
    return {
        'run_id': run_id,
        'defects': run_defects,
        'proven': run_proven,
        'barrels': {s: {'drawn_g': drawn.get(s, 0), 'expected_g': b['grams']}
                    for s, b in barrels.items()},
        'jars': out_jars,
        'edges': out_edges,
    }
