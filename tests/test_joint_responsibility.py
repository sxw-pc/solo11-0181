"""Joint-spray causal responsibility and recovery allocation.

The scenarios here use the synthetic ``fixtures/joint-spray.json`` input:
two neighbouring orchards spray the same night, hive H1 dies at time 2, and
the effective dose at the hive is A=7 from Z1 plus B=6 drifting in from Z2.
The lethal threshold is 10 and doses combine additively: 7<10, 6<10, 7+6=13,
so removal of either batch alone is non-lethal and both are necessary causes.

Both entry points (dispatch -> 蜂群损失事故归因, settle -> 事故赔付责任裁定)
must report the same graph.  The old boolean time-window implementation
returns the bare label "果园方" for both; these tests first reproduce that
label on the *original* spray/dead/incident input, then assert the dose graph.
"""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import pollinate
from pollinate.store import connect, ingest, evidence, save_decisions, report
from pollinate.__main__ import run_fixture

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures/joint-spray.json"
SUBJECT = "joint-1"
DISPATCH_SURFACE = "蜂群损失事故归因"
SETTLE_SURFACE = "事故赔付责任裁定"


def load_events():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["events"]


class JointSprayBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp.name) / "joint.db")
        self.load(load_events())

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def load(self, events, subject=SUBJECT):
        with self.db:
            for event in events:
                ingest(self.db, event)
            self.recompute(subject)

    def add(self, event, subject=SUBJECT):
        with self.db:
            ingest(self.db, event)
            self.recompute(subject)

    def recompute(self, subject=SUBJECT):
        results = []
        records = evidence(self.db, subject)
        for module in pollinate.MODULES:
            results.extend(module.decide(records))
        save_decisions(self.db, subject, results)

    def surfaces(self, subject=SUBJECT):
        return report(self.db, subject)

    def result(self, surface, subject=SUBJECT):
        return self.surfaces(subject)[surface]["result"]

    def both(self):
        return self.result(DISPATCH_SURFACE), self.result(SETTLE_SURFACE)

    def graph(self):
        dispatch, settle = self.both()
        self.assertEqual(dispatch, settle, "dispatch and settle must agree")
        return dispatch


def spray(batch_id, *, zone, entity, dose, start=1, end=3, received_at=12, **extra):
    data = {
        "batch_id": batch_id,
        "orchard": entity,
        "entity": entity,
        "zone": zone,
        "start": start,
        "end": end,
        "delivered": True,
        "delivered_at": start,
        "substance": "X",
        "dose": dose,
        "dose_range": [dose, dose],
        "sample_id": f"SAMPLE-{batch_id}",
        "calibration_id": f"CAL-{batch_id}",
        "threshold_id": "LAB-TOX-2026-11",
    }
    if zone != "Z1":
        data["drift_id"] = f"DRIFT-{batch_id}"
    data.update(extra)
    return {
        "id": f"spray-{batch_id}",
        "kind": "spray_notice",
        "subject": SUBJECT,
        "event_time": received_at,
        "received_at": received_at,
        "data": data,
    }


class LegacyCompatibility(JointSprayBase):
    """The original boolean time-window label must still be reproducible."""

    def setUp(self):
        super().setUp()
        self.db.close()
        self.db = connect(Path(self.temp.name) / "legacy.db")

    def build_legacy(self):
        # exactly the old input shape: a delivered spray, an explicit dead
        # health check and an incident with time/zone; no doses anywhere
        self.load(
            [
                {"id": "legacy-spray", "kind": "spray_notice", "subject": SUBJECT,
                 "event_time": 11, "received_at": 11,
                 "data": {"delivered": True, "zone": "Z1", "start": 1, "end": 3}},
                {"id": "legacy-health", "kind": "health_check", "subject": SUBJECT,
                 "event_time": 12, "received_at": 12,
                 "data": {"healthy": False, "dead": True, "death_time": 2, "zone": "Z1"}},
                {"id": "legacy-incident", "kind": "incident", "subject": SUBJECT,
                 "event_time": 13, "received_at": 13,
                 "data": {"time": 2, "zone": "Z1", "loss": 1}},
            ]
        )

    def test_old_time_window_label_still_reproduced_by_both_entries(self):
        self.build_legacy()
        self.assertEqual(self.both(), ("果园方", "果园方"))

    def test_normal_fixtures_stay_accident_free(self):
        self.db.close()
        self.db = connect(Path(self.temp.name) / "normal.db")
        for module in pollinate.MODULES:
            run_fixture(self.db, module.NORMAL)
        self.assertEqual(self.result(DISPATCH_SURFACE, "case-1"), "无事故")
        self.assertEqual(self.result(SETTLE_SURFACE, "case-1"), "无事故")

    def test_incident_restating_loss_is_not_an_accident(self):
        # a normal settle incident (no time, no death) must not be promoted
        self.load(
            [
                {"id": "c", "kind": "contract", "subject": SUBJECT, "event_time": 1,
                 "received_at": 1, "data": {"fee": 20, "min_hours": 0}},
                {"id": "i", "kind": "incident", "subject": SUBJECT, "event_time": 2,
                 "received_at": 2, "data": {"loss": 0.4}},
            ]
        )
        self.assertEqual(self.both(), ("无事故", "无事故"))


class TwoEntryParity(JointSprayBase):
    def test_both_entry_points_reproduce_the_same_graph(self):
        dispatch = self.result(DISPATCH_SURFACE)
        settle = self.result(SETTLE_SURFACE)
        self.assertEqual(dispatch, settle)
        self.assertEqual(dispatch["mode"], "joint")
        self.assertEqual(dispatch["death_event_id"], "joint-incident-h1")
        self.assertEqual(dispatch["at"], 2)

    def test_dispatch_and_settle_evidence_ids_track_the_same_facts(self):
        surfaces = self.surfaces()
        dispatch_ids = set(surfaces[DISPATCH_SURFACE]["evidence_ids"])
        settle_ids = set(surfaces[SETTLE_SURFACE]["evidence_ids"])
        self.assertEqual(dispatch_ids, settle_ids)
        for required in ("joint-spray-a", "joint-spray-b", "joint-threshold",
                         "joint-incident-h1", "joint-health-dead"):
            self.assertIn(required, dispatch_ids)


class NecessaryCauseGraph(JointSprayBase):
    def test_two_doses_below_threshold_combine_and_each_is_necessary(self):
        graph = self.graph()
        self.assertEqual(graph["threshold"], 10)
        self.assertEqual(graph["combined_dose"], 13)
        self.assertTrue(graph["combined"]["lethal"])
        self.assertTrue(graph["combined"]["counted_once"])
        by_id = {batch["batch_id"]: batch for batch in graph["batches"]}
        self.assertEqual(by_id["BATCH-A"]["dose"], 7)
        self.assertEqual(by_id["BATCH-B"]["dose"], 6)
        for batch in by_id.values():
            self.assertLess(batch["dose"], graph["threshold"], "no batch is lethal alone")
            self.assertLess(batch["counterfactual_dose"], graph["threshold"])
            self.assertTrue(batch["necessary"])
            self.assertFalse(batch["sufficient"])
            self.assertTrue(batch["cause"])

    def test_batch_edges_carry_counterfactual_not_the_summed_total(self):
        graph = self.graph()
        batch_edges = [e for e in graph["edges"] if e["type"] == "batch"]
        combined = [e for e in graph["edges"] if e["type"] == "combined"]
        self.assertEqual(len(batch_edges), 2)
        self.assertEqual(len(combined), 1)
        self.assertEqual(
            sorted(e["dose"] for e in batch_edges), [6, 7], "edges must not repeat 13"
        )
        self.assertEqual(
            {e["from"]: e["counterfactual_dose"] for e in batch_edges},
            {"BATCH-A": 6, "BATCH-B": 7},
        )
        self.assertEqual(sum(e["dose"] for e in batch_edges), 13)
        self.assertEqual(combined[0]["dose"], 13)
        self.assertEqual(combined[0]["to"], "joint-incident-h1")

    def test_combined_total_is_not_billed_twice(self):
        graph = self.graph()
        amounts = [row["amount"] for row in graph["recovery"]]
        self.assertEqual(amounts, [7, 6])
        self.assertEqual(sum(amounts), graph["claim"]["total"])
        self.assertEqual(graph["claim"]["total"], 13)

    def test_signing_versions_differ_but_internal_doses_do_not(self):
        necessary = self.graph()
        self.assertEqual(necessary["protocol"], "joint_necessary")
        self.assertEqual(
            {k: v["fraction"] for k, v in necessary["shares"].items()},
            {"A": "7/13", "B": "6/13"},
        )
        self.assertEqual(len(necessary["claim"]["objects"]), 2)
        self.assertEqual(
            sorted(row["entity"] for row in necessary["claim"]["objects"]), ["A", "B"]
        )
        self.assertEqual(necessary["freeze_keys"],
                         ["joint-necessary:joint-incident-h1:A",
                          "joint-necessary:joint-incident-h1:B"])


class ProtocolVariants(JointSprayBase):
    def switch_to_several(self):
        # flip which signed attachment is effective; the settled property is
        # the version string, never the upload order of spray packages
        for event in load_events():
            data = event.get("data", {})
            if data.get("protocol") == "joint_necessary":
                data["effective"] = False
            if data.get("protocol") == "joint_several":
                data["effective"] = True
            yield event

    def test_several_liability_gives_one_total_claim_and_same_internal_split(self):
        self.db.close()
        self.db = connect(Path(self.temp.name) / "several.db")
        self.load(list(self.switch_to_several()))
        graph = self.graph()
        self.assertEqual(graph["protocol"], "joint_several")
        self.assertEqual(graph["protocol_version"], "joint-several-v1")
        self.assertEqual(len(graph["claim"]["objects"]), 1)
        claim = graph["claim"]["objects"][0]
        self.assertEqual(claim["amount"], 13)
        self.assertEqual(claim["debtors"], ["A", "B"])
        self.assertTrue(graph["claim"]["joint_and_several"])
        self.assertEqual(
            {k: v["fraction"] for k, v in graph["shares"].items()},
            {"A": "7/13", "B": "6/13"},
        )
        # one freeze key at the joint claim, not one per household
        self.assertEqual(graph["freeze_keys"], ["joint-several:joint-incident-h1"])

    def test_both_signed_attachments_are_present_in_the_fixture(self):
        protocols = {
            event["data"].get("protocol")
            for event in load_events()
            if event["kind"] == "contract"
        }
        self.assertEqual(protocols, {"joint_necessary", "joint_several"})

    def test_last_uploaded_spray_package_cannot_change_signed_shares(self):
        before = self.graph()
        # A late spray package that carries its own protocol/effective flag is
        # operational evidence, not a signed attachment: it must not switch the
        # governing version or renegotiate the 7:6 split.
        late = spray("BATCH-B", zone="Z2", entity="B", dose=6, received_at=90)
        late["id"] = "spray-batch-b-late"
        late["event_time"] = 90
        late["data"].update({
            "protocol": "joint_several", "effective": True, "agreement": True,
            "signed": True, "signed_by": ["A", "B"],
        })
        self.add(late)
        after = self.graph()
        self.assertEqual(after["protocol_version"], before["protocol_version"])
        self.assertEqual(after["protocol_version"], "joint-necessary-v1")
        self.assertEqual(
            {k: v["fraction"] for k, v in after["shares"].items()},
            {"A": "7/13", "B": "6/13"},
        )
        self.assertEqual(
            after["freeze_keys"],
            ["joint-necessary:joint-incident-h1:A",
             "joint-necessary:joint-incident-h1:B"],
        )


class DoseChangesCauseGraph(JointSprayBase):
    def without_batch_b(self):
        self.db.close()
        self.db = connect(Path(self.temp.name) / "single.db")
        events = [e for e in load_events()
                  if e.get("data", {}).get("batch_id") != "BATCH-B"]
        for event in events:
            data = event.get("data", {})
            if data.get("batch_id") == "BATCH-A":
                data["dose"] = 11
                data["dose_range"] = [11, 11]
                # keep the replay chain consistent with the new declared dose
                data["applied_dose"] = 11
                data.pop("calibration_factor", None)
                data.pop("drift_factor", None)
        self.load(events)

    def test_single_batch_at_eleven_changes_the_edges(self):
        self.without_batch_b()
        graph = self.graph()
        self.assertEqual(graph["batch_ids"], ["BATCH-A"])
        a = graph["batches"][0]
        self.assertEqual(a["dose"], 11)
        self.assertTrue(a["sufficient"])
        self.assertFalse(a["necessary"])
        self.assertTrue(a["cause"])
        self.assertEqual(graph["combined_dose"], 11)
        self.assertEqual(graph["shares"]["A"]["fraction"], "11/11")
        self.assertEqual([e["type"] for e in graph["edges"]], ["batch", "combined"])

    def test_time_disjoint_batch_is_not_forced_onto_the_other_household(self):
        self.db.close()
        self.db = connect(Path(self.temp.name) / "disjoint.db")
        events = deepcopy(load_events())
        for event in events:
            data = event.get("data", {})
            if data.get("batch_id") == "BATCH-B":
                data.update({"start": 4, "end": 6, "delivered_at": 4})
        self.load(events)
        graph = self.graph()
        self.assertEqual(graph["combined_dose"], 7)
        self.assertFalse(graph["payment_allowed"])
        self.assertEqual(graph["claim"]["total"], 0)
        self.assertFalse(graph["funds"]["allowed"])
        b = next(row for row in graph["batches"] if row["batch_id"] == "BATCH-B")
        self.assertEqual(b["reason"], "time_disjoint")
        self.assertFalse(b["cause"])
        self.assertFalse(graph["shares"])

    def test_missing_sampling_chain_blocks_attribution(self):
        self.db.close()
        self.db = connect(Path(self.temp.name) / "chain.db")
        events = deepcopy(load_events())
        for event in events:
            data = event.get("data", {})
            if data.get("batch_id") == "BATCH-B":
                data["sample_chain"] = False
        self.load(events)
        graph = self.graph()
        self.assertEqual(graph["combined_dose"], 13)
        self.assertFalse(graph["payment_allowed"])
        self.assertEqual(graph["reason"], "attribution_incomplete")
        b = next(row for row in graph["batches"] if row["batch_id"] == "BATCH-B")
        self.assertEqual(b["reason"], "sample_chain_missing")
        self.assertFalse(b["cause"])
        a = next(row for row in graph["batches"] if row["batch_id"] == "BATCH-A")
        self.assertEqual(a["state"], "unattributed")
        self.assertFalse(a["cause"])
        self.assertEqual(graph["claim"]["total"], 0)

    def test_cross_zone_dose_without_drift_conversion_is_not_attributed(self):
        self.db.close()
        self.db = connect(Path(self.temp.name) / "drift.db")
        events = deepcopy(load_events())
        for event in events:
            data = event.get("data", {})
            if data.get("batch_id") == "BATCH-B":
                data.pop("drift_id", None)
        self.load(events)
        graph = self.graph()
        b = next(row for row in graph["batches"] if row["batch_id"] == "BATCH-B")
        self.assertEqual(b["reason"], "sample_chain_missing")
        self.assertFalse(graph["payment_allowed"])

    def test_mixed_substances_are_not_summed_into_a_certain_cause(self):
        self.db.close()
        self.db = connect(Path(self.temp.name) / "mixed.db")
        events = deepcopy(load_events())
        for event in events:
            data = event.get("data", {})
            if data.get("batch_id") == "BATCH-B":
                data["substance"] = "Y"
        self.load(events)
        graph = self.graph()
        self.assertEqual(graph["sum_rule"], "mixed_substances")
        self.assertTrue(graph["uncertain"])
        self.assertFalse(graph["payment_allowed"])

    def test_declared_dose_must_match_the_replayed_chain(self):
        self.db.close()
        self.db = connect(Path(self.temp.name) / "mismatch.db")
        events = deepcopy(load_events())
        for event in events:
            data = event.get("data", {})
            if data.get("batch_id") == "BATCH-B":
                # applied 12 x calibration 1.0 x drift 0.5 = 6, not 9
                data["dose"] = 9
                data["dose_range"] = [9, 9]
        self.load(events)
        graph = self.graph()
        b = next(row for row in graph["batches"] if row["batch_id"] == "BATCH-B")
        self.assertEqual(b["reason"], "dose_chain_mismatch")
        self.assertFalse(b["usable"])
        replay = graph["provenance"]["dose_replay"]["BATCH-B"]
        self.assertEqual(replay["computed"], 6)
        self.assertEqual(replay["declared"], 9)
        self.assertFalse(replay["consistent"])
        self.assertFalse(graph["payment_allowed"])

    def test_provenance_replays_calibration_and_drift(self):
        graph = self.graph()
        replay = graph["provenance"]["dose_replay"]
        self.assertEqual(
            replay["BATCH-A"],
            {"applied_dose": 14, "calibration_factor": 0.5, "drift_factor": None,
             "computed": 7, "declared": 7, "consistent": True},
        )
        self.assertEqual(
            replay["BATCH-B"],
            {"applied_dose": 12, "calibration_factor": 1, "drift_factor": 0.5,
             "computed": 6, "declared": 6, "consistent": True},
        )

    def test_interval_straddling_threshold_blocks_payment(self):
        self.db.close()
        self.db = connect(Path(self.temp.name) / "interval.db")
        events = deepcopy(load_events())
        for event in events:
            data = event.get("data", {})
            if data.get("batch_id") == "BATCH-A":
                data["dose_range"] = [6, 12]
                data["dose"] = 9
                data["applied_dose"] = 9
                data.pop("calibration_factor", None)
        self.load(events)
        graph = self.graph()
        self.assertTrue(graph["uncertain"])
        self.assertEqual(graph["reason"], "dose_range_straddles_threshold")
        self.assertFalse(graph["payment_allowed"])
        self.assertFalse(graph["funds"]["allowed"])
        self.assertEqual(graph["claim"]["total"], 0)
        for batch in graph["batches"]:
            self.assertEqual(batch["state"], "possible")


class FundLedger(JointSprayBase):
    def test_freeze_keys_do_not_double_the_total(self):
        graph = self.graph()
        funds = graph["funds"]
        self.assertTrue(funds["allowed"])
        self.assertEqual(funds["claim_total"], 13)
        self.assertEqual(funds["frozen_total"], 13)
        self.assertEqual(set(funds["keys"]), set(graph["freeze_keys"]))
        self.assertEqual(funds["paid_total"], 7)

    def test_foreign_protocol_freeze_key_is_rejected(self):
        # key from the *other* signed protocol must not hold money
        self.add({
            "id": "foreign-freeze", "kind": "settlement", "subject": SUBJECT,
            "event_time": 40, "received_at": 40,
            "data": {"action": "freeze",
                     "dispute_id": "joint-several:joint-incident-h1", "amount": 13},
        })
        funds = self.graph()["funds"]
        row = next(r for r in funds["ledger"] if r["event_id"] == "foreign-freeze")
        self.assertEqual(row["state"], "rejected")
        self.assertEqual(row["reason"], "foreign_freeze_key")
        self.assertEqual(funds["frozen_total"], 13)

    def test_over_freeze_is_capped_at_the_share(self):
        self.add({
            "id": "over-freeze", "kind": "settlement", "subject": SUBJECT,
            "event_time": 41, "received_at": 41,
            "data": {"action": "freeze",
                     "dispute_id": "joint-necessary:joint-incident-h1:B", "amount": 99},
        })
        funds = self.graph()["funds"]
        row = next(r for r in funds["ledger"] if r["event_id"] == "over-freeze")
        # B is already frozen for 6; a second freeze cannot enlarge the share
        self.assertEqual(row["state"], "duplicate")
        self.assertEqual(row["amount"], 0)
        self.assertEqual(funds["frozen_total"], 13)


class ReplayAndRevisions(JointSprayBase):
    def test_replay_reproduces_the_graph_from_raw_evidence(self):
        records = evidence(self.db, SUBJECT)
        computed = {}
        for module in pollinate.MODULES:
            for surface, value, ids in module.decide(records):
                computed[surface] = {"result": value, "evidence_ids": sorted(set(ids))}
        stored = self.surfaces()
        for surface, item in computed.items():
            self.assertEqual(stored[surface]["result"], item["result"])
            self.assertEqual(stored[surface]["evidence_ids"], item["evidence_ids"])

    def test_incident_backfill_and_spray_withdrawal_keep_entries_consistent(self):
        # late incident supplement: types the same death, then withdraw batch B
        self.add({
            "id": "incident-supplement", "kind": "incident", "subject": SUBJECT,
            "event_time": 50, "received_at": 50,
            "data": {"time": 2, "zone": "Z1", "claim_amount": 13, "supplement": True},
        })
        self.assertEqual(self.both()[0], self.both()[1])

        self.add({
            "id": "batch-b-withdraw", "kind": "spray_notice", "subject": SUBJECT,
            "event_time": 51, "received_at": 51,
            "data": {"batch_id": "BATCH-B", "orchard": "B", "zone": "Z2",
                     "start": 1, "end": 3, "delivered": False,
                     "dose": 6, "sample_id": "SAMPLE-BATCH-B",
                     "calibration_id": "CAL-BATCH-B", "drift_id": "DRIFT-BATCH-B"},
        })
        dispatch, settle = self.both()
        self.assertEqual(dispatch, settle)
        self.assertFalse(dispatch["payment_allowed"])
        self.assertEqual(dispatch["combined_dose"], 7)
        self.assertEqual(dispatch["claim"]["total"], 0)

    def test_versions_append_not_overwrite(self):
        versions = self.surfaces()
        first = versions[DISPATCH_SURFACE]["version"]
        self.add({
            "id": "late-health-note", "kind": "health_check", "subject": SUBJECT,
            "event_time": 60, "received_at": 60,
            "data": {"healthy": False, "dead": True, "death_time": 2, "zone": "Z1",
                     "note": "supplement", "valid_from": 0, "valid_to": 4},
        })
        after = self.surfaces()
        self.assertGreater(after[DISPATCH_SURFACE]["version"], first)
        self.assertEqual(after[DISPATCH_SURFACE]["result"],
                         self.result(SETTLE_SURFACE))


class Provenance(JointSprayBase):
    def test_provenance_is_replayable(self):
        graph = self.graph()
        provenance = graph["provenance"]
        self.assertEqual(provenance["threshold_source"], "LAB-TOX-2026-11")
        samples = provenance["batch_samples"]
        self.assertEqual(samples["BATCH-A"]["sample_id"], "SAMPLE-A-01")
        self.assertEqual(samples["BATCH-A"]["calibration_id"], "CAL-A-01")
        self.assertEqual(samples["BATCH-B"]["drift_id"], "DRIFT-B-01")
        self.assertEqual(provenance["batch_samples"], {
            "BATCH-A": {"sample_id": "SAMPLE-A-01", "calibration_id": "CAL-A-01",
                        "drift_id": None},
            "BATCH-B": {"sample_id": "SAMPLE-B-01", "calibration_id": "CAL-B-01",
                        "drift_id": "DRIFT-B-01"},
        })
        self.assertEqual(provenance["protocol_version"], "joint-necessary-v1")
        self.assertEqual(provenance["protocol_approval_id"], "joint-agreement-necessary")
        self.assertIn("joint-pay-a", provenance["fund_event_ids"])
        for event_id in ("joint-spray-a", "joint-spray-b", "joint-threshold"):
            self.assertIn(event_id, provenance["evidence_ids"])

    def test_threshold_source_is_carried_onto_every_edge(self):
        graph = self.graph()
        for edge in graph["edges"]:
            self.assertEqual(edge["threshold"], 10)


if __name__ == "__main__":
    unittest.main()
