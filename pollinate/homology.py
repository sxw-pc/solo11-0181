"""Two independent routes for deciding whether two registry labels denote the
SAME physical breeding individual.

Route A (paper): a complete certificate chain -- original certificate
numbers, a transfer receipt whose seal number lies inside the issuing
registry's declared seal-number domain, and parentage records on both ends.
A short code alone never merges anything.

Route B (genetic): a calibrated high-density marker comparison with panel
coverage above the lab's calibrated threshold.  A single similar locus cannot
establish identity; an insufficient panel can also prove two labels are
different provenances only when coverage and threshold requirements are met.

Merge records are first-class and reversible.  Reversing a wrong merge does
not patch one pair: the next graph rebuild revises every descendant edge.
"""
from collections import defaultdict

# Calibrated panel requirements (synthetic-lab defaults).  A receipt may carry
# its own threshold_source/coverage requirements; these are the floors.
MIN_COVERAGE = 0.8
SAME_BODY_SCORE = 0.95
DIFFERENT_BODY_SCORE = 0.45


def entity_key(registry, cert_no):
    """Deterministic physical-entity id from a namespaced certificate."""
    return 'ent::%s::%s' % (registry, cert_no)


def seal_ok(seal_prefixes, registry, seal_no):
    prefixes = seal_prefixes.get(registry, ())
    if not prefixes:
        return False
    return any(str(seal_no).startswith(prefix) for prefix, _, _ in prefixes)


def resolve(events):
    """Turn raw breeding evidence into entity resolutions.

    Returns a dict with seal domains, registry identities, parent links,
    accepted/rejected merges, genetic panels and structured rejections.  It is
    a pure function of the evidence list so replay is reproducible.
    """
    seal_prefixes = defaultdict(list)
    identities = {}                       # (registry, cert) -> record
    parents = defaultdict(list)           # (registry, cert) -> parent refs
    panels = {}                           # evidence id -> panel row
    merges = []                           # accepted merge rows
    rejections = []                       # (evidence_id, reason, detail)

    def reject(eid, reason, detail):
        rejections.append((eid, reason, detail))

    for e in events:
        d, kind = e['data'], e['kind']
        if kind == 'seal_domain':
            for prefix in d['prefixes']:
                slot = seal_prefixes[d['registry']]
                if not any(item[0] == prefix for item in slot):
                    slot.append((prefix, e['id'], e['event_time']))
        elif kind == 'paper_record':
            ref = (d['registry'], d['cert_no'])
            if ref in identities:
                reject(e['id'], 'duplicate certificate',
                       'certificate %s/%s already registered' % ref)
                continue
            identities[ref] = dict(registry=d['registry'], cert_no=d['cert_no'],
                                   local_code=d.get('local_code'), sex=d.get('sex'),
                                   event_time=e['event_time'], evidence_id=e['id'])
            for p in d.get('parents', []):
                pref = (p['registry'], p['cert_no'])
                gap = (int(p.get('gap_min', 1)),
                       p.get('gap_max', p.get('gap_min', 1)))
                parents[ref].append(dict(ref=pref, gap_min=gap[0], gap_max=gap[1],
                                         evidence_id=e['id'], missing=bool(p.get('missing_page'))))
                if p.get('missing_page'):
                    reject(e['id'], 'missing generation page',
                           'parent %s/%s of %s/%s only reachable through a gap edge'
                           % (p['registry'], p['cert_no'], d['registry'], d['cert_no']))
        elif kind == 'page_gap':
            # A gap notice widens an already-declared parent link's range.
            ref = (d['registry'], d['cert_no'])
            pref = (d['parent_registry'], d['parent_cert'])
            link = next((x for x in parents[ref] if x['ref'] == pref), None)
            if link is None:
                parents[ref].append(dict(ref=pref, gap_min=int(d.get('gap_min', 1)),
                                         gap_max=d.get('gap_max'), evidence_id=e['id'],
                                         missing=True))
            else:
                link['gap_max'] = d.get('gap_max')
                link['missing'] = True
            reject(e['id'], 'missing generation page',
                   'lineage %s/%s -> %s/%s keeps reachability range only'
                   % (ref[0], ref[1], pref[0], pref[1]))
        elif kind == 'transfer_receipt':
            a = (d['from_registry'], d['from_cert'])
            b = (d['to_registry'], d['to_cert'])
            if not seal_ok(seal_prefixes, d['from_registry'], d.get('seal_no', '')):
                reject(e['id'], 'forged transfer seal',
                       'seal %r is outside registry %s declared domain'
                       % (d.get('seal_no'), d['from_registry']))
                continue
            if a not in identities or b not in identities:
                reject(e['id'], 'broken certificate chain',
                       'transfer %s/%s -> %s/%s lacks an original certificate on one end'
                       % (a[0], a[1], b[0], b[1]))
                continue
            merges.append(dict(merge_id='merge::' + e['id'], basis='paper_chain',
                               registry_a=a[0], cert_a=a[1], registry_b=b[0], cert_b=b[1],
                               entity_id=entity_key(*a), evidence_id=e['id'],
                               event_time=e['event_time'], received_at=e['received_at'],
                               reversed=0, reversal_evidence_id=None))
        elif kind == 'duplicate_registration':
            a = (d['registry'], d['cert_no'])
            b = (d['other_registry'], d['other_cert'])
            if a not in identities or b not in identities:
                reject(e['id'], 'broken certificate chain',
                       'duplicate link %s/%s -> %s/%s references an unknown certificate'
                       % (a[0], a[1], b[0], b[1]))
                continue
            if a[0] == b[0] and identities[a].get('local_code') != identities[b].get('local_code'):
                reject(e['id'], 'conflicting duplicate registration',
                       'same registry %s reissued certs %s and %s under different short codes'
                       % (a[0], a[1], b[1]))
                continue
            merges.append(dict(merge_id='merge::' + e['id'], basis='duplicate_registration',
                               registry_a=a[0], cert_a=a[0], registry_b=b[0], cert_b=b[1],
                               entity_id=entity_key(*a), evidence_id=e['id'],
                               event_time=e['event_time'], received_at=e['received_at'],
                               reversed=0, reversal_evidence_id=None))
        elif kind == 'genetic_receipt':
            panel = d.get('panel', {})
            coverage = float(panel.get('coverage', 0.0))
            threshold = float(panel.get('threshold', MIN_COVERAGE))
            calibrated = bool(panel.get('calibrated'))
            score = float(d.get('score', 0.0))
            enough = calibrated and coverage >= max(threshold, MIN_COVERAGE)
            if not enough:
                reject(e['id'], 'insufficient genetic panel',
                       'coverage %.2f vs threshold %.2f, calibrated=%s; a single similar '
                       'locus cannot identify a physical individual'
                       % (coverage, threshold, calibrated))
                continue
            if score >= SAME_BODY_SCORE:
                verdict = 'same_body'
            elif score <= DIFFERENT_BODY_SCORE:
                verdict = 'different_body'
            else:
                verdict = 'inconclusive'
                reject(e['id'], 'inconclusive genetic comparison',
                       'score %.3f lies between the calibrated same/different bands' % score)
            panels[e['id']] = dict(evidence_id=e['id'], panel=panel.get('name', 'hd-panel'),
                                   marker_count=int(panel.get('marker_count', 0)),
                                   coverage=coverage, threshold=threshold,
                                   threshold_source=panel.get('threshold_source', 'unattributed'),
                                   calibrated=int(calibrated), score=score, verdict=verdict,
                                   registry_a=d['registry_a'], cert_a=d['cert_a'],
                                   registry_b=d['registry_b'], cert_b=d['cert_b'])
            if verdict == 'same_body':
                a = (d['registry_a'], d['cert_a'])
                b = (d['registry_b'], d['cert_b'])
                if a not in identities or b not in identities:
                    reject(e['id'], 'broken certificate chain',
                           'genetic merge references an unknown certificate')
                    continue
                merges.append(dict(merge_id='merge::' + e['id'], basis='genetic',
                                   registry_a=a[0], cert_a=a[1], registry_b=b[0], cert_b=b[1],
                                   entity_id=entity_key(*a), evidence_id=e['id'],
                                   event_time=e['event_time'], received_at=e['received_at'],
                                   reversed=0, reversal_evidence_id=None))
        elif kind == 'merge_reversal':
            target = next((m for m in merges if m['merge_id'] == 'merge::' + d['reverses']), None)
            if target is None:
                reject(e['id'], 'unknown merge reversal',
                       'no accepted merge %s to reverse' % d['reverses'])
                continue
            target['reversed'] = 1
            target['reversal_evidence_id'] = e['id']

    return dict(seal_prefixes=dict(seal_prefixes), identities=identities,
                parents=dict(parents), merges=merges, panels=panels,
                rejections=rejections)


def union_find(merges, all_refs=()):
    """Physical entity root for every certificate, honoring only live merges.

    Every certificate is seeded, so standalone refs resolve to their own
    ``ent::registry::cert`` entity instead of leaking a raw tuple into the
    graph.
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            # Stable ordering keeps replay entity ids deterministic.
            ra, rb = sorted((ra, rb))
            parent[rb] = ra

    for ref in all_refs:
        find(ref)
    for m in merges:
        if m['reversed']:
            continue
        union((m['registry_a'], m['cert_a']), (m['registry_b'], m['cert_b']))
    return {ref: find(ref) for ref in list(parent)}


def short_code_collisions(identities, roots):
    """Bare local short codes shared by distinct physical entities.

    Return local_code -> sorted list of entity ids.  Such a collision means a
    short code cannot be treated as a global name without a namespace.
    """
    buckets = defaultdict(set)
    for ref, rec in identities.items():
        if rec.get('local_code'):
            buckets[rec['local_code']].add(roots.get(ref, entity_key(*ref)))
    return {code: sorted(ents) for code, ents in buckets.items() if len(ents) > 1}
