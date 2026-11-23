"""Crash-safe roster publication with two reader contracts.

Publication identity is ``(publish_dir, version)``.  Versions are monotonic
integers allocated per publish directory and never reused; content identity is
the SHA-256 of the canonical assignment JSON.  Every version is persisted in
three independent places:

1. ``roster_publish`` rows in SQLite (transactional, may roll back);
2. an append-only journal under ``<dir>/.publish/journal/`` (durable, survives
   rollback and rebuilt databases — recovery's memory of what happened);
3. immutable versioned files under ``<dir>/.publish/vN/`` (the actual payload,
   written atomically and never overwritten).

Two consumer contracts share the same protocol:

* ``pointer`` mode: new-generation parties read ``current.json``.  The pointer
  is flipped atomically only after the SQLite commit, so readers observe whole
  versions and can keep executing the previous complete version during failure.
* ``fixed`` mode: legacy parties can only read the fixed filenames
  ``workers.csv`` / ``orchard_jobs.csv``.  A cooperative-issued ``STOP_WORK``
  stop-work order is raised first and cleared only when both live files and
  both party receipts agree, so a torn state is observed as an explicit pause,
  never as a split roster.

Receipts are append-only on disk (O_EXCL) and insert-only in SQLite.  Once any
party has observed vN, no older version may ever be presented as current again.
"""
import csv, hashlib, io, json, os, time
from pathlib import Path

POINTER_MODE='pointer'
FIXED_MODE='fixed'
PARTIES=('driver','gate')
STATE_STAGED='staged'
STATE_ACTIVE='active'

CURRENT_POINTER='current.json'
STOP_ORDER='STOP_WORK'
META_DIR='.publish'
JOURNAL_DIR='journal'
VERSIONS_DIR='versions'
RECEIPTS_DIR='receipts'

WORKERS_NAME='workers.csv'
JOBS_NAME='orchard_jobs.csv'

# Recognised synthetic fault points. They live in the fixture's "fault" field
# (or the legacy "fail_after_workers" alias).
FAULT_AFTER_WORKERS='after_workers'
FAULT_AFTER_JOBS='after_jobs'
FAULT_BEFORE_COMMIT='before_commit'
FAULT_ONE_RECEIPT='one_receipt'


class PublishError(RuntimeError):
    """Base class for publication protocol violations."""

class VersionRollbackError(PublishError):
    """A version already observed in the field would be downgraded."""

class PublicationMissing(PublishError):
    """A pointer names a version whose durable payload is absent."""

class PublicationSuspended(PublishError):
    """Fixed-mode readers attempted to work under an active stop-work order."""

class PublicationInconsistent(PublishError):
    """Live files / manifests / receipts disagree and need recovery."""


# --------------------------------------------------------------------------- #
# low level helpers
# --------------------------------------------------------------------------- #
def _root(publish_dir):
    return Path(publish_dir)

def _meta(publish_dir):
    return _root(publish_dir)/META_DIR

def _journal_dir(publish_dir):
    return _meta(publish_dir)/JOURNAL_DIR

def _versions_dir(publish_dir):
    return _meta(publish_dir)/VERSIONS_DIR

def _receipts_dir(publish_dir):
    return _meta(publish_dir)/RECEIPTS_DIR

def _version_dir(publish_dir, version):
    return _versions_dir(publish_dir)/f'v{version}'

def _journal_file(publish_dir, version):
    return _journal_dir(publish_dir)/f'v{version}.json'

def _stop_file(publish_dir):
    return _root(publish_dir)/STOP_ORDER

def _ensure_layout(publish_dir):
    root=_root(publish_dir)
    root.mkdir(parents=True, exist_ok=True)
    for path in (_journal_dir(publish_dir), _versions_dir(publish_dir),
                 _receipts_dir(publish_dir)):
        path.mkdir(parents=True, exist_ok=True)
    return root

def atomic_write(path, data):
    """Write bytes to *path* atomically (tmp file + fsync + replace)."""
    path=Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_name(path.name+'.tmp.'+str(os.getpid()))
    with open(tmp, 'wb') as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)
    # best effort directory durability so the rename itself survives a crash
    try:
        dir_fd=os.open(path.parent, os.O_RDONLY)
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
    except OSError:
        pass

def _dump_json(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf8')

def canonical_assignments(assigned):
    """Stable textual form whose hash identifies roster content."""
    return _dump_json(assigned)

def content_hash_of(assigned):
    return hashlib.sha256(canonical_assignments(assigned)).hexdigest()

def render_workers_csv(day_id, assigned):
    buf=io.StringIO()
    writer=csv.writer(buf)
    writer.writerow(['day_id','worker','task'])
    for a in assigned:
        writer.writerow([day_id, a['worker'], a['task']])
    return buf.getvalue().encode('utf8')

def render_jobs_csv(day_id, assigned):
    buf=io.StringIO()
    writer=csv.writer(buf)
    writer.writerow(['day_id','task','worker','start','end'])
    for a in assigned:
        writer.writerow([day_id, a['task'], a['worker'], a['start'], a['end']])
    return buf.getvalue().encode('utf8')

def _sha(data):
    return hashlib.sha256(data).hexdigest()

def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf8'))

def _write_manifest(version_dir, record, workers_bytes, jobs_bytes):
    manifest={
        'version': record['version'],
        'day_id': record['day_id'],
        'mode': record['mode'],
        'content_hash': record['content_hash'],
        'revoked_ids': record['revoked_ids'],
        'files': {
            WORKERS_NAME: _sha(workers_bytes),
            JOBS_NAME: _sha(jobs_bytes),
        },
    }
    atomic_write(version_dir/'manifest.json', _dump_json(manifest))

def _allocated_versions(publish_dir):
    """Versions known to durable disk storage — survives SQLite rollback."""
    versions=set()
    jdir=_journal_dir(publish_dir)
    if jdir.is_dir():
        for path in jdir.glob('v*.json'):
            stem=path.name[1:-5]
            if stem.isdigit():
                versions.add(int(stem))
    vdir=_versions_dir(publish_dir)
    if vdir.is_dir():
        for path in vdir.glob('v*'):
            stem=path.name[1:]
            if stem.isdigit():
                versions.add(int(stem))
    return versions

def _next_version(publish_dir):
    known=_allocated_versions(publish_dir)
    return (max(known)+1) if known else 1

def _write_journal(publish_dir, record):
    atomic_write(_journal_file(publish_dir, record['version']), _dump_json(record))

def _journal_exists(publish_dir, version):
    return _journal_file(publish_dir, version).exists()

def _db_row(db, publish_dir, version):
    return db.execute(
        'SELECT * FROM roster_publish WHERE publish_dir=? AND version=?',
        (str(publish_dir), version)).fetchone()

def _insert_db_row(db, record, state, committed_at=None):
    db.execute('INSERT OR REPLACE INTO roster_publish VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (str(record['publish_dir']), record['version'], record['day_id'],
         record['mode'], state, record['content_hash'],
         record['workers_hash'], record['jobs_hash'],
         record['assignments_json'], json.dumps(record['revoked_ids'], sort_keys=True),
         record['created_at'], committed_at))

def _stop_active(publish_dir):
    return _stop_file(publish_dir).exists()

def _raise_stop(publish_dir, version, reason):
    stop=_stop_file(publish_dir)
    atomic_write(stop, _dump_json({
        'publish_dir': str(publish_dir),
        'version': version,
        'reason': reason,
        'raised_at': time.time_ns(),
    }))

def _clear_stop(publish_dir):
    stop=_stop_file(publish_dir)
    if stop.exists():
        stop.unlink()

# --------------------------------------------------------------------------- #
# monotonicity guard
# --------------------------------------------------------------------------- #
def max_observed_version(publish_dir, db=None):
    """Highest version any party has a durable receipt for.

    Disk receipts are authoritative: they are written O_EXCL and append-only,
    so they survive SQLite rollback. SQLite receipts are folded in as well.
    """
    versions=set()
    rdir=_receipts_dir(publish_dir)
    if rdir.is_dir():
        for path in rdir.glob('v*.receipt.json'):
            stem=path.name[1:].split('.', 1)[0]
            if stem.isdigit():
                versions.add(int(stem))
    if db is not None:
        for row in db.execute(
                'SELECT version FROM roster_receipt WHERE publish_dir=?',
                (str(publish_dir),)):
            versions.add(row['version'])
    return max(versions) if versions else 0

def _guard_no_downgrade(publish_dir, target_version, db):
    observed=max_observed_version(publish_dir, db)
    if target_version < observed:
        raise VersionRollbackError(
            f'refusing to present v{target_version}: field already observed v{observed}')

# --------------------------------------------------------------------------- #
# phase 1 — durable staging, runs inside the caller's SQLite transaction
# --------------------------------------------------------------------------- #
def publish_roster(db, day_id, publish_dir, assigned, mode=FIXED_MODE,
                   revoked_ids=None, fault=None, fail_after_workers=False):
    """Stage a new roster version and return a post-commit activation plan.

    All SQLite writes participate in *db*'s open transaction.  Disk payloads
    and the journal are made durable before this returns; if the transaction
    later rolls back, the journal still records the attempt and recovery can
    rebuild the database without reusing the version number.
    """
    if fail_after_workers and not fault:
        fault=FAULT_AFTER_WORKERS
    revoked_ids=sorted(revoked_ids or [])
    publish_dir=str(publish_dir)
    _ensure_layout(publish_dir)

    # Idempotent re-publication: identical content already published in this
    # directory reuses the existing version instead of minting a new one.
    # Both the SQLite state and the durable journal are consulted, so a
    # post-recovery re-import of a rolled-back day still dedupes.
    digest=content_hash_of(assigned)
    prior=db.execute(
        'SELECT * FROM roster_publish WHERE publish_dir=? AND state=? '
        'ORDER BY version DESC LIMIT 1', (publish_dir, STATE_ACTIVE)).fetchone()
    if prior is not None and (prior['content_hash'] != digest
                               or prior['mode'] != mode
                               or prior['day_id'] != day_id):
        prior=None
    if prior is None:
        for ver in sorted(_allocated_versions(publish_dir), reverse=True):
            jpath=_journal_file(publish_dir, ver)
            if jpath.exists():
                candidate=_read_json(jpath)
                if candidate.get('content_hash') == digest \
                        and candidate.get('mode') == mode \
                        and candidate.get('day_id') == day_id:
                    class _JournalRow(dict):
                        __getattr__=dict.get
                    prior=_JournalRow(
                        version=ver,
                        workers_hash=candidate['workers_hash'],
                        jobs_hash=candidate['jobs_hash'],
                        created_at=candidate['created_at'])
                    break
    if prior is not None and not fault:
        record={
            'publish_dir': publish_dir,
            'version': prior['version'],
            'day_id': day_id,
            'mode': mode,
            'content_hash': digest,
            'workers_hash': prior['workers_hash'],
            'jobs_hash': prior['jobs_hash'],
            'assignments_json': canonical_assignments(assigned).decode('utf8'),
            'revoked_ids': revoked_ids,
            'created_at': prior['created_at'],
        }
        return _Activation(publish_dir, prior['version'], mode, record,
                           resumed_pause=_stop_active(publish_dir))

    version=_next_version(publish_dir)
    workers_bytes=render_workers_csv(day_id, assigned)
    jobs_bytes=render_jobs_csv(day_id, assigned)
    record={
        'publish_dir': publish_dir,
        'version': version,
        'day_id': day_id,
        'mode': mode,
        'content_hash': content_hash_of(assigned),
        'workers_hash': _sha(workers_bytes),
        'jobs_hash': _sha(jobs_bytes),
        'assignments_json': canonical_assignments(assigned).decode('utf8'),
        'revoked_ids': revoked_ids,
        'created_at': time.time_ns(),
    }

    already_paused=_stop_active(publish_dir)
    if mode == FIXED_MODE and not already_paused:
        # Raise the cooperative pause BEFORE touching any payload, so a fault
        # at any later point leaves both fixed-name readers explicitly
        # suspended instead of exposed to a half-swapped pair of files.
        _raise_stop(publish_dir, version, f'publishing v{version}')

    # The journal is the first durable artefact and carries the full record
    # (including the canonical assignments). A crash after this point can be
    # rebuilt even if neither CSV payload made it to disk.
    staged_record=dict(record, journal_state=STATE_STAGED)
    if not _journal_exists(publish_dir, version):
        _write_journal(publish_dir, staged_record)

    version_dir=_version_dir(publish_dir, version)
    version_dir.mkdir(parents=True, exist_ok=True)
    # Immutable payloads: never overwrite an existing version's files.
    workers_path=version_dir/WORKERS_NAME
    if not workers_path.exists():
        atomic_write(workers_path, workers_bytes)
    if fault == FAULT_AFTER_WORKERS:
        raise RuntimeError('simulated failure after workers.csv')
    jobs_path=version_dir/JOBS_NAME
    if not jobs_path.exists():
        atomic_write(jobs_path, jobs_bytes)
    if fault == FAULT_AFTER_JOBS:
        raise RuntimeError('simulated failure after orchard_jobs.csv')

    # Manifest last: its presence marks the version payload complete.
    _write_manifest(version_dir, record, workers_bytes, jobs_bytes)
    _write_journal(publish_dir, dict(record, journal_state=STATE_ACTIVE))

    _insert_db_row(db, record, STATE_STAGED)

    if fault == FAULT_BEFORE_COMMIT:
        raise RuntimeError('simulated failure before database commit')

    return _Activation(publish_dir, version, mode, record,
                       resumed_pause=already_paused)


class _Activation:
    """Post-commit boundary switch. Call :meth:`run` after the DB commits."""

    def __init__(self, publish_dir, version, mode, record, resumed_pause):
        self.publish_dir=publish_dir
        self.version=version
        self.mode=mode
        self.record=record
        self.resumed_pause=resumed_pause

    def run(self, db):
        if self.mode == POINTER_MODE:
            self._run_pointer(db)
        else:
            self._run_fixed(db)
        return self.version

    # -- pointer mode ----------------------------------------------------- #
    def _run_pointer(self, db):
        _guard_no_downgrade(self.publish_dir, self.version, db)
        pointer=_meta(self.publish_dir)/CURRENT_POINTER
        current=None
        if pointer.exists():
            current=_read_json(pointer)
        if not current or current.get('version') != self.version:
            atomic_write(pointer, _dump_json({
                'version': self.version,
                'day_id': self.record['day_id'],
                'content_hash': self.record['content_hash'],
                'workers_hash': self.record['workers_hash'],
                'jobs_hash': self.record['jobs_hash'],
            }))
        self._mark_active(db)

    # -- fixed mode ------------------------------------------------------- #
    def _run_fixed(self, db):
        _guard_no_downgrade(self.publish_dir, self.version, db)
        root=_root(self.publish_dir)
        version_dir=_version_dir(self.publish_dir, self.version)
        # Verify the immutable payload before exposing it under live names.
        self._verify_version_files(version_dir)
        atomic_write(root/WORKERS_NAME,
                     (version_dir/WORKERS_NAME).read_bytes())
        atomic_write(root/JOBS_NAME,
                     (version_dir/JOBS_NAME).read_bytes())
        self._mark_active(db)
        if not self.resumed_pause:
            # Healthy in-process publish: both swaps already succeeded and the
            # manifests agree, so the pause can lift immediately. A pause that
            # was already active before this publish is a recovery case: it may
            # only clear via try_resume() once both field receipts land.
            if self._live_matches_version(db, self.version):
                _clear_stop(self.publish_dir)

    def _mark_active(self, db):
        # Activation runs after the caller's staging transaction committed;
        # manage our own short transaction instead of leaving one open.
        with db:
            db.execute(
                'UPDATE roster_publish SET state=?, committed_at=? '
                'WHERE publish_dir=? AND version=?',
                (STATE_ACTIVE, time.time_ns(), self.publish_dir, self.version))

    def _verify_version_files(self, version_dir):
        manifest=_read_json(version_dir/'manifest.json')
        for name, expected in manifest['files'].items():
            path=version_dir/name
            if not path.exists() or _sha(path.read_bytes()) != expected:
                raise PublicationInconsistent(
                    f'version payload {name} missing or corrupt: {path}')

    def _live_matches_version(self, db, version):
        try:
            driver=read_fixed_driver(self.publish_dir, db, allow_suspended=True)
            gate=read_fixed_gate(self.publish_dir, db, allow_suspended=True)
        except PublicationSuspended:
            return False
        return driver['version'] == version and gate['version'] == version


# --------------------------------------------------------------------------- #
# receipts — append-only evidence of what the field actually saw
# --------------------------------------------------------------------------- #
def acknowledge(publish_dir, party, version, content_hash, db=None, fault=None):
    """Record that *party* observed a complete, hash-checked version.

    The disk receipt is created O_EXCL so an existing acknowledgement can
    never be overwritten; the SQLite row is insert-only. Re-acknowledging the
    same version is an idempotent no-op. Acknowledging anything other than the
    exact published content is rejected.
    """
    if party not in PARTIES:
        raise ValueError(f'unknown party {party!r}')
    publish_dir=str(publish_dir)
    record=db.execute(
        'SELECT * FROM roster_publish WHERE publish_dir=? AND version=?',
        (publish_dir, version)).fetchone()
    if record is None:
        raise PublicationMissing(f'no publication v{version} known to database')
    if record['content_hash'] != content_hash:
        raise PublicationInconsistent(
            f'party {party} observed content {content_hash[:12]} but '
            f'v{version} is {record["content_hash"][:12]}')
    # Monotonicity: a party can never sign for a version older than the
    # highest one already observed in the field.
    observed=max_observed_version(publish_dir, db)
    if version < observed:
        raise VersionRollbackError(
            f'party {party} cannot acknowledge v{version}: '
            f'v{observed} was already observed in the field')

    rdir=_receipts_dir(publish_dir)
    rdir.mkdir(parents=True, exist_ok=True)
    receipt_path=rdir/f'v{version}.{party}.receipt.json'
    if not receipt_path.exists():
        # O_EXCL: an existing observation can never be overwritten. A crash
        # between this write and the SQLite insert still leaves durable
        # evidence on disk that sync_receipts() reconciles on restart.
        fd=os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, _dump_json({
                'publish_dir': publish_dir, 'party': party,
                'version': version, 'content_hash': content_hash,
                'received_at': time.time_ns(),
            }))
            os.fsync(fd)
        finally:
            os.close(fd)
    # Receipts are field evidence in their own transaction: they must survive
    # a fault that interrupts the surrounding acknowledgement workflow.
    with db:
        db.execute('INSERT OR IGNORE INTO roster_receipt VALUES(?,?,?,?,?)',
            (publish_dir, version, party, content_hash, time.time_ns()))
    if fault == FAULT_ONE_RECEIPT and party == PARTIES[0]:
        raise RuntimeError('simulated failure after only one party receipt')
    return version

def sync_receipts(db, publish_dir):
    """Reconcile durable disk receipts into the insert-only SQLite table."""
    rdir=_receipts_dir(publish_dir)
    if not rdir.is_dir():
        return 0
    count=0
    for path in sorted(rdir.glob('v*.receipt.json')):
        payload=_read_json(path)
        cur=db.execute(
            'SELECT 1 FROM roster_receipt WHERE publish_dir=? AND version=? AND party=?',
            (str(publish_dir), payload['version'], payload['party']))
        if cur.fetchone() is None:
            db.execute('INSERT OR IGNORE INTO roster_receipt VALUES(?,?,?,?,?)',
                (str(publish_dir), payload['version'], payload['party'],
                 payload['content_hash'], payload['received_at']))
            count += 1
    return count

def _receipt_parties(db, publish_dir, version):
    rows=db.execute(
        'SELECT party FROM roster_receipt WHERE publish_dir=? AND version=?',
        (str(publish_dir), version)).fetchall()
    return {r['party'] for r in rows}


# --------------------------------------------------------------------------- #
# recovery — rebuild DB state from the durable journal and clear half-states
# --------------------------------------------------------------------------- #
def recover_publish(db, publish_dir):
    """Reconcile SQLite with disk after a crash or a rebuilt database.

    Journal entries whose SQLite rows vanished in a rollback are rebuilt as
    active versions (their payloads are durable). A pointer that never got
    flipped is flipped now; a fixed-mode stop order is left in place until both
    parties acknowledge the rebuilt version and :func:`try_resume` succeeds.
    Returns a summary dict.
    """
    publish_dir=str(publish_dir)
    summary={'rebuilt': [], 'pointer_advanced': None, 'resumed': False,
             'suspended_version': None, 'receipts_synced': 0}
    with db:
        summary['receipts_synced']=sync_receipts(db, publish_dir)
    journals=[]
    jdir=_journal_dir(publish_dir)
    if jdir.is_dir():
        for path in sorted(jdir.glob('v*.json')):
            journals.append(_read_json(path))
    journals.sort(key=lambda r: r['version'])
    for record in journals:
        version=record['version']
        version_dir=_version_dir(publish_dir, version)
        version_dir.mkdir(parents=True, exist_ok=True)
        assigned=json.loads(record['assignments_json'])
        workers_bytes=render_workers_csv(record['day_id'], assigned)
        jobs_bytes=render_jobs_csv(record['day_id'], assigned)
        # Rebuild any payload that the crash prevented from landing. Payloads
        # are content-addressed by the journal hashes, so this never invents
        # data — it only re-materialises what the version already committed to.
        workers_path=version_dir/WORKERS_NAME
        if not workers_path.exists() or \
                _sha(workers_path.read_bytes()) != record['workers_hash']:
            atomic_write(workers_path, workers_bytes)
        jobs_path=version_dir/JOBS_NAME
        if not jobs_path.exists() or \
                _sha(jobs_path.read_bytes()) != record['jobs_hash']:
            atomic_write(jobs_path, jobs_bytes)
        if not (version_dir/'manifest.json').exists():
            _write_manifest(version_dir, record, workers_bytes, jobs_bytes)
        _write_journal(publish_dir, dict(record, journal_state=STATE_ACTIVE))
        with db:
            row=_db_row(db, publish_dir, version)
            if row is None:
                _insert_db_row(db, record, STATE_ACTIVE, time.time_ns())
                summary['rebuilt'].append(version)
            elif row['state'] != STATE_ACTIVE:
                db.execute(
                    'UPDATE roster_publish SET state=? WHERE publish_dir=? AND version=?',
                    (STATE_ACTIVE, publish_dir, version))
                summary['rebuilt'].append(version)
        if record['mode'] == POINTER_MODE:
            pointer=_meta(publish_dir)/CURRENT_POINTER
            current=_read_json(pointer) if pointer.exists() else None
            if current is None or current.get('version', 0) < version:
                is_latest=(not any(j['version'] > version for j in journals))
                if is_latest:
                    _guard_no_downgrade(publish_dir, version, db)
                atomic_write(pointer, _dump_json({
                    'version': version,
                    'day_id': record['day_id'],
                    'content_hash': record['content_hash'],
                    'workers_hash': record['workers_hash'],
                    'jobs_hash': record['jobs_hash'],
                }))
                summary['pointer_advanced']=version
        elif record['mode'] == FIXED_MODE:
            # Promote the rebuilt immutable payload to the fixed live names.
            # STOP_WORK stays raised until try_resume() verifies both receipts.
            # The downgrade guard applies only when this version is actually
            # the latest one being presented; rebuilding historical versions
            # below an already-observed newer one must stay possible.
            is_latest=(not any(j['version'] > version for j in journals))
            if is_latest:
                _guard_no_downgrade(publish_dir, version, db)
            root=_root(publish_dir)
            atomic_write(root/WORKERS_NAME,
                         (version_dir/WORKERS_NAME).read_bytes())
            atomic_write(root/JOBS_NAME,
                         (version_dir/JOBS_NAME).read_bytes())
            if not _stop_active(publish_dir):
                _raise_stop(publish_dir, version,
                            f'recovering publication v{version}')
    if journals and journals[-1]['mode'] == FIXED_MODE:
        latest=journals[-1]['version']
        with db:
            resumed=try_resume(db, publish_dir)
        if resumed:
            summary['resumed']=True
        elif _stop_active(publish_dir):
            summary['suspended_version']=latest
    return summary

def try_resume(db, publish_dir):
    """Lift a fixed-mode stop order only when the whole boundary agrees.

    Conditions: live CSVs hash-match the latest active version's manifest and
    both parties hold receipts for that exact version. Never downgrades past
    the highest version observed in the field.
    """
    publish_dir=str(publish_dir)
    row=db.execute(
        'SELECT * FROM roster_publish WHERE publish_dir=? '
        'ORDER BY version DESC LIMIT 1', (publish_dir,)).fetchone()
    if row is None:
        return False
    version=row['version']
    _guard_no_downgrade(publish_dir, version, db)
    if row['state'] != STATE_ACTIVE:
        return False
    version_dir=_version_dir(publish_dir, version)
    manifest_path=version_dir/'manifest.json'
    if not manifest_path.exists():
        return False
    manifest=_read_json(manifest_path)
    root=_root(publish_dir)
    for name, expected in manifest['files'].items():
        live=root/name
        if not live.exists() or _sha(live.read_bytes()) != expected:
            return False
    if _receipt_parties(db, publish_dir, version) != set(PARTIES):
        return False
    _clear_stop(publish_dir)
    return True


# --------------------------------------------------------------------------- #
# reader contract — what the two field parties are allowed to observe
# --------------------------------------------------------------------------- #
def _check_payload(version_dir, name, expected_hash):
    path=version_dir/name
    if not path.exists():
        raise PublicationMissing(f'published payload absent: {path}')
    data=path.read_bytes()
    if _sha(data) != expected_hash:
        raise PublicationInconsistent(f'payload hash mismatch: {path}')
    return data

def read_pointer_driver(publish_dir, db):
    """New-generation driver: whole-version reads via the versioned pointer."""
    publish_dir=str(publish_dir)
    pointer=_meta(publish_dir)/CURRENT_POINTER
    if not pointer.exists():
        raise PublicationMissing('no published roster pointer')
    current=_read_json(pointer)
    version=current['version']
    _guard_no_downgrade(publish_dir, version, db)
    data=_check_payload(_version_dir(publish_dir, version),
                        WORKERS_NAME, current['workers_hash'])
    return {'version': version, 'day_id': current['day_id'],
            'content_hash': current['content_hash'],
            'workers_csv': data.decode('utf8')}

def read_pointer_gate(publish_dir, db):
    """New-generation gate: same pointer, same version, jobs payload."""
    publish_dir=str(publish_dir)
    pointer=_meta(publish_dir)/CURRENT_POINTER
    if not pointer.exists():
        raise PublicationMissing('no published roster pointer')
    current=_read_json(pointer)
    version=current['version']
    _guard_no_downgrade(publish_dir, version, db)
    data=_check_payload(_version_dir(publish_dir, version),
                        JOBS_NAME, current['jobs_hash'])
    return {'version': version, 'day_id': current['day_id'],
            'content_hash': current['content_hash'],
            'jobs_csv': data.decode('utf8')}

def _fixed_live_version(db, publish_dir):
    """Resolve which version the live fixed-name files actually carry."""
    publish_dir=str(publish_dir)
    row=db.execute(
        'SELECT * FROM roster_publish WHERE publish_dir=? AND state=? '
        'ORDER BY version DESC LIMIT 1', (publish_dir, STATE_ACTIVE)).fetchone()
    if row is None:
        raise PublicationMissing('no published roster')
    version=row['version']
    root=_root(publish_dir)
    workers=root/WORKERS_NAME
    jobs=root/JOBS_NAME
    if not workers.exists() or not jobs.exists():
        raise PublicationMissing('live roster files absent')
    if _sha(workers.read_bytes()) != row['workers_hash'] or \
       _sha(jobs.read_bytes()) != row['jobs_hash']:
        raise PublicationInconsistent(
            'live roster files do not match the latest published version')
    _guard_no_downgrade(publish_dir, version, db)
    return row

def read_fixed_driver(publish_dir, db, allow_suspended=False):
    """Legacy driver view. Under STOP_WORK this is an explicit suspension."""
    publish_dir=str(publish_dir)
    if _stop_active(publish_dir) and not allow_suspended:
        stop=_read_json(_stop_file(publish_dir))
        raise PublicationSuspended(
            f'roster suspended pending version v{stop.get("version")}')
    row=_fixed_live_version(db, publish_dir)
    return {'version': row['version'], 'day_id': row['day_id'],
            'content_hash': row['content_hash'],
            'workers_csv': (_root(publish_dir)/WORKERS_NAME).read_text('utf8'),
            'suspended': _stop_active(publish_dir)}

def read_fixed_gate(publish_dir, db, allow_suspended=False):
    """Legacy gate view — same suspension semantics as the driver."""
    publish_dir=str(publish_dir)
    if _stop_active(publish_dir) and not allow_suspended:
        stop=_read_json(_stop_file(publish_dir))
        raise PublicationSuspended(
            f'roster suspended pending version v{stop.get("version")}')
    row=_fixed_live_version(db, publish_dir)
    return {'version': row['version'], 'day_id': row['day_id'],
            'content_hash': row['content_hash'],
            'jobs_csv': (_root(publish_dir)/JOBS_NAME).read_text('utf8'),
            'suspended': _stop_active(publish_dir)}

def suspension_status(publish_dir):
    """Plain inspection helper for tests and operators."""
    stop=_stop_file(publish_dir)
    if not stop.exists():
        return {'suspended': False}
    payload=_read_json(stop)
    return {'suspended': True, 'version': payload.get('version'),
            'reason': payload.get('reason')}


# --------------------------------------------------------------------------- #
# backwards-compatible shim used by the original direct-export call chain
# --------------------------------------------------------------------------- #
def write_two(db, day_id, output_dir, fail_after_workers=False, mode=None,
              fault=None, revoked_ids=None):
    """Compatibility wrapper around the staged publication protocol.

    Stages inside a transaction and performs the post-commit activation
    immediately, so callers using the old signature still get the fixed
    ``workers.csv`` / ``orchard_jobs.csv`` exports — now with a real
    publication boundary instead of two bare overwrites.
    """
    from .assignment import _load_day
    day=_load_day(db, day_id)
    mode=mode or day.get('publish_mode', FIXED_MODE)
    fault=fault or day.get('fault')
    if fail_after_workers and not fault:
        fault=FAULT_AFTER_WORKERS
    assigned=json.loads(
        db.execute('SELECT assignments_json FROM field_roster WHERE day_id=?',
                   (day_id,)).fetchone()['assignments_json'])
    with db:
        plan=publish_roster(db, day_id, output_dir, assigned, mode=mode,
                            revoked_ids=revoked_ids, fault=fault)
    plan.run(db)
    return plan.version
