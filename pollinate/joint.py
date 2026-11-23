"""Joint-spray causal responsibility and recovery allocation.

The module is a pure function of the evidence list.  Both ``dispatch`` and
``settle`` call :func:`snapshot` so their responsibility output is necessarily
the same, and ``replay`` can recompute it from the raw evidence.

Why a boolean time window is not enough
---------------------------------------
A hive can die while two neighbouring orchards are spraying the same night.
Neither batch may reach the lethal threshold alone, yet the two effective doses
at the hive sum past it.  Removing either batch then drops the counterfactual
dose below the threshold, so both batches are *necessary causes* of one death.
The graph therefore keeps:

* one edge per batch -> death event, carrying the dose and the dose without
  that batch (the counterfactual);
* exactly one combined-exposure edge carrying the summed dose.  The sum is not
  duplicated onto each individual edge.

Doses may be intervals.  If an interval straddles the threshold, the batch is
reported as a *possible* cause only.  ``payment_allowed`` is false and the fund
ledger blocks any freeze/payment; money is never released from a centre value.

Signed protocols
----------------
Both signed attachments see the same doses and the same causal graph but give
different external claim objects and dispute-freeze keys:

``joint_necessary``
    Both entities are confirmed un-excludable.  Internal recovery follows the
    actual contribution doses (7:6 in the reference fixture); the victim gets a
    single non-duplicated total claim, recoverable per entity share.

``joint_several``
    A and B are jointly and severally liable to the victim: one total claim
    against both.  Internally the same 7:6 split applies.  The victim never gets
    two full invoices.

The first fully signed agreement (by ``approved_at``, then receipt order) pins
the protocol version.  A later-uploaded spray package cannot change the signed
legal shares; it can only change what each share is worth (revoked / recoverable)
in a new version.

Evidence model (all optional keys are additive, existing fixtures keep working)
-----------------------------------------------------------------------------
``spray_notice``
    ``batch_id`` identifies the batch (defaults to the event id), ``orchard`` /
    ``entity`` the operator, ``zone`` the sprayed zone, ``start``/``end`` the
    application window, ``delivered``/``delivered_at`` delivery, and
    ``dose`` / ``dose_range`` the *effective dose at the hive* after
    calibration and wind-drift conversion (plain numbers, same unit as
    ``threshold``).  ``substance`` groups additive batches.

``health_check``
    ``dead=True`` plus ``death_time`` and optionally ``zone`` / ``hive``.

``incident``
    ``time`` / ``zone`` / ``hive`` and optionally ``death``.

``orchard``
    ``zones`` or ``zone`` for the subject's own land; ``subject``/``name`` may
    label the entity.

Dose provenance
---------------
A dose is only *nameable* when its chain can be replayed: an original chemical
sample (``sample_id`` / ``credential`` / ``lab_sample``), a calibration
reference, and — for a batch that drifted in from another zone — a drift
conversion.  The lethal threshold must declare a source (``threshold_source``
or ``threshold_id``).  A delivered, in-window batch whose chain is broken still
contributes to the observed total (so nobody can pretend it was not there) but
is reported as ``unattributed``: the result becomes ``attribution_incomplete``
and no household is forced to pay for it.
"""

from decimal import Decimal

from .common import rows, latest

# Signed attachment versions.  The mapping is deliberately explicit so the
# stored version string (not the last-arrived document) decides the money.
PROTOCOL_VERSIONS = {
    "joint_necessary": "joint-necessary-v1",
    "joint_several": "joint-several-v1",
}
PROTOCOL_ALIASES = {
    "necessary": "joint_necessary",
    "joint": "joint_necessary",
    "common_necessary": "joint_necessary",
    "several": "joint_several",
    "solidary": "joint_several",
    "joint_and_several": "joint_several",
}
PROTOCOL_TITLES = {
    "joint_necessary": "共同必要原因协议",
    "joint_several": "联合且分别负责协议",
}

class JointError(ValueError):
    pass


# --------------------------------------------------------------------------
# numbers
# --------------------------------------------------------------------------
def _number(value):
    if value is None:
        return None
    return Decimal(str(value))


def _plain(value):
    """Decimal -> JSON-friendly int/float without binary drift."""
    if value is None:
        return None
    if not isinstance(value, Decimal):
        return value
    if value == value.to_integral_value():
        return int(value)
    return float(value.normalize())


def _range(raw):
    """Normalise ``dose_range`` / ``dose`` into ``(lo, hi, point)`` decimals."""
    if raw is None:
        return None, None, None
    if isinstance(raw, dict):
        lo = raw.get("low", raw.get("min", raw.get("lo")))
        hi = raw.get("high", raw.get("max", raw.get("hi")))
        point = raw.get("point", raw.get("value", raw.get("dose")))
        lo = _number(lo)
        hi = _number(hi)
        point = _number(point)
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        if point is not None:
            lo = point if lo is None else lo
            hi = point if hi is None else hi
        if lo is None or hi is None:
            point = lo if lo is not None else hi
            lo = point
            hi = point
        return lo, hi, point if point is not None else (lo + hi) / 2
    if isinstance(raw, (list, tuple)):
        if len(raw) != 2:
            raise JointError("dose_range must hold two values")
        lo, hi = _number(raw[0]), _number(raw[1])
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi, (lo + hi) / 2
    point = _number(raw)
    return point, point, point


def _dose_uncertain(lo, hi, threshold):
    """True when the interval straddles the lethal threshold."""
    if lo is None or hi is None:
        return True
    return lo < threshold <= hi and lo != hi


def _dose_chain(data):
    """Replay ``applied_dose × calibration × drift`` into an effective dose.

    Returns a provenance dict.  ``computed`` is ``None`` unless the chain is
    complete enough to reproduce the number; ``declared`` is whatever the
    spray notice reported.  ``consistent`` is false when both exist but
    disagree, which is treated as a dose uncertainty (money stays frozen).
    """
    applied = _number(data.get("applied_dose", data.get("sample_dose")))
    calibration = _number(
        data.get("calibration_factor",
                 data.get("calibration", {}).get("factor")
                 if isinstance(data.get("calibration"), dict) else None)
    )
    drift = _number(data.get("drift_factor"))
    declared = data.get("dose")
    if declared is None:
        rng = data.get("dose_range")
        if isinstance(rng, dict):
            declared = rng.get("point", rng.get("value"))
        elif isinstance(rng, (list, tuple)) and len(rng) == 2 and rng[0] == rng[1]:
            declared = rng[0]
    declared = _number(declared)
    computed = None
    if applied is not None:
        computed = applied
        if calibration is not None:
            computed *= calibration
        if drift is not None:
            computed *= drift
        # an applied dose with neither conversion factor is still a usable
        # chain only when it is the declared effective dose itself
        if calibration is None and drift is None and declared is not None:
            computed = declared
    consistent = True
    if computed is not None and declared is not None and computed != declared:
        consistent = False
    return {
        "applied_dose": applied,
        "calibration_factor": calibration,
        "drift_factor": drift,
        "computed": computed,
        "declared": declared,
        "consistent": consistent,
    }


# --------------------------------------------------------------------------
# evidence helpers
# --------------------------------------------------------------------------
def _zone(event, default=None):
    return event["data"].get("zone", default)


def _entity(event, default=None):
    data = event["data"]
    return data.get("orchard", data.get("entity", default))


def _current(ev):
    """The event-time view of the case (revisions are separate identities).

    A withdrawal is modelled as a later ``spray_notice`` revision with the same
    ``batch_id`` and ``delivered=False`` (or ``withdrawn=True``).  Latest
    ``(event_time, received_at, id)`` wins for a batch, mirroring ``latest``.
    """
    batches = {}
    for spray in rows(ev, "spray_notice"):
        key = spray["data"].get("batch_id", spray["id"])
        old = batches.get(key)
        rank = (spray["event_time"], spray["received_at"], spray["id"])
        if old is None or rank > old[0]:
            batches[key] = (rank, spray)
    return {key: item[1] for key, item in sorted(batches.items())}


def _sample_chain(data):
    """Return ``(ok, ids, missing)`` for the laboratory/calibration chain.

    A usable dose needs an original chemical credential (``sample_id`` /
    ``credential`` / ``lab_sample``) *and* a calibration reference
    (``calibration_id`` or an explicit ``calibration`` block).  ``drift_id`` is
    collected when present; a cross-zone batch must also carry it (checked by
    the caller).  ``sample_chain=False`` is a hard rejection used by the
    negative fixture.
    """
    if data.get("sample_chain") is False:
        return False, [], ["sample_chain"]
    ids = []
    for key in (
        "sample_id",
        "credential",
        "lab_sample",
        "calibration_id",
        "drift_id",
        "threshold_id",
    ):
        if data.get(key):
            ids.append(str(data[key]))
    missing = []
    if not any(data.get(k) for k in ("sample_id", "credential", "lab_sample")):
        missing.append("sample_id")
    if not (data.get("calibration_id") or data.get("calibration") or data.get("calibrated") is True):
        missing.append("calibration_id")
    return not missing, ids, missing


def _spray_window(spray):
    data = spray["data"]
    return (
        float(data.get("start", spray["event_time"])),
        float(data.get("end", 10**12)),
        float(data.get("delivered_at", data.get("start", spray["event_time"]))),
    )


def _covers_time(spray, at):
    start, end, delivered = _spray_window(spray)
    return bool(spray["data"].get("delivered")) and max(start, delivered) <= at < end


def _dead_event(ev):
    """Return the death facts from the latest explicit health check/incident."""
    candidates = []
    for record in rows(ev, "health_check"):
        data = record["data"]
        if data.get("dead"):
            candidates.append(
                (
                    record["event_time"],
                    record["received_at"],
                    record["id"],
                    _number(data.get("death_time", record["event_time"])),
                    data,
                    record,
                )
            )
    for record in rows(ev, "incident"):
        data = record["data"]
        if data.get("dead") or data.get("death_time") is not None:
            candidates.append(
                (
                    record["event_time"],
                    record["received_at"],
                    record["id"],
                    _number(data.get("death_time", data.get("time", record["event_time"]))),
                    data,
                    record,
                )
            )
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[:3])
    return candidates[-1]


def _threshold(ev):
    """Latest declared threshold and its source."""
    for kind in ("toxicity_profile", "threshold"):
        choices = rows(ev, kind)
        if choices:
            record = max(choices, key=lambda e: (e["event_time"], e["received_at"], e["id"]))
            data = record["data"]
            value = data.get("threshold", data.get("lethal_threshold", data.get("value")))
            return (
                _number(value),
                data.get("source", data.get("threshold_source", record["id"])),
                record["id"],
            )
    # thresholds may travel on the spray notice / incident as a signed annex
    for kind in ("spray_notice", "incident"):
        choices = [
            e
            for e in rows(ev, kind)
            if e["data"].get("threshold") is not None
            or e["data"].get("lethal_threshold") is not None
        ]
        if choices:
            record = max(choices, key=lambda e: (e["event_time"], e["received_at"], e["id"]))
            data = record["data"]
            value = data.get("lethal_threshold", data.get("threshold"))
            return (
                _number(value),
                data.get("threshold_source", record["id"]),
                record["id"],
            )
    return None, None, None


def _sum_rule(ev):
    for record in reversed(sorted(rows(ev, "toxicity_profile"), key=lambda e: (e["event_time"], e["received_at"], e["id"]))):
        data = record["data"]
        if data.get("combine", data.get("sum_rule")):
            return data.get("combine", data.get("sum_rule")), record["id"]
    return None, None


def _entity_name(ev, zone):
    """Owner label for a zone from the orchard records."""
    choices = [e for e in rows(ev, "orchard") if zone in (e["data"].get("zones") or [e["data"].get("zone")])]
    if choices:
        record = max(choices, key=lambda e: (e["event_time"], e["received_at"], e["id"]))
        data = record["data"]
        return data.get("owner", data.get("orchard", data.get("entity", data.get("name", zone))))
    return zone


# --------------------------------------------------------------------------
# agreement / signed protocol
# --------------------------------------------------------------------------
def _agreements(ev):
    """Every signed joint-liability attachment, canonical order."""
    found = []
    for record in rows(ev, "settlement") + rows(ev, "contract"):
        data = record["data"]
        raw = data.get("protocol")
        if not raw:
            continue
        action = data.get("action")
        named = action in ("protocol", "agreement") or data.get("agreement") is True
        if not named:
            continue
        protocol = PROTOCOL_ALIASES.get(raw, raw)
        if protocol not in PROTOCOL_VERSIONS:
            raise JointError("unknown joint responsibility protocol " + str(raw))
        # a protocol record is signed unless explicitly unsigned; an explicit
        # signed_by with fewer than two parties cannot bind both households
        signed_by = data.get("signed_by")
        if data.get("signed") is False:
            signed = False
        elif isinstance(signed_by, (list, tuple)):
            signed = len(set(signed_by)) >= 2
        else:
            signed = True
        found.append(
            (
                (data.get("approved_at", record["event_time"]), record["received_at"], record["id"]),
                protocol,
                signed,
                bool(data.get("effective", data.get("selected", False))),
                record,
            )
        )
    found.sort(key=lambda item: item[0])
    return found


def _pinned_protocol(ev):
    """First signed attachment; an explicitly effective one wins.

    Selection depends only on the signed attachments, never on which spray
    package uploaded last, so the legal shares cannot be rewritten by late
    operational evidence.
    """
    agreements = [item for item in _agreements(ev) if item[2]]
    if not agreements:
        return None, None
    effective = [item for item in agreements if item[3]]
    chosen = (effective or agreements)[0]
    return chosen[1], chosen[4]


# --------------------------------------------------------------------------
# the causal graph
# --------------------------------------------------------------------------
def _loss_amount(ev, incident):
    """The contractual claim total; independent of the causal dose shares.

    ``loss`` is only read from an incident (an explicit claim record); the
    contract's ``loss`` key is a damage *rate* in the older fixtures and must
    not be mistaken for money.
    """
    if incident:
        data = incident["data"]
        for key in ("claim_amount", "amount", "loss"):
            if data.get(key) is not None:
                return _number(data[key]), incident["id"]
    contract = latest(ev, "contract")
    if contract:
        for key in ("claim_amount", "amount"):
            if contract["data"].get(key) is not None:
                return _number(contract["data"][key]), contract["id"]
    return Decimal(0), None


def _reference(ev):
    """The event time and zone the responsibility question is asked at."""
    incident = latest(ev, "incident")
    death = _dead_event(ev)
    at = None
    zone = None
    if incident:
        data = incident["data"]
        if data.get("time") is not None or data.get("death_time") is not None:
            at = _number(data.get("time", data.get("death_time")))
            zone = data.get("zone")
    if at is None and death:
        at = death[3]
        zone = death[4].get("zone")
    if zone is None and death:
        zone = death[4].get("zone")
    return at, zone, incident, death


def _legacy_label(ev):
    """The pre-existing scalar label, retained when no dose graph exists.

    A normal incident record that merely restates the contractual loss (no
    explicit ``time`` and no death) is not an accident, so both entry points
    keep ``无事故`` on the untouched normal fixtures.  An explicit death or an
    incident with a clock time is matched against delivered spray windows; only
    that time-window match yields the old ``果园方`` label, and an unmatched one
    stays ``待调查`` instead of being forced onto a household.
    """
    sprays = rows(ev, "spray_notice")
    death = _dead_event(ev)
    incident = latest(ev, "incident")
    if death:
        at = death[3]
        zone = death[4].get("zone")
    elif incident and incident["data"].get("time") is not None:
        at = _number(incident["data"]["time"])
        zone = incident["data"].get("zone")
    else:
        return "无事故"
    covered = any(
        _covers_time(spray, float(at))
        and (not zone or not _zone(spray) or _zone(spray) == zone)
        for spray in sprays
    )
    return "果园方" if covered else "待调查"


def _exposure(ev):
    """Build batch, combined and counterfactual exposure facts.

    Returns ``(facts, evidence_ids, reason)``.  ``facts`` is ``None`` when the
    evidence cannot support a dose graph (no death, no declared dose, or no
    threshold); the caller then keeps the legacy scalar label.
    """
    at, zone, incident, death = _reference(ev)
    death_time = at
    if death_time is None and death:
        death_time = death[3]
    death_evidence_id = death[5]["id"] if death else None
    death_id = death_evidence_id or (incident["id"] if incident else None)
    if death_time is None:
        return None, [], "no_death_event"

    threshold, threshold_source, threshold_event = _threshold(ev)
    sum_rule, sum_event = _sum_rule(ev)
    day = float(death_time)
    ids = []
    if incident:
        ids.append(incident["id"])
    if death_evidence_id and death_evidence_id not in ids:
        ids.append(death_evidence_id)
    # every explicit death record is part of the proof chain, even when the
    # incident is the record that pins the clock time
    for record in rows(ev, "health_check"):
        if record["data"].get("dead") and record["id"] not in ids:
            ids.append(record["id"])
    if threshold_event:
        ids.append(threshold_event)
    if sum_event:
        ids.append(sum_event)

    batches = _current(ev)
    # Only batches that declare an effective dose participate; this keeps a
    # bare spray notice (the untouched normal fixture) out of every graph.
    declared = [
        spray
        for spray in batches.values()
        if spray["data"].get("dose") is not None
        or spray["data"].get("dose_range") is not None
    ]
    if not declared:
        return None, sorted(set(ids)), "no_dose_evidence"
    if threshold is None:
        return None, sorted(set(ids) + [s["id"] for s in declared]), "threshold_missing"
    if threshold <= 0:
        raise JointError("lethal threshold must be positive")
    if not sum_rule:
        sum_rule = "additive"

    facts = []
    for spray in declared:
        data = spray["data"]
        batch_id = data.get("batch_id", spray["id"])
        entity = _entity(spray, _entity_name(ev, _zone(spray)))
        window_ok = _covers_time(spray, day)
        chain_ok, chain_ids, missing = _sample_chain(data)
        cross_zone = bool(zone and _zone(spray) and _zone(spray) != zone)
        if cross_zone and not (data.get("drift_id") or data.get("drift")):
            chain_ok = False
            missing = missing + ["drift_id"]
        lo, hi, point = _range(data.get("dose_range", data.get("dose")))
        chain = _dose_chain(data)
        usable = (
            bool(data.get("delivered"))
            and window_ok
            and chain_ok
            and point is not None
            and chain["consistent"]
        )
        if not data.get("delivered"):
            reason = "not_delivered"
        elif not window_ok:
            reason = "time_disjoint"
        elif not chain_ok:
            reason = "sample_chain_missing"
        elif point is None:
            reason = "dose_missing"
        elif not chain["consistent"]:
            reason = "dose_chain_mismatch"
        else:
            reason = None
        # A delivered, in-window batch with a measured dose but an incomplete
        # sample/calibration/drift chain still counts as a *declared dose* for
        # the exposure probe.  The attribution probe below decides whether it
        # can be named; this is what stops a broken chain from silently
        # converting the other household into the sole payer.
        contributes_state = bool(data.get("delivered")) and window_ok and point is not None
        facts.append(
            {
                "id": batch_id,
                "batch_id": batch_id,
                "evidence_id": spray["id"],
                "entity": entity,
                "zone": _zone(spray),
                "substance": data.get("substance", "same"),
                "window": list(_spray_window(spray)[:2]),
                "delivered_at": _spray_window(spray)[2],
                "dose": point,
                "dose_lo": lo,
                "dose_hi": hi,
                "usable": usable,
                "contributes": contributes_state,
                "reason": reason,
                "chain_ids": chain_ids,
                "missing": missing,
                "cross_zone": cross_zone,
                "drift_id": data.get("drift_id", data.get("drift")),
                "sample_id": data.get("sample_id", data.get("credential", data.get("lab_sample"))),
                "calibration_id": data.get("calibration_id", data.get("calibration")),
                "dose_chain": chain,
            }
        )
        ids.append(spray["id"])
        ids.extend(str(x) for x in chain_ids)

    # The observed total includes every delivered, in-window measured dose,
    # even one whose provenance is incomplete.  That total is what tells us
    # whether *anyone* can be named; a dose without a replayable chain is
    # never converted into a claim on the other household.
    contributing = [item for item in facts if item.get("contributes")]
    usable = [item for item in facts if item["usable"]]
    total_point = sum((item["dose"] for item in contributing), Decimal(0))
    total_lo = sum((item["dose_lo"] for item in contributing), Decimal(0))
    total_hi = sum((item["dose_hi"] for item in contributing), Decimal(0))
    combined_possible = total_hi >= threshold
    lethal = total_point >= threshold
    uncertain = False
    for item in contributing:
        item["possible"] = _dose_uncertain(item["dose_lo"], item["dose_hi"], threshold)
        if item["possible"]:
            uncertain = True
    if _dose_uncertain(total_lo, total_hi, threshold):
        uncertain = True
    # A simple sum is only valid when efficiency is common across substances;
    # mixing profiles would silently inflate the dose, so flag it and let the
    # uncertain gate keep money frozen rather than summing anyway.
    substances = {item["substance"] for item in contributing}
    if len(substances) > 1:
        sum_rule = "mixed_substances"
        uncertain = True

    # A cause is an *individual* counterfactual test: removing this batch alone
    # must drop the total below the threshold (necessary) or the batch alone
    # must clear it (sufficient).  This holds even when the combined estimate
    # is below the threshold: removing a batch from 7+6=13 is only lethal if
    # 13 itself is lethal, so the graph needs both the total and the removal.
    # A contributing batch with a broken sample/calibration/drift chain is
    # part of the observed total but cannot be named.  When the lethal total
    # depends on it (a merely necessary co-cause), no household can be singled
    # out: the other batch is not silently promoted to sole payer.
    unresolved = any(item.get("contributes") and not item["usable"] for item in facts)
    causes = []
    for item in usable:
        counterfactual = total_point - item["dose"]
        # Inside a multi-batch total, necessity means removing this batch
        # drops below threshold.  For a lone batch the removal counterfactual
        # is 0, which says nothing: a batch that clears the threshold alone is
        # sufficient, not necessary (the other batch was never needed).
        necessary = len(contributing) > 1 and counterfactual < threshold
        sufficient = item["dose"] >= threshold
        item["counterfactual"] = counterfactual
        item["necessary"] = necessary and not uncertain
        item["sufficient"] = sufficient and not uncertain
        blocked_necessary = bool(unresolved and necessary and not sufficient)
        # An edge is a *cause* only when removing it from a lethal total drops
        # below the threshold (necessary) or it alone clears it (sufficient).
        # The combined total must itself be lethal: with B excluded the total
        # is 7, so A is "individually necessary inside 13" but not a cause.
        item["cause"] = bool(
            lethal and not uncertain and (necessary or sufficient) and not blocked_necessary
        )
        if item["possible"]:
            item["cause_state"] = "possible"
        elif uncertain and (necessary or sufficient):
            item["cause_state"] = "possible"
        elif item["cause"]:
            item["cause_state"] = "sufficient" if sufficient else "necessary"
        elif lethal and blocked_necessary:
            item["cause_state"] = "unattributed"
        elif lethal and necessary:
            item["cause_state"] = "not_necessary"
        else:
            item["cause_state"] = "below_threshold"
        if item["cause"] or item["possible"] or item["cause_state"] in ("possible", "unattributed"):
            causes.append(item)

    if not lethal and not causes:
        reason = "below_threshold"
    elif not lethal and causes:
        # individually necessary within the observed total, but the joint
        # estimate itself never crosses the threshold: no payable causal claim
        reason = "combined_below_threshold"
    elif uncertain:
        reason = "dose_range_straddles_threshold"
    elif not any(item["cause"] for item in causes):
        if any(item.get("cause_state") == "unattributed" for item in causes):
            # a lethal total exists but a contributing batch has no replayable
            # chain: name nobody rather than force the other household
            reason = "attribution_incomplete"
        else:
            reason = "no_necessary_cause"
    else:
        reason = None

    basis = sum((item["dose"] for item in causes if item["cause"]), Decimal(0))
    shares = {}
    if basis > 0:
        for item in causes:
            if not item["cause"]:
                continue
            # Damage is allocated by each source's share of the *causal* dose
            # actually attributed to a household, so a health-revision or a
            # withdrawal can never leave shares summing above 100%.
            shares[item["entity"]] = {
                "entity": item["entity"],
                "batch_id": item["batch_id"],
                "dose": item["dose"],
                "numerator": item["dose"],
                "denominator": basis,
                "fraction": f"{_plain(item['dose'])}/{_plain(basis)}",
                "ratio": item["dose"] / basis,
                "necessary": item["necessary"],
                "sufficient": item["sufficient"],
                "basis": "necessary" if item["necessary"] else "sufficient",
            }

    combined = {
        "id": "combined-exposure",
        "type": "combined",
        "to": death_id,
        "dose": total_point,
        "dose_range": [total_lo, total_hi],
        "threshold": threshold,
        "over_by": total_point - threshold,
        "rule": sum_rule,
        "lethal": lethal,
        # "certain" means the whole interval clears the threshold; "possible"
        # means the upper end could, while the point estimate says lethal.
        "certain": total_lo >= threshold,
        "possible": combined_possible,
        # the combined dose is one edge; it is never copied onto the batches
        "counted_once": True,
    }

    return (
        {
            "death_id": death_id,
            "death_evidence_id": death_evidence_id,
            "at": _plain(death_time),
            "zone": zone,
            "threshold": threshold,
            "threshold_source": threshold_source,
            "threshold_evidence_id": threshold_event,
            "sum_rule": sum_rule,
            "batches": facts,
            "combined": combined,
            "causes": causes,
            "shares": shares,
            "uncertain": uncertain,
            "reason": reason,
            "evidence_ids": sorted(set(ids)),
        },
        sorted(set(ids)),
        reason,
    )


def _causal_edges(facts, death_id):
    edges = []
    for item in facts["batches"]:
        edges.append(
            {
                "id": f"{item['batch_id']}->{death_id}",
                "type": "batch",
                "from": item["batch_id"],
                "from_entity": item["entity"],
                "from_zone": item["zone"],
                "to": death_id,
                "dose": _plain(item["dose"]),
                "counterfactual_dose": _plain(item.get("counterfactual")),
                "threshold": _plain(facts["threshold"]),
                "necessary": item.get("necessary", False),
                "sufficient": item.get("sufficient", False),
                "cause": item.get("cause", False),
                "possible": item.get("possible", False),
                "state": item.get("cause_state", "excluded"),
                "time_ok": item["usable"] or item["reason"] != "time_disjoint",
                "chain_ok": item["reason"] not in ("sample_chain_missing", "dose_missing"),
                "reason": item["reason"],
            }
        )
    edges.append(
        {
            key: (
                [_plain(v) for v in value]
                if isinstance(value, list)
                else _plain(value) if isinstance(value, Decimal) else value
            )
            for key, value in dict(facts["combined"], to=death_id).items()
        }
    )
    return edges


def _graph_result(ev, facts):
    """Apply the pinned protocol to the causal facts."""
    protocol, approval = _pinned_protocol(ev)
    loss, loss_id = _loss_amount(ev, latest(ev, "incident"))
    # Money moves only when a cause is established *and* a signed attachment
    # governs it.  A merely possible cause, a below-threshold total, a broken
    # sampling chain or a missing drift conversion never releases funds.
    payment_allowed = bool(
        protocol
        and not facts["uncertain"]
        and any(item["cause"] for item in facts["causes"])
        and facts["combined"]["lethal"]
    )
    reason = facts["reason"]
    if not protocol:
        payment_allowed = False
        if facts["causes"] or facts["combined"]["lethal"]:
            reason = "protocol_unratified"
    elif not payment_allowed and reason is None:
        if facts["uncertain"]:
            reason = "dose_range_straddles_threshold"
        elif not facts["combined"]["lethal"]:
            reason = "combined_below_threshold"
        elif any(item.get("cause_state") == "unattributed" for item in facts["causes"]):
            # the lethal total exists but one contributing batch has no
            # replayable sampling/drift chain, so the household cannot be named
            reason = "attribution_incomplete"
        else:
            reason = "no_necessary_cause"

    shares = facts["shares"]
    frozen_by_entity = {}
    for entity, share in shares.items():
        amount = loss * share["ratio"] if payment_allowed else Decimal(0)
        frozen_by_entity[entity] = {
            **share,
            "amount": _plain(amount),
            "amount_exact": amount,
        }
    frozen_total = sum((row["amount_exact"] for row in frozen_by_entity.values()), Decimal(0))

    claim = None
    freeze_keys = []
    recovery = []
    if protocol:
        if protocol == "joint_necessary":
            object_mode = "per_entity"
            objects = [
                {
                    "entity": entity,
                    "amount": _plain(row["amount"]),
                    "share": row["fraction"],
                    "batch_id": row["batch_id"],
                }
                for entity, row in sorted(frozen_by_entity.items())
            ]
            freeze_keys = [
                {
                    "key": f"joint-necessary:{facts['death_id']}:{entity}",
                    "entity": entity,
                    "amount": _plain(row["amount"]),
                }
                for entity, row in sorted(frozen_by_entity.items())
            ]
            claim_note = "各自按实际贡献剂量分担；总额只开一次"
        else:
            object_mode = "joint"
            objects = [
                {
                    "entity": None,
                    "debtors": sorted(frozen_by_entity),
                    "amount": _plain(frozen_total),
                    "share": "joint",
                }
            ]
            freeze_keys = [
                {
                    "key": f"joint-several:{facts['death_id']}",
                    "entity": None,
                    "debtors": sorted(frozen_by_entity),
                    "amount": _plain(frozen_total),
                }
            ]
            claim_note = "对外一张总额不重复的联带请求；内部仍按剂量份额结算"
        claim = {
            "total": _plain(frozen_total),
            "loss": _plain(loss),
            "loss_evidence_id": loss_id,
            "object": object_mode,
            "debtors": sorted(frozen_by_entity),
            "objects": objects,
            "joint_and_several": protocol == "joint_several",
            "note": claim_note,
            "payment_allowed": payment_allowed,
            "reason": None if payment_allowed else reason,
        }
        for entity, row in sorted(frozen_by_entity.items()):
            recovery.append(
                {
                    "entity": entity,
                    "batch_id": row["batch_id"],
                    "dose": _plain(row["dose"]),
                    "share": row["fraction"],
                    "ratio": float(row["ratio"]),
                    "amount": _plain(row["amount"]),
                    "basis": "necessary" if row["necessary"] else "sufficient",
                }
            )

    funds = _fund_ledger(ev, freeze_keys, frozen_total, payment_allowed)

    return {
        "mode": "joint",
        "legacy_label": "果园方" if facts["causes"] else "待调查",
        "death_event_id": facts["death_id"],
        "death_evidence_id": facts["death_evidence_id"],
        "at": facts["at"],
        "zone": facts["zone"],
        "threshold": _plain(facts["threshold"]),
        "threshold_source": facts["threshold_source"],
        "threshold_evidence_id": facts["threshold_evidence_id"],
        "sum_rule": facts["sum_rule"],
        "batch_ids": [item["batch_id"] for item in facts["batches"]],
        "batches": [
            {
                "batch_id": item["batch_id"],
                "evidence_id": item["evidence_id"],
                "entity": item["entity"],
                "zone": item["zone"],
                "dose": _plain(item["dose"]),
                "dose_range": [_plain(item["dose_lo"]), _plain(item["dose_hi"])],
                "usable": item["usable"],
                "reason": item["reason"],
                "counterfactual_dose": _plain(item.get("counterfactual")),
                "necessary": item.get("necessary", False),
                "sufficient": item.get("sufficient", False),
                "cause": item.get("cause", False),
                "possible": item.get("possible", False),
                "state": item.get("cause_state", "excluded"),
                "sample_id": item["sample_id"],
                "calibration_id": item["calibration_id"],
                "drift_id": item["drift_id"],
            }
            for item in facts["batches"]
        ],
        "causes": [
            {
                "batch_id": item["batch_id"],
                "entity": item["entity"],
                "dose": _plain(item["dose"]),
                "counterfactual_dose": _plain(item["counterfactual"]),
                "necessary": item["necessary"],
                "sufficient": item["sufficient"],
            }
            for item in facts["causes"]
        ],
        "edges": _causal_edges(facts, facts["death_id"]),
        "combined": {
            key: (
                [_plain(v) for v in value]
                if isinstance(value, list)
                else _plain(value) if isinstance(value, Decimal) else value
            )
            for key, value in facts["combined"].items()
        },
        "combined_dose": _plain(facts["combined"]["dose"]),
        "uncertain": facts["uncertain"],
        "reason": reason,
        "payment_allowed": payment_allowed,
        "protocol": protocol,
        "protocol_title": PROTOCOL_TITLES.get(protocol),
        "protocol_version": PROTOCOL_VERSIONS.get(protocol),
        "protocol_approval_id": approval["id"] if approval else None,
        "claim": claim,
        "shares": {
            entity: {
                "dose": _plain(row["dose"]),
                "fraction": row["fraction"],
                "ratio": float(row["ratio"]),
                "amount": _plain(row["amount"]),
            }
            for entity, row in sorted(frozen_by_entity.items())
        },
        "recovery": recovery,
        "freeze_keys": [row["key"] for row in freeze_keys],
        "freeze_details": freeze_keys,
        "funds": funds,
        "provenance": {
            "evidence_ids": facts["evidence_ids"],
            "batch_samples": {
                item["batch_id"]: {
                    "sample_id": item["sample_id"],
                    "calibration_id": item["calibration_id"],
                    "drift_id": item["drift_id"],
                }
                for item in facts["batches"]
            },
            "dose_replay": {
                item["batch_id"]: {
                    "applied_dose": _plain(item["dose_chain"]["applied_dose"]),
                    "calibration_factor": _plain(item["dose_chain"]["calibration_factor"]),
                    "drift_factor": _plain(item["dose_chain"]["drift_factor"]),
                    "computed": _plain(item["dose_chain"]["computed"]),
                    "declared": _plain(item["dose_chain"]["declared"]),
                    "consistent": item["dose_chain"]["consistent"],
                }
                for item in facts["batches"]
            },
            "threshold_source": facts["threshold_source"],
            "threshold_evidence_id": facts["threshold_evidence_id"],
            "protocol_version": PROTOCOL_VERSIONS.get(protocol),
            "protocol_approval_id": approval["id"] if approval else None,
            "fund_event_ids": [row["event_id"] for row in funds["ledger"]],
        },
    }


def _fund_ledger(ev, freeze_keys, claim_total, allowed):
    """Only the pinned protocol's keys can hold money; totals are not doubled."""
    expected = {row["key"]: row for row in freeze_keys}
    ledger = []
    held = {}
    paid = Decimal(0)
    for record in sorted(rows(ev, "settlement"), key=lambda e: (e["received_at"], e["event_time"], e["id"])):
        data = record["data"]
        action = data.get("action", "freeze" if data.get("disputed") else "ignore")
        if action not in ("freeze", "pay"):
            continue
        key = data.get("dispute_id", data.get("freeze_key"))
        if key not in expected:
            if data.get("joint") or data.get("joint_key") or str(key or "").startswith("joint-"):
                ledger.append(
                    {
                        "event_id": record["id"],
                        "key": key,
                        "amount": 0,
                        "state": "rejected",
                        "reason": "foreign_freeze_key",
                    }
                )
            continue
        amount = _number(data.get("amount", 0))
        if not allowed:
            ledger.append(
                {
                    "event_id": record["id"],
                    "key": key,
                    "amount": 0,
                    "state": "blocked",
                    "reason": "uncertain_or_unratified",
                }
            )
            continue
        if action == "freeze":
            cap = _number(expected[key]["amount"])
            if key in held:
                ledger.append(
                    {
                        "event_id": record["id"],
                        "key": key,
                        "amount": 0,
                        "state": "duplicate",
                        "reason": "already_frozen",
                    }
                )
                continue
            take = min(amount, cap)
            held[key] = take
            ledger.append(
                {
                    "event_id": record["id"],
                    "key": key,
                    "amount": _plain(take),
                    "state": "frozen" if take == amount else "partially_frozen",
                    "reason": None if take == amount else "exceeds_share",
                }
            )
        else:
            available = sum(held.values(), Decimal(0)) - paid
            if amount > available:
                ledger.append(
                    {
                        "event_id": record["id"],
                        "key": key,
                        "amount": 0,
                        "state": "rejected",
                        "reason": "exceeds_frozen",
                    }
                )
            else:
                paid += amount
                ledger.append(
                    {
                        "event_id": record["id"],
                        "key": key,
                        "amount": _plain(amount),
                        "state": "paid",
                        "reason": None,
                    }
                )
    return {
        "allowed": allowed,
        "claim_total": _plain(claim_total) if allowed else 0,
        "frozen_total": _plain(sum(held.values(), Decimal(0))),
        "paid_total": _plain(paid),
        "keys": sorted(expected),
        "ledger": ledger,
    }


def snapshot(ev):
    """Return ``(result, evidence_ids)`` for the responsibility surfaces.

    ``result`` is the legacy scalar (``无事故`` / ``待调查`` / ``果园方``) when
    the evidence has no declared dose graph.  When it does, the result is a
    structured graph with batches, counterfactual doses, the single combined
    edge, the pinned protocol, claim objects, recovery detail and the fund
    ledger.  ``dispatch`` and ``settle`` both call this function, so their
    responsibility outputs cannot diverge.
    """
    facts, ids, _reason = _exposure(ev)
    if facts is None:
        label = _legacy_label(ev)
        if label == "无事故":
            return "无事故", ids
        # keep the pre-existing scalar surface for evidence without doses
        return label, ids
    # Evidence that declares effective doses is treated as an investigated
    # accident even when no batch qualifies; the reason explains why no money
    # moves.  This is what keeps "the other household" from being charged by
    # default when the sampling chain is incomplete or the windows miss.
    result = _graph_result(ev, facts)
    proof = set(ids)
    proof.update(result["provenance"]["evidence_ids"])
    proof.update(row["event_id"] for row in result["funds"]["ledger"])
    if result["protocol_approval_id"]:
        proof.add(result["protocol_approval_id"])
    for item in result["batches"]:
        proof.add(item["evidence_id"])
    return result, sorted(proof)
