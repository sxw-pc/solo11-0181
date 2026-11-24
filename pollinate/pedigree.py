"""Directed ancestor graph over physical breeding individuals.

Nodes are physical entities (certificates joined by accepted merge edges);
arcs point from a descendant to an ancestor and carry a generation range
``(gap_min, gap_max)`` where ``gap_max=None`` means an unbounded reachability
span kept because a paper page is missing.  Self-loops and ancestor cycles are
refused instead of being inserted, so the graph stays a DAG and every release
decision consumes only this graph.
"""
import hashlib
import json
import time
from .breeding_store import init_breeding, breeding_events, clear_derived
from .homology import resolve, union_find, short_code_collisions, entity_key

INF = None  # unbounded generation upper bound


def _combine(lo_a, hi_a, lo_b, hi_b):
    hi = INF if hi_a is INF or hi_b is INF else hi_a + hi_b
    return lo_a + lo_b, hi


def _merge_range(existing, lo, hi):
    new_lo = min(existing[0], lo)
    if existing[1] is INF or hi is INF:
        new_hi = INF
    else:
        new_hi = min(existing[1], hi)
    return new_lo, new_hi


class AncestorGraph:
    def __init__(self):
        self.adj = {}          # entity -> {parent entity: (lo, hi)}
        self.basis = {}        # (child, parent) -> (basis, evidence_id)
        self.nodes = set()

    @staticmethod
    def root_of(roots, ref):
        """Physical entity of a certificate ref, merged or standalone."""
        return roots.get(ref, entity_key(*ref))

    def add_node(self, entity):
        self.nodes.add(entity)
        self.adj.setdefault(entity, {})

    def reaches(self, start, target):
        """True if target is an ancestor of start (strict, DFS)."""
        stack = list(self.adj.get(start, {}))
        seen = set()
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self.adj.get(cur, {}))
        return False

    def add_edge(self, child, parent, lo, hi, basis, evidence_id, rejections):
        self.add_node(child)
        self.add_node(parent)
        if child == parent:
            rejections.append((evidence_id, 'self loop',
                               'entity %s cannot be its own ancestor' % child))
            return
        if self.reaches(parent, child):
            rejections.append((evidence_id, 'ancestor cycle',
                               'edge %s -> %s would close an inbreeding cycle' % (child, parent)))
            return
        slot = self.adj[child]
        if parent in slot:
            slot[parent] = _merge_range(slot[parent], lo, hi)
        else:
            slot[parent] = (lo, hi)
        self.basis[(child, parent)] = (basis, evidence_id)

    def reach_map(self, start):
        """Every ancestor reachable from start with a tight hop range.

        Range semantics: ``lo`` is the shortest guaranteed generation count;
        ``hi`` is the tightest finite upper bound or ``None`` when a missing
        page leaves the span unbounded.
        """
        out = {}

        def walk(node, lo, hi, seen):
            for parent, (edge_lo, edge_hi) in self.adj.get(node, {}).items():
                nlo, nhi = _combine(lo, hi, edge_lo, edge_hi)
                if parent in out:
                    out[parent] = _merge_range(out[parent], nlo, nhi)
                else:
                    out[parent] = (nlo, nhi)
                if parent not in seen:
                    seen.add(parent)
                    walk(parent, nlo, nhi, seen)

        walk(start, 0, 0, {start})
        return out


def _snapshot_hash(edges, merges, rejections):
    payload = json.dumps(
        dict(edges=sorted(edges), merges=sorted((m['merge_id'], m['reversed']) for m in merges),
             rejections=sorted(rejections)), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def build_graph(db, subject, now=None):
    """Replay all raw breeding evidence for subject into a versioned graph.

    Derived tables are rebuilt from scratch, so reversing an entity merge
    revises every descendant edge and every pair decision -- never just the
    pair currently under review.
    """
    init_breeding(db)
    events = breeding_events(db, subject)
    resolved = resolve(events)
    raw_roots = union_find(resolved['merges'], resolved['identities'].keys())
    # Union-find roots are certificate refs; name each component after its
    # lexicographically smallest member's deterministic entity id.
    component_name = {}
    roots = {}
    for ref, root in raw_roots.items():
        if root not in component_name:
            members = sorted(r for r, v in raw_roots.items() if v == root)
            component_name[root] = entity_key(*members[0])
        roots[ref] = component_name[root]
    identities, parents = resolved['identities'], resolved['parents']
    rejections = list(resolved['rejections'])

    with db:
        clear_derived(db, subject)
        for registry, entries in resolved['seal_prefixes'].items():
            for prefix, eid, event_time in entries:
                db.execute('INSERT OR IGNORE INTO seal_domain VALUES(?,?,?,?)',
                           (registry, prefix, event_time, eid))
        for ref, rec in sorted(identities.items()):
            db.execute('INSERT INTO registry_identity VALUES(?,?,?,?,?,?)',
                       (rec['registry'], rec['cert_no'], rec.get('local_code'),
                        rec.get('sex'), rec['event_time'], rec['evidence_id']))
        for ref, links in sorted(parents.items()):
            for link in links:
                db.execute('INSERT OR IGNORE INTO identity_parent VALUES(?,?,?,?,?,?,?)',
                           (ref[0], ref[1], link['ref'][0], link['ref'][1],
                            link['gap_min'], link['gap_max'], link['evidence_id']))
        for m in resolved['merges']:
            db.execute('INSERT OR REPLACE INTO entity_merge VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (m['merge_id'], m['basis'], m['registry_a'], m['cert_a'],
                        m['registry_b'], m['cert_b'], m['entity_id'], m['evidence_id'],
                        m['event_time'], m['received_at'], m['reversed'],
                        m['reversal_evidence_id']))
        for panel in resolved['panels'].values():
            db.execute('INSERT INTO genetic_panel VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (panel['evidence_id'], panel['panel'], panel['marker_count'],
                        panel['coverage'], panel['threshold'], panel['threshold_source'],
                        panel['calibrated'], panel['score'], panel['verdict'],
                        json.dumps(panel, ensure_ascii=False, sort_keys=True)))

        graph = AncestorGraph()
        for ref in sorted(identities):
            graph.add_node(AncestorGraph.root_of(roots, ref))
        for ref in sorted(parents):
            child = AncestorGraph.root_of(roots, ref)
            for link in sorted(parents[ref], key=lambda x: (x['ref'], x['evidence_id'])):
                parent = AncestorGraph.root_of(roots, link['ref'])
                graph.add_edge(child, parent, link['gap_min'], link['gap_max'],
                               'gap' if link['missing'] else 'paper',
                               link['evidence_id'], rejections)

        flat_edges = sorted((c, p, lo, hi, basis, eid)
                            for c, rel in graph.adj.items() for p, (lo, hi) in rel.items()
                            for (basis, eid) in [graph.basis[(c, p)]])
        content_hash = _snapshot_hash(
            [(c, p, lo, hi) for c, p, lo, hi, _, _ in flat_edges],
            resolved['merges'], rejections)
        prior = db.execute('SELECT version FROM graph_version WHERE subject=? '
                           'ORDER BY version DESC LIMIT 1', (subject,)).fetchone()
        # Identical evidence must reproduce the identical graph version so the
        # build is byte-stable under replay and re-importing the same file is
        # a no-op; new evidence opens the next version.
        match = db.execute('SELECT version FROM graph_version WHERE subject=? AND content_hash=?',
                           (subject, content_hash)).fetchone()
        if match is not None:
            version = match[0]
        else:
            version = 1 if prior is None else prior[0] + 1
        evidence_ids = sorted({e['id'] for e in events})
        db.execute('INSERT OR REPLACE INTO graph_version VALUES(?,?,?,?,?,?,?,?)',
                   (subject, version, int(now if now is not None else time.time()),
                    content_hash, sum(1 for m in resolved['merges'] if not m['reversed']),
                    len(flat_edges), len(rejections), json.dumps(evidence_ids)))
        for c, p, lo, hi, basis, eid in flat_edges:
            db.execute('INSERT OR REPLACE INTO ancestor_edge VALUES(?,?,?,?,?,?)',
                       (version, c, p, lo, hi, basis + ':' + eid))
        for eid, reason, detail in rejections:
            db.execute('INSERT OR REPLACE INTO lineage_rejection VALUES(?,?,?,?)',
                       (version, eid, reason, detail))

    state = dict(graph=graph, roots=roots, identities=identities,
                 merges=resolved['merges'], panels=resolved['panels'],
                 rejections=rejections, collisions=short_code_collisions(identities, roots),
                 version=version, content_hash=content_hash,
                 evidence_ids=sorted({e['id'] for e in events}))
    return state


def entities_for_label(state, label, refs=None):
    """Resolve a pair-sheet label to physical entities.

    Explicit ``refs`` (registry, cert) from the pair original win; otherwise
    every certificate carrying the bare local code is returned, which may be
    several distinct entities when registries reused a short code.
    """
    roots, identities = state['roots'], state['identities']
    if refs:
        out = set()
        for registry, cert in refs:
            ref = (registry, cert)
            if ref not in identities:
                raise ValueError('pair references unknown certificate %s/%s' % ref)
            out.add(AncestorGraph.root_of(roots, ref))
        return sorted(out)
    out = {AncestorGraph.root_of(roots, ref)
           for ref, rec in identities.items() if rec.get('local_code') == label}
    return sorted(out)


def common_ancestors(state, queen_entities, drone_entities):
    """Common-ancestor levels between two sets of physical entities."""
    graph = state['graph']
    roots, identities = state['roots'], state['identities']
    q_reach, d_reach = {}, {}
    for ent in queen_entities:
        for anc, rng in graph.reach_map(ent).items():
            q_reach[anc] = rng if anc not in q_reach else _merge_range(q_reach[anc], *rng)
        q_reach.setdefault(ent, (0, 0))
    for ent in drone_entities:
        for anc, rng in graph.reach_map(ent).items():
            d_reach[anc] = rng if anc not in d_reach else _merge_range(d_reach[anc], *rng)
        d_reach.setdefault(ent, (0, 0))
    certs_of = {}
    for ref in identities:
        root = AncestorGraph.root_of(roots, ref)
        certs_of.setdefault(root, []).append('%s/%s' % ref)
    levels = []
    for anc in sorted(set(q_reach) & set(d_reach)):
        qlo, qhi = q_reach[anc]
        dlo, dhi = d_reach[anc]
        levels.append(dict(entity=anc,
                           certificates=sorted(set(certs_of.get(anc, []))),
                           queen_distance=[qlo, qhi], drone_distance=[dlo, dhi],
                           level=max(qlo, dlo)))
    levels.sort(key=lambda x: (x['level'], x['entity']))
    return levels, q_reach, d_reach
