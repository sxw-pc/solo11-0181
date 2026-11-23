"""Dispute-ledger acceptance tests.

These exercise the versioned dispute -> reservation table directly and through
the SQLite store.  Cents are integers throughout; labels are ``元`` strings.

Scenarios mirror the signed protocols:
  * precedence-v1 (先受理优先): D1 12, D2 8 of a 20 元 contract
  * equal-v1 (平等分摊): D1 10, D2 10
and prove that a single ``release`` input keeps the two signed splits distinct.
"""

import json
import tempfile
import unittest
from pathlib import Path

import pollinate
from pollinate import disputes
from pollinate.store import (
    connect,
    ingest,
    evidence,
    save_decisions,
    report,
    ledger_history,
)
from pollinate.__main__ import run_fixture

ROOT = Path(__file__).resolve().parents[1]
SUBJECT = "ledger-1"
SURFACE = "争议款冻结与释放"


def settlement(event_id, received_at, data, event_time=None):
    return dict(
        id=event_id,
        kind="settlement",
        subject=SUBJECT,
        event_time=event_time if event_time is not None else received_at,
        received_at=received_at,
        data=data,
    )


def freeze(event_id, received_at, dispute_id, amount, **extra):
    return settlement(
        event_id,
        received_at,
        {"dispute_id": dispute_id, "action": "freeze", "amount": amount, **extra},
    )


def release(event_id, received_at, dispute_id):
    return settlement(
        event_id, received_at, {"dispute_id": dispute_id, "action": "release"}
    )


def withdraw(event_id, received_at, dispute_id, **extra):
    return settlement(
        event_id,
        received_at,
        {"dispute_id": dispute_id, "action": "withdraw", **extra},
    )


def pay(event_id, received_at, amount, dispute_id=None):
    return settlement(
        event_id,
        received_at,
        {"action": "pay", "amount": amount, "dispute_id": dispute_id},
    )


def late_spray(event_id="SP-LATE", received_at=40, **data):
    payload = {"delivered": True, "delivered_at": 1, "start": 1, "end": 3, "zone": "Z1"}
    payload.update(data)
    return dict(
        id=event_id,
        kind="spray_notice",
        subject=SUBJECT,
        event_time=40,
        received_at=received_at,
        data=payload,
    )


class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp.name) / "case.db")
        self.ingest_fixture(ROOT / "fixtures/ledger_base.json")
        with self.db:
            self._resettle()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def ingest_fixture(self, path):
        events = json.loads(Path(path).read_text(encoding="utf-8"))["events"]
        with self.db:
            for event in events:
                ingest(self.db, event)

    def add(self, event, resettle=True):
        with self.db:
            ingest(self.db, event)
            if resettle:
                self._resettle()

    def _resettle(self):
        records = evidence(self.db, SUBJECT)
        results = []
        for module in pollinate.MODULES:
            results.extend(module.decide(records))
        save_decisions(self.db, SUBJECT, results)

    def ledger(self):
        return report(self.db, SUBJECT)[SURFACE]["result"]

    def assert_conserved(self, ledger):
        self.assertEqual(ledger["reserved_total"] + ledger["payable"], ledger["fee"])
        # accepted payments can exceed the current payable only when coverage
        # collapsed afterwards; that excess is reported, not hidden
        self.assertEqual(
            ledger["reserved_total"] + ledger["paid_total"] + ledger["available_payable"],
            ledger["fee"] + ledger["overpaid_total"],
        )
        self.assertEqual(
            ledger["available_payable"],
            max(0, ledger["payable"] - ledger["paid_total"]),
        )
        for dispute_id, row in ledger["shares"].items():
            self.assertLessEqual(row["secured"], row["funded"])
            self.assertLessEqual(row["funded"], row["claim"])


class BalanceSample(LedgerTestBase):
    """Original 20 元 happy path and the two signed protocols."""

    def test_original_twenty_yuan_sample(self):
        ledger = self.ledger()
        self.assertEqual(ledger["fee"], 2000)
        self.assertEqual(ledger["payable"], 2000)
        self.assertEqual(ledger["reserved_total"], 0)
        self.assertEqual(ledger["dispute_order"], [])
        self.assert_conserved(ledger)

    def _two_freezes(self, protocol):
        self.add(freeze("d1", 32, "D1", 12, protocol=protocol))
        self.add(freeze("d2", 33, "D2", 12))
        return self.ledger()

    def test_precedence_protocol_initial_shares(self):
        ledger = self._two_freezes("priority")
        self.assertEqual(ledger["protocol"], "priority")
        self.assertEqual(ledger["protocol_version"], "precedence-v1")
        self.assertEqual(ledger["shares"]["D1"]["share"], 1200)
        self.assertEqual(ledger["shares"]["D2"]["share"], 800)
        self.assertEqual(ledger["reserved_total"], 2000)
        self.assertEqual(ledger["payable"], 0)
        self.assert_conserved(ledger)

    def test_equal_protocol_initial_shares_split_by_cents(self):
        ledger = self._two_freezes("equal")
        self.assertEqual(ledger["protocol"], "equal")
        self.assertEqual(ledger["protocol_version"], "equal-v1")
        self.assertEqual(ledger["shares"]["D1"]["share"], 1000)
        self.assertEqual(ledger["shares"]["D2"]["share"], 1000)
        self.assertEqual(ledger["reserved_total"], 2000)
        self.assert_conserved(ledger)

    def test_release_keeps_signed_split_under_precedence(self):
        self._two_freezes("priority")
        self.add(release("d1-release", 34, "D1"))
        ledger = self.ledger()
        # D1 vacates only its own 12; D2 keeps its signed 8
        self.assertEqual(ledger["shares"]["D1"]["secured"], 0)
        self.assertEqual(ledger["shares"]["D2"]["secured"], 800)
        self.assertEqual(ledger["shares"]["D2"]["status"], "reserved")
        self.assertEqual(ledger["reserved_total"], 800)
        self.assertEqual(ledger["available_payable"], 1200)
        self.assert_conserved(ledger)

    def test_release_keeps_signed_split_under_equal(self):
        self._two_freezes("equal")
        self.add(release("d1-release", 34, "D1"))
        ledger = self.ledger()
        self.assertEqual(ledger["shares"]["D1"]["secured"], 0)
        self.assertEqual(ledger["shares"]["D2"]["secured"], 1000)
        self.assertEqual(ledger["reserved_total"], 1000)
        self.assertEqual(ledger["available_payable"], 1000)
        self.assert_conserved(ledger)

    def test_same_release_input_yields_distinct_totals(self):
        """One release byte-for-byte cannot explain both signed splits."""
        self._two_freezes("priority")
        self.add(release("d1-release", 34, "D1"))
        precedence = self.ledger()

        other = LedgerTestBase("run")
        other.setUp()
        try:
            other.add(freeze("d1", 32, "D1", 12, protocol="equal"))
            other.add(freeze("d2", 33, "D2", 12))
            other.add(release("d1-release", 34, "D1"))
            equal = other.ledger()
        finally:
            other.tearDown()

        self.assertEqual(precedence["shares"]["D2"]["secured"], 800)
        self.assertEqual(equal["shares"]["D2"]["secured"], 1000)
        self.assertNotEqual(
            precedence["shares"]["D2"]["secured"],
            equal["shares"]["D2"]["secured"],
        )


class ProtocolVersionPinning(LedgerTestBase):
    def test_later_protocol_event_cannot_reinterpret_signed_version(self):
        self.add(freeze("d1", 32, "D1", 12, protocol="priority"))
        self.add(freeze("d2", 33, "D2", 12))
        first = self.ledger()
        self.assertEqual(first["protocol_version"], "precedence-v1")
        # a second sign-off arrives later and tries to switch to equal split
        self.add(
            settlement(
                "late-protocol",
                50,
                {"action": "protocol", "protocol": "equal", "disputes": ["D1", "D2"]},
            )
        )
        after = self.ledger()
        self.assertEqual(after["protocol_version"], "precedence-v1")
        self.assertEqual(after["shares"]["D2"]["share"], 800)
        self.assertEqual(after["approval_id"], "d1")

    def test_equal_split_remainder_rule_is_replayable(self):
        # 10 元 fee over 6 元 of claims: each claim is fully covered, 4 元 stays
        # payable; the split never inflates a claim beyond what was approved
        claims = {"A": 300, "B": 300}
        order = {"A": (1, "a"), "B": (2, "b")}
        shares, payable = disputes.allocate(1000, claims, "equal", order)
        self.assertEqual(shares, {"A": 300, "B": 300})
        self.assertEqual(payable, 400)

    def test_equal_split_odd_cents_use_largest_remainder(self):
        # 20 元 fee, claims 12 and 12 -> 10/10 exactly; 1 元 with claims 1/1/1
        # leaves one cent, awarded to the largest remainder then protocol order
        claims = {"A": 100, "B": 100, "C": 100}
        order = {"A": (1, "a"), "B": (2, "b"), "C": (3, "c")}
        shares, payable = disputes.allocate(100, claims, "equal", order)
        self.assertEqual(shares, {"A": 34, "B": 33, "C": 33})
        self.assertEqual(payable, 0)


class LateEvidenceAndRecovery(LedgerTestBase):
    def _two_freezes(self, protocol="priority"):
        self.add(freeze("d1", 32, "D1", 12, protocol=protocol))
        self.add(freeze("d2", 33, "D2", 12))

    def test_late_spray_zeroes_fee_and_revokes_reservations(self):
        self._two_freezes("priority")
        before = self.ledger()
        self.assertEqual(before["fee"], 2000)
        self.add(late_spray())
        after = self.ledger()
        self.assertEqual(after["fee"], 0)
        self.assertEqual(after["reserved_total"], 0)
        self.assertEqual(after["payable"], 0)
        self.assertEqual(after["recoverable_total"], 2000)
        self.assertEqual(after["shares"]["D1"]["revoked"], 1200)
        self.assertEqual(after["shares"]["D2"]["revoked"], 800)
        self.assertEqual(after["shares"]["D1"]["status"], "revoked")
        self.assertEqual(after["shares"]["D2"]["status"], "revoked")
        self.assert_conserved(after)

    def test_late_spray_does_not_erase_settled_payment(self):
        self._two_freezes("equal")
        self.add(release("d1-release", 34, "D1"))
        self.add(pay("p1", 35, 10))
        before = self.ledger()
        self.assertEqual(before["paid_total"], 1000)
        self.assertEqual(before["available_payable"], 0)

        self.add(late_spray())
        after = self.ledger()
        self.assertEqual(after["fee"], 0)
        # the old 10 元 payment stays on the books and is chased, not hidden
        self.assertEqual(after["paid_total"], 1000)
        self.assertEqual(after["overpaid_total"], 1000)
        self.assertEqual(after["recoverable_total"], 2000)
        self.assert_conserved(after)

    def test_history_keeps_old_credentials_and_new_version(self):
        self._two_freezes("priority")
        self.add(release("d1-release", 34, "D1"))
        self.add(late_spray())
        history = ledger_history(self.db, SUBJECT, SURFACE)
        self.assertGreaterEqual(len(history), 3)
        versions = [item["version"] for item in history]
        self.assertEqual(versions, sorted(set(versions)))
        # the last version is the zero-funding revision; an earlier version still
        # records the pre-spray 20 元 split unchanged
        old = next(
            item
            for item in history
            if item["result"]["fee"] == 2000
            and item["result"]["reserved_total"] == 800
        )
        self.assertEqual(
            {row["dispute_id"]: row["secured"] for row in old["shares"]},
            {"D1": 0, "D2": 800},
        )
        latest = history[-1]["result"]
        self.assertEqual(latest["fee"], 0)
        # D1 already vacated its 12 before the spray; only D2's live 8 is now
        # an unfunded reservation to chase
        self.assertEqual(latest["recoverable_total"], 800)
        self.assertEqual(latest["shares"]["D1"]["status"], "released")
        self.assertEqual(latest["shares"]["D2"]["status"], "revoked")
        self.assertEqual(latest["shares"]["D2"]["revoked"], 800)


class LedgerEdgeCases(LedgerTestBase):
    def test_same_time_disputes_are_ordered_by_stable_id(self):
        self.add(freeze("z-freeze", 32, "Z", 5, protocol="priority"))
        self.add(freeze("a-freeze", 32, "A", 12))
        ledger = self.ledger()
        # equal received_at -> stable id decides; a-freeze sorts before z-freeze
        self.assertEqual(ledger["dispute_order"], ["A", "Z"])
        self.assertEqual(ledger["shares"]["A"]["share"], 1200)
        self.assertEqual(ledger["shares"]["Z"]["share"], 500)

    def test_withdraw_returns_share_to_payable(self):
        self.add(freeze("d1", 32, "D1", 12, protocol="priority"))
        self.add(freeze("d2", 33, "D2", 12))
        self.add(withdraw("d2-withdraw", 34, "D2", reason="settled"))
        ledger = self.ledger()
        self.assertEqual(ledger["shares"]["D2"]["status"], "withdrawn")
        self.assertEqual(ledger["shares"]["D1"]["secured"], 1200)
        self.assertEqual(ledger["available_payable"], 800)
        self.assert_conserved(ledger)

    def test_refreeze_after_withdraw_reopens_the_dispute(self):
        self.add(freeze("d1", 32, "D1", 12, protocol="equal"))
        self.add(freeze("d2", 33, "D2", 12))
        self.add(withdraw("w1", 34, "D1"))
        self.assertEqual(self.ledger()["shares"]["D1"]["status"], "withdrawn")
        self.add(freeze("d1b", 35, "D1", 12, claim_revision=True))
        ledger = self.ledger()
        self.assertEqual(ledger["shares"]["D1"]["status"], "reserved")
        self.assertEqual(ledger["shares"]["D1"]["secured"], 1000)
        self.assertEqual(ledger["available_payable"], 0)
        # the pinned protocol survives the withdraw/re-freeze cycle
        self.assertEqual(ledger["protocol_version"], "equal-v1")

    def test_duplicate_release_is_idempotent(self):
        self.add(freeze("d1", 32, "D1", 12, protocol="priority"))
        self.add(release("d1-release", 34, "D1"))
        first = self.ledger()
        self.add(release("d1-release-again", 35, "D1"))
        second = self.ledger()
        self.assertEqual(first["available_payable"], second["available_payable"])
        self.assertEqual(second["shares"]["D1"]["status"], "released")
        self.assertEqual(second["reserved_total"], 0)

    def test_release_before_freeze_is_a_recorded_noop(self):
        self.add(release("early-release", 32, "D1"))
        self.add(freeze("d1", 33, "D1", 12, protocol="priority"))
        ledger = self.ledger()
        self.assertFalse(ledger["shares"]["D1"]["released"])
        self.assertEqual(ledger["shares"]["D1"]["secured"], 1200)

    def test_release_blocked_while_unfunded_takes_effect_after_restore(self):
        self.add(freeze("d1", 32, "D1", 12, protocol="priority"))
        self.add(late_spray("SP1", 40))
        self.assertEqual(self.ledger()["shares"]["D1"]["status"], "revoked")
        self.add(release("blocked", 41, "D1"))
        self.assertEqual(self.ledger()["shares"]["D1"]["status"], "revoked")
        # a second hive is placed over 3-5, outside the notified 1-3 spray, and
        # the bloom window widens to 1-5, restoring two billable hours
        self.add(
            dict(
                id="placement-restore",
                kind="placement",
                subject=SUBJECT,
                event_time=50,
                received_at=50,
                data={"zone": "Z1", "start": 3, "end": 5, "allocation": "second-hive"},
            )
        )
        self.add(
            dict(
                id="bloom-restore",
                kind="bloom_observation",
                subject=SUBJECT,
                event_time=51,
                received_at=51,
                data={"window": [1, 5]},
            )
        )
        restored = self.ledger()
        self.assertEqual(restored["fee"], 2000)
        self.assertEqual(restored["shares"]["D1"]["status"], "reserved")
        # ...and a fresh release, now that it holds money, clears it
        self.add(release("effective", 51, "D1"))
        cleared = self.ledger()
        self.assertEqual(cleared["shares"]["D1"]["status"], "released")
        self.assertEqual(cleared["reserved_total"], 0)

    def test_recovery_restores_reservation_after_crash(self):
        self.add(freeze("d1", 32, "D1", 12, protocol="equal"))
        self.add(freeze("d2", 33, "D2", 12))
        self.add(release("r1", 34, "D1"))
        self.add(pay("p1", 35, 10))
        self.add(late_spray())
        crashed = self.ledger()
        self.assertEqual(crashed["fee"], 0)
        self.assertEqual(crashed["overpaid_total"], 1000)
        # a second hive and a wider bloom window restore two billable hours
        self.add(
            dict(
                id="placement-restore",
                kind="placement",
                subject=SUBJECT,
                event_time=50,
                received_at=50,
                data={"zone": "Z1", "start": 3, "end": 5, "allocation": "second-hive"},
            )
        )
        self.add(
            dict(
                id="bloom-restore",
                kind="bloom_observation",
                subject=SUBJECT,
                event_time=51,
                received_at=51,
                data={"window": [1, 5]},
            )
        )
        restored = self.ledger()
        self.assertEqual(restored["fee"], 2000)
        self.assertEqual(restored["shares"]["D2"]["status"], "reserved")
        self.assertEqual(restored["reserved_total"], 1000)
        self.assertEqual(restored["paid_total"], 1000)
        self.assertEqual(restored["overpaid_total"], 0)
        self.assertEqual(restored["available_payable"], 0)
        self.assert_conserved(restored)

    def test_partial_payment_and_available_balance(self):
        self.add(freeze("d1", 32, "D1", 12, protocol="priority"))
        self.add(freeze("d2", 33, "D2", 12))
        self.add(release("d1-release", 34, "D1"))
        self.add(pay("p1", 35, 5))
        ledger = self.ledger()
        self.assertEqual(ledger["paid_total"], 500)
        self.assertEqual(ledger["available_payable"], 700)
        self.assertEqual(ledger["reserved_total"], 800)
        self.assertEqual(ledger["payments"][0]["status"], "accepted")
        self.assert_conserved(ledger)

    def test_payment_above_payable_is_rejected(self):
        self.add(freeze("d1", 32, "D1", 12, protocol="priority"))
        self.add(freeze("d2", 33, "D2", 12))
        self.add(pay("too-much", 35, 5))
        ledger = self.ledger()
        self.assertEqual(ledger["paid_total"], 0)
        self.assertEqual(ledger["payments"][0]["status"], "rejected")
        self.assertEqual(ledger["payments"][0]["payable_then"], 0)

    def test_conflicting_claim_requires_signed_revision(self):
        self.add(freeze("d1", 32, "D1", 12, protocol="priority"))
        with self.assertRaises(disputes.LedgerError):
            self.add(freeze("d1-again", 33, "D1", 15))
        self.add(freeze("d1-revised", 34, "D1", 15, claim_revision=True))
        self.assertEqual(self.ledger()["shares"]["D1"]["claim"], 1500)


class PersistedLedgerTest(LedgerTestBase):
    def test_sqlite_share_rows_match_decision_version(self):
        self.add(freeze("d1", 32, "D1", 12, protocol="equal"))
        self.add(freeze("d2", 33, "D2", 12))
        decision = report(self.db, SUBJECT)[SURFACE]
        rows = self.db.execute(
            "SELECT dispute_id, secured, protocol_version FROM dispute_share WHERE subject=? AND version=? ORDER BY dispute_id",
            (SUBJECT, decision["version"]),
        ).fetchall()
        self.assertEqual([r["dispute_id"] for r in rows], ["D1", "D2"])
        self.assertEqual([r["secured"] for r in rows], [1000, 1000])
        self.assertTrue(all(r["protocol_version"] == "equal-v1" for r in rows))

    def test_spray_revision_appends_ledger_version_and_keeps_payment_rows(self):
        self.add(freeze("d1", 32, "D1", 12, protocol="equal"))
        self.add(freeze("d2", 33, "D2", 12))
        self.add(release("d1-release", 34, "D1"))
        self.add(pay("p1", 35, 10))
        pre = report(self.db, SUBJECT)[SURFACE]["version"]
        pre_payments = [
            dict(r)
            for r in self.db.execute(
                "SELECT event_id, amount, accepted FROM payment WHERE subject=? AND version=?",
                (SUBJECT, pre),
            )
        ]
        self.add(late_spray())
        post = report(self.db, SUBJECT)[SURFACE]["version"]
        self.assertGreater(post, pre)
        kept = [
            dict(r)
            for r in self.db.execute(
                "SELECT event_id, amount, accepted FROM payment WHERE subject=? AND version=?",
                (SUBJECT, pre),
            )
        ]
        self.assertEqual(kept, pre_payments)

    def test_replay_matches_persisted_ledger(self):
        self.add(freeze("d1", 32, "D1", 12, protocol="precedence" if False else "priority"))
        self.add(freeze("d2", 33, "D2", 12))
        self.add(release("d1-release", 34, "D1"))
        records = evidence(self.db, SUBJECT)
        computed = {}
        for module in pollinate.MODULES:
            for name, value, ids in module.decide(records):
                computed[name] = (value, sorted(set(ids)))
        stored = report(self.db, SUBJECT)
        for name, (value, ids) in computed.items():
            self.assertEqual(stored[name]["result"], value)
            self.assertEqual(stored[name]["evidence_ids"], ids)


class FixtureLateSprayTest(unittest.TestCase):
    """The synthetic spray arrives at received_at=40 but was delivered at t=1."""

    def test_fixture_drives_fee_to_zero_through_bloom_and_settle(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "case.db")
            try:
                base = json.loads(
                    (ROOT / "fixtures/ledger_base.json").read_text(encoding="utf-8")
                )["events"]
                late = json.loads(
                    (ROOT / "fixtures/ledger_late_spray.json").read_text(
                        encoding="utf-8"
                    )
                )["events"]
                with db:
                    for event in base + late:
                        ingest(db, event)
                    records = evidence(db, SUBJECT)
                    results = []
                    for module in pollinate.MODULES:
                        results.extend(module.decide(records))
                    save_decisions(db, SUBJECT, results)
                now = report(db, SUBJECT)
                self.assertEqual(now["果园分区覆盖认定"]["result"], 0)
                self.assertEqual(now["合同最低覆盖结算"]["result"], 0)
                ledger = now[SURFACE]["result"]
                self.assertEqual(ledger["fee"], 0)
                self.assertEqual(ledger["reserved_total"], 0)
                self.assertEqual(ledger["recoverable_total"], 2000)
                self.assertEqual(ledger["shares"]["D1"]["revoked"], 1200)
                self.assertEqual(ledger["shares"]["D2"]["revoked"], 800)
            finally:
                db.close()

    def test_ingest_command_runs_cross_subsystem_fixtures(self):
        import contextlib
        import io

        from pollinate.__main__ import main

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            with contextlib.redirect_stdout(io.StringIO()):
                main(["init", "--db", str(path)])
                main(
                    [
                        "ingest",
                        "--db",
                        str(path),
                        "--fixture",
                        str(ROOT / "fixtures/ledger_base.json"),
                    ]
                )
                main(
                    [
                        "ingest",
                        "--db",
                        str(path),
                        "--fixture",
                        str(ROOT / "fixtures/ledger_late_spray.json"),
                    ]
                )
            db = connect(path)
            try:
                now = report(db, SUBJECT)
                self.assertEqual(len(now), 10)
                self.assertEqual(now["合同最低覆盖结算"]["result"], 0)
                self.assertEqual(now[SURFACE]["result"]["recoverable_total"], 2000)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
