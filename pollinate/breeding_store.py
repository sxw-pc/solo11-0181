"""SQLite schema and raw-evidence intake for the queen mating subsystem.

The provisional screen in ``queen.py``/``kinship.py`` only intersects the
direct parent labels of a pair sheet.  This module owns the durable ledger for
the physical-individual layer instead: paper pedigree records, seal-number
domains, transfer/alias receipts, missing-generation notices, genetic panels
and entity merge/split resolutions.  Nothing here trusts a bare local short
code; every identity is namespaced by its registering institution.
"""
import json
import sqlite3

BREEDING_KINDS = (
    'seal_domain', 'paper_record', 'transfer_receipt', 'page_gap',
    'duplicate_registration', 'genetic_receipt', 'merge_reversal',
)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS breeding_evidence (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL,
        event_time INTEGER NOT NULL, received_at INTEGER NOT NULL, payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS seal_domain (
        registry TEXT NOT NULL, prefix TEXT NOT NULL, event_time INTEGER NOT NULL,
        evidence_id TEXT NOT NULL, PRIMARY KEY(registry, prefix))""",
    """CREATE TABLE IF NOT EXISTS registry_identity (
        registry TEXT NOT NULL, cert_no TEXT NOT NULL, local_code TEXT NOT NULL,
        sex TEXT, event_time INTEGER NOT NULL, evidence_id TEXT NOT NULL,
        PRIMARY KEY(registry, cert_no))""",
    """CREATE TABLE IF NOT EXISTS identity_parent (
        registry TEXT NOT NULL, cert_no TEXT NOT NULL,
        parent_registry TEXT NOT NULL, parent_cert TEXT NOT NULL,
        gap_min INTEGER NOT NULL, gap_max INTEGER,
        evidence_id TEXT NOT NULL, PRIMARY KEY(registry, cert_no, parent_registry, parent_cert))""",
    """CREATE TABLE IF NOT EXISTS entity_merge (
        merge_id TEXT PRIMARY KEY, basis TEXT NOT NULL, registry_a TEXT NOT NULL,
        cert_a TEXT NOT NULL, registry_b TEXT NOT NULL, cert_b TEXT NOT NULL,
        entity_id TEXT NOT NULL, evidence_id TEXT NOT NULL, event_time INTEGER NOT NULL,
        received_at INTEGER NOT NULL, reversed INTEGER NOT NULL DEFAULT 0,
        reversal_evidence_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS ancestor_edge (
        graph_version INTEGER NOT NULL, child_entity TEXT NOT NULL,
        parent_entity TEXT NOT NULL, gap_min INTEGER NOT NULL, gap_max INTEGER,
        basis TEXT NOT NULL,
        PRIMARY KEY(graph_version, child_entity, parent_entity))""",
    """CREATE TABLE IF NOT EXISTS lineage_rejection (
        graph_version INTEGER NOT NULL, evidence_id TEXT NOT NULL,
        reason TEXT NOT NULL, detail TEXT NOT NULL,
        PRIMARY KEY(graph_version, evidence_id))""",
    """CREATE TABLE IF NOT EXISTS graph_version (
        subject TEXT NOT NULL, version INTEGER NOT NULL,
        built_at INTEGER NOT NULL, content_hash TEXT NOT NULL,
        merge_count INTEGER NOT NULL, edge_count INTEGER NOT NULL,
        rejection_count INTEGER NOT NULL, evidence_ids TEXT NOT NULL,
        PRIMARY KEY(subject, version))""",
    """CREATE TABLE IF NOT EXISTS breeding_decision (
        pair_id TEXT NOT NULL, version INTEGER NOT NULL,
        graph_version INTEGER NOT NULL, status TEXT NOT NULL,
        result TEXT NOT NULL, evidence_ids TEXT NOT NULL,
        PRIMARY KEY(pair_id, version))""",
    """CREATE TABLE IF NOT EXISTS genetic_panel (
        evidence_id TEXT PRIMARY KEY, panel TEXT NOT NULL, marker_count INTEGER NOT NULL,
        coverage REAL NOT NULL, threshold REAL NOT NULL, threshold_source TEXT NOT NULL,
        calibrated INTEGER NOT NULL, score REAL NOT NULL, verdict TEXT NOT NULL, payload TEXT NOT NULL)""",
)


def init_breeding(db):
    for ddl in SCHEMA:
        db.execute(ddl)
    db.commit()


def _validated(event):
    for key in ('id', 'kind', 'subject', 'event_time', 'received_at'):
        if key not in event:
            raise ValueError('missing ' + key)
    if event['kind'] not in BREEDING_KINDS:
        raise ValueError('unknown breeding kind ' + str(event['kind']))
    return (event['id'], event['kind'], event['subject'], int(event['event_time']),
            int(event['received_at']),
            json.dumps(event.get('data', {}), ensure_ascii=False, sort_keys=True))


def ingest_breeding(db, event):
    """Append one raw breeding evidence event; conflicting ids are rejected."""
    current = _validated(event)
    old = db.execute(
        'SELECT kind,subject,event_time,received_at,payload FROM breeding_evidence WHERE id=?',
        (event['id'],)).fetchone()
    if old:
        if tuple(old) == current[1:]:
            return False
        raise ValueError('conflicting breeding evidence id ' + event['id'])
    db.execute('INSERT INTO breeding_evidence VALUES (?,?,?,?,?,?)', current)
    return True


def ingest_many(db, events):
    for event in events:
        ingest_breeding(db, event)


def breeding_events(db, subject):
    return [dict(id=r['id'], kind=r['kind'], subject=r['subject'],
                 event_time=r['event_time'], received_at=r['received_at'],
                 data=json.loads(r['payload']))
            for r in db.execute(
                'SELECT * FROM breeding_evidence WHERE subject=? ORDER BY event_time,received_at,id',
                (subject,))]


def pair_raw(db, pair_id):
    row = db.execute('SELECT raw_json FROM queen_pair WHERE id=?', (pair_id,)).fetchone()
    return json.loads(row['raw_json']) if row else None


def clear_derived(db, subject):
    """Reset the resolution scratch tables for a fresh replay.

    Versioned history (``graph_version``, ``ancestor_edge`` and
    ``lineage_rejection`` rows keyed by graph version, plus every
    ``breeding_decision`` revision) is deliberately kept: a rebuild that
    changes the graph inserts a NEW version instead of overwriting history,
    and an identical rebuild reproduces the same version.
    """
    db.execute('DELETE FROM genetic_panel')
    db.execute('DELETE FROM seal_domain')
    db.execute('DELETE FROM registry_identity')
    db.execute('DELETE FROM identity_parent')
    db.execute('DELETE FROM entity_merge')
