"""Mating release decisions.

The release surface consumes the physical-entity ancestor graph and nothing
else from the raw pair sheet: direct-parent label intersection stays frozen
in ``kinship.assess`` as a provisional screen.  Per the yard contract, a pair
is held when a common ancestor is provably within two generations on both
sides; it stays pending while a missing page or an unresolved same-short-code
collision could still hide such an ancestor, and is allowed only when the
forbidden relationship is disproved.
"""
import json
import tempfile
from pathlib import Path
from .breeding_store import init_breeding, pair_raw
from .pedigree import (build_graph, common_ancestors, entities_for_label,
                       AncestorGraph)

DEFAULT_LIMIT = 2
STATUS_HOLD, STATUS_PENDING, STATUS_ALLOW = 'hold', 'pending', 'allow'


def _refs(doc, side):
    refs = doc.get(side + '_refs')
    return [tuple(r) for r in refs] if refs else None


def evaluate_pair(db, state, pair_id, limit=DEFAULT_LIMIT):
    """Decide one pair from the graph; persist a versioned breeding decision."""
    doc = pair_raw(db, pair_id)
    if doc is None:
        raise ValueError('unknown pair ' + pair_id)
    limit = int(doc.get('ancestor_limit', limit))
    q_refs, d_refs = _refs(doc, 'queen'), _refs(doc, 'drone')
    # Explicit namespaced certificate refs disambiguate entities themselves;
    # only bare-label pairs are exposed to short-code collision ambiguity.
    explicit_refs = bool(q_refs and d_refs)
    try:
        q_ents = entities_for_label(state, doc['queen_id'], q_refs)
        d_ents = entities_for_label(state, doc['drone_id'], d_refs)
    except ValueError as exc:
        return _save(db, state, pair_id, STATUS_PENDING,
                     dict(reason='unresolved certificate', detail=str(exc),
                          common_ancestors=[], limit=limit,
                          queen_entities=[], drone_entities=[]), [])

    levels, q_reach, d_reach = common_ancestors(state, q_ents, d_ents)
    evidence = set(state['evidence_ids'])
    base = dict(common_ancestors=levels, limit=limit,
                queen_entities=q_ents, drone_entities=d_ents)

    forbidden = [ca for ca in levels
                 if ca['queen_distance'][0] <= limit and ca['drone_distance'][0] <= limit]
    definite = [ca for ca in forbidden
                if ca['queen_distance'][1] is not None and ca['drone_distance'][1] is not None
                and ca['queen_distance'][1] <= limit and ca['drone_distance'][1] <= limit]
    if definite:
        result = dict(base, reason='common ancestor within %d generations' % limit)
        return _save(db, state, pair_id, STATUS_HOLD, result,
                     sorted(evidence | _edge_evidence(state, definite)))

    split_panels = _split_panels_for(state, q_ents, d_ents)
    blocked_code = None if explicit_refs else _collision_blocks(state, q_ents, d_ents)
    # An unbounded ancestor on either side means a missing page could still
    # hide a common ancestor inside the forbidden window.
    unbounded_queen = {a for a, (lo, hi) in q_reach.items() if hi is None}
    unbounded_drone = {a for a, (lo, hi) in d_reach.items() if hi is None}
    unbounded = bool(unbounded_queen or unbounded_drone)
    reasons = []
    if forbidden:
        reasons.append('common ancestor may fall within %d generations once a missing '
                       'page is resolved' % limit)
    if blocked_code and not split_panels:
        reasons.append('short code %s names several physical entities without a merge or '
                       'split receipt' % blocked_code)
    if unbounded and not definite:
        reasons.append('missing generation keeps an ancestor reachability range unbounded')
    if split_panels and not forbidden:
        result = dict(base,
                      reason='calibrated genetic panel proves distinct provenances; '
                             'shared-ancestor upper bound below the forbidden line',
                      genetic_evidence=split_panels,
                      threshold_sources=[state['panels'][pid]['threshold_source']
                                         for pid in split_panels])
        return _save(db, state, pair_id, STATUS_ALLOW, result,
                     sorted(evidence | set(split_panels)))
    if not reasons:
        result = dict(base,
                      reason='no common ancestor within %d generations on the complete graph'
                             % limit)
        return _save(db, state, pair_id, STATUS_ALLOW, result, sorted(evidence))

    result = dict(base, reason='; '.join(reasons))
    return _save(db, state, pair_id, STATUS_PENDING, result, sorted(evidence))


def _split_panels_for(state, q_ents, d_ents):
    """Different-body calibrated panels resolving the pair's disputed labels.

    The panel need not compare the two breeding individuals themselves: the
    yard dispute is whether a shared short code on an ancestor names one
    physical body.  A panel whose two compared entities are reachable from
    opposite sides (or are the side entities themselves) settles that.
    """
    graph = state['graph']
    q_ancestors, d_ancestors = set(q_ents), set(d_ents)
    for ent in q_ents:
        q_ancestors.update(graph.reach_map(ent))
    for ent in d_ents:
        d_ancestors.update(graph.reach_map(ent))
    hits = []
    for panel in state['panels'].values():
        if panel['verdict'] != 'different_body':
            continue
        a = AncestorGraph.root_of(state['roots'], (panel['registry_a'], panel['cert_a']))
        b = AncestorGraph.root_of(state['roots'], (panel['registry_b'], panel['cert_b']))
        compared = {a, b}
        if compared & q_ancestors and compared & d_ancestors:
            hits.append(panel['evidence_id'])
    return sorted(hits)


def _edge_evidence(state, ancestors):
    wanted = {ca['entity'] for ca in ancestors}
    return {eid for (child, parent), (basis, eid) in state['graph'].basis.items()
            if parent in wanted}


def _collision_blocks(state, q_ents, d_ents):
    """Short code shared by distinct physical entities on either lineage.

    A bare short code with no merge or genetic split receipt is globally
    ambiguous, so any collision whose entities lie on the queen or drone side
    (the pair entities themselves or an ancestor) blocks a clean release.
    """
    graph = state['graph']
    q_scope, d_scope = set(q_ents), set(d_ents)
    for ent in q_ents:
        q_scope.update(graph.reach_map(ent))
    for ent in d_ents:
        d_scope.update(graph.reach_map(ent))
    touched = q_scope | d_scope
    for code, ents in state['collisions'].items():
        if touched & set(ents):
            return code
    return None


def _save(db, state, pair_id, status, result, evidence_ids):
    ids = sorted(set(evidence_ids) | (set(state['evidence_ids']) if state['evidence_ids']
                                      else set()))
    value = json.dumps(result, ensure_ascii=False, sort_keys=True)
    proof = json.dumps(ids, ensure_ascii=False)
    # One decision revision per graph version: a new ancestor graph always
    # gets a fresh decision row even when the status happens to be unchanged,
    # so reversing a wrong merge leaves a full audit trail.
    same_graph = db.execute(
        'SELECT version,result,evidence_ids FROM breeding_decision WHERE pair_id=? AND '
        'graph_version=? ORDER BY version DESC LIMIT 1', (pair_id, state['version'])).fetchone()
    if same_graph and same_graph['result'] == value and same_graph['evidence_ids'] == proof:
        version = same_graph['version']
    else:
        latest = db.execute('SELECT MAX(version) v FROM breeding_decision WHERE pair_id=?',
                            (pair_id,)).fetchone()['v']
        version = 1 if latest is None else latest + 1
        db.execute('INSERT INTO breeding_decision VALUES(?,?,?,?,?,?)',
                   (pair_id, version, state['version'], status, value, proof))
    return dict(pair_id=pair_id, version=version, graph_version=state['version'],
                status=status, result=result, evidence_ids=ids)


def evaluate_all(db_path, subject, now=None):
    """Rebuild the graph from raw evidence and re-decide every stored pair.

    Because the graph is rebuilt wholesale, reversing an entity merge revises
    every descendant edge and every pair decision -- never only this pair.
    """
    from .store import connect
    db = connect(db_path)
    init_breeding(db)
    try:
        state = build_graph(db, subject, now=now)
        pair_ids = [r['id'] for r in db.execute('SELECT id FROM queen_pair ORDER BY id')]
        decisions = [evaluate_pair(db, state, pid) for pid in pair_ids]
        db.commit()
        return dict(graph_version=state['version'], content_hash=state['content_hash'],
                    rejections=[dict(evidence_id=eid, reason=reason, detail=detail)
                                for eid, reason, detail in state['rejections']],
                    decisions=decisions)
    finally:
        db.close()


def replay(db_path, subject):
    """Rebuild from raw evidence on a throwaway copy and compare stored state.

    The live database is never mutated: the backup API clones it into a temp
    file, the derived tables are recomputed there, and only the comparison
    comes back.  A tampered stored decision therefore shows as a mismatch.
    """
    from .store import connect
    live = connect(db_path)
    init_breeding(live)
    try:
        with tempfile.TemporaryDirectory() as d:
            clone_path = str(Path(d) / 'replay.db')
            clone = connect(clone_path)
            with clone:
                live.backup(clone)
            clone.close()
            live.close()

            clone = connect(clone_path)
            init_breeding(clone)
            # Capture the stored decisions BEFORE the rebuild clears derived
            # tables, otherwise the comparison would always see null storage.
            stored = {r['pair_id']: dict(graph_version=r['graph_version'], status=r['status'],
                                         result=json.loads(r['result']),
                                         evidence_ids=json.loads(r['evidence_ids']))
                      for r in clone.execute(
                          'SELECT * FROM breeding_decision d WHERE version=(SELECT MAX(version) '
                          'FROM breeding_decision WHERE pair_id=d.pair_id)')}
            state = build_graph(clone, subject)
            recomputed = {}
            for r in clone.execute('SELECT id FROM queen_pair ORDER BY id'):
                decision = evaluate_pair(clone, state, r['id'])
                recomputed[r['id']] = dict(graph_version=decision['graph_version'],
                                           status=decision['status'],
                                           result=decision['result'],
                                           evidence_ids=decision['evidence_ids'])
            clone.close()
    finally:
        if live:
            try:
                live.close()
            except Exception:
                pass

    mismatches = {pid: dict(stored=stored.get(pid), recomputed=comp)
                  for pid, comp in recomputed.items()
                  if not stored.get(pid) or _core(stored[pid]) != _core(comp)}
    return dict(match=bool(recomputed) and not mismatches, mismatches=mismatches,
                graph_version=state['version'], pair_count=len(recomputed))


def _core(d):
    return d['graph_version'], d['status'], d['result']
