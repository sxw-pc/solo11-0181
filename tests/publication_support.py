"""Shared helpers for the crash-safe publication tests.

These helpers build self-contained synthetic fault fixtures (no network, no
shared mutable state) and drive the real staff -> assignment -> publish call
chain, then assert what each field party is allowed to observe.
"""
import json
import sqlite3
import tempfile
from pathlib import Path

from pollinate.store import connect
from pollinate.staff import import_day
from pollinate import publish as P
from pollinate.publish import (
    POINTER_MODE, FIXED_MODE, PublicationSuspended, VersionRollbackError,
    FAULT_AFTER_WORKERS, FAULT_AFTER_JOBS, FAULT_BEFORE_COMMIT,
    FAULT_ONE_RECEIPT,
)


class PublicationHarness:
    def __init__(self, mode=FIXED_MODE):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.export=self.root/'export'
        self.db_path=self.root/'case.db'
        self.mode=mode

    def cleanup(self):
        self.tmp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()

    # -- fixture construction -------------------------------------------- #
    def write_day(self, day_id, worker='I1', task='J1', skill='health',
                  start=1, end=3, fault=None, revoked_tasks=None,
                  extra_workers=None):
        workers=[{'id': worker, 'skills': [skill]}]
        for w in extra_workers or []:
            workers.append(w)
        payload={
            'day_id': day_id,
            'publish_dir': str(self.export),
            'publish_mode': self.mode,
            'workers': workers,
            'tasks': [{'id': task, 'skill': skill,
                       'start': start, 'end': end}],
        }
        if fault is not None:
            payload['fault']=fault
            # keep the legacy hook exercised too
            if fault == FAULT_AFTER_WORKERS:
                payload['fail_after_workers']=True
        if revoked_tasks:
            payload['revoked_tasks']=list(revoked_tasks)
        path=self.root/f'{day_id}.json'
        path.write_text(json.dumps(payload), encoding='utf8')
        return path

    def open_db(self):
        db=connect(self.db_path)
        db.row_factory=sqlite3.Row
        return db

    # -- real call chain ------------------------------------------------- #
    def import_day(self, path):
        return import_day(self.db_path, path)

    def publish(self, day_id, worker='I1', fault=None, **kw):
        return self.import_day(self.write_day(day_id, worker=worker,
                                              fault=fault, **kw))

    def restart_and_recover(self):
        """Simulate a process restart: reopen the DB and run recovery."""
        db=self.open_db()
        try:
            summary=P.recover_publish(db, str(self.export))
        finally:
            db.close()
        return summary

    # -- database assertions --------------------------------------------- #
    def roster_day_ids(self):
        db=self.open_db()
        try:
            return [r['day_id'] for r in db.execute(
                'SELECT day_id FROM field_roster ORDER BY day_id')]
        finally:
            db.close()

    def publish_rows(self):
        db=self.open_db()
        try:
            return [dict(r) for r in db.execute(
                'SELECT * FROM roster_publish ORDER BY version')]
        finally:
            db.close()

    def receipt_rows(self):
        db=self.open_db()
        try:
            return [dict(r) for r in db.execute(
                'SELECT * FROM roster_receipt ORDER BY version,party')]
        finally:
            db.close()

    # -- reader views ---------------------------------------------------- #
    def read_driver(self):
        db=self.open_db()
        try:
            if self.mode == POINTER_MODE:
                return P.read_pointer_driver(self.export, db)
            return P.read_fixed_driver(self.export, db)
        finally:
            db.close()

    def read_gate(self):
        db=self.open_db()
        try:
            if self.mode == POINTER_MODE:
                return P.read_pointer_gate(self.export, db)
            return P.read_fixed_gate(self.export, db)
        finally:
            db.close()

    def suspended(self):
        return P.suspension_status(self.export)

    # -- field receipts -------------------------------------------------- #
    def acknowledge(self, party, version, fault=None):
        db=self.open_db()
        try:
            row=db.execute(
                'SELECT content_hash FROM roster_publish WHERE publish_dir=? '
                'AND version=?', (str(self.export), version)).fetchone()
            # The one-receipt fault is raised only after the durable disk
            # receipt and its SQLite row have both been committed, so the
            # RuntimeError propagates while the observation survives.
            return P.acknowledge(self.export, party, version,
                                 row['content_hash'], db=db, fault=fault)
        finally:
            db.close()

    def ack_both(self, version, fault=None):
        """Both parties observe a version; fault may strike after the first."""
        results=[]
        for party in P.PARTIES:
            results.append(self.acknowledge(party, version, fault=fault))
        return results

    def try_resume(self):
        db=self.open_db()
        try:
            return P.try_resume(db, str(self.export))
        finally:
            db.close()

    def disk_receipts(self):
        rdir=self.export/'.publish'/P.RECEIPTS_DIR
        if not rdir.is_dir():
            return []
        return sorted(p.name for p in rdir.glob('*.receipt.json'))

    def versions_on_disk(self):
        vdir=self.export/'.publish'/P.VERSIONS_DIR
        if not vdir.is_dir():
            return []
        return sorted(int(p.name[1:]) for p in vdir.glob('v*') if p.name[1:].isdigit())
