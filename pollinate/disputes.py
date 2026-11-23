"""Versioned dispute-to-reservation allocator.

Every snapshot here is a pure function of the evidence list, so ``replay`` can
recompute it independently and the store can persist a new ledger version
whenever the evidence changes.

Money is handled internally in cents (integers) so proportional allocation is
exact and replayable.  Public results expose integer cents plus a yuan label.

Ledger fields per dispute
-------------------------
``claim``        the amount approved/signed by the dispute event.
``entitlement``  the dispute's share if the contract's *face* fee were funded.
``funded``       the dispute's share of the *current* fee (after spray/health
                 revisions).  This is the signed split re-applied to today's fee.
``secured``      the amount actually held for the dispute: ``funded`` unless the
                 dispute was released or withdrawn, in which case ``0``.
``revoked``      the live share that lost its funding because coverage fell.  It
                 is *not* an asset; it is the amount to unwind (撤销/追偿).  A
                 released or withdrawn dispute has no live share, so its
                 ``revoked`` is 0 even if the fee later collapses.
``status``       ``reserved`` / ``released`` / ``withdrawn`` / ``revoked`` /
                 ``partially_funded`` / ``unfunded``.

Global invariants (asserted on every snapshot):

* ``reserved_total + payable == fee``
* ``reserved_total + paid_total + available_payable - overpaid_total == fee``;
  an accepted payment above the current payable is reported as
  ``overpaid_total`` (追偿) instead of being hidden or erased.
* ``0 <= secured[dispute] <= funded[dispute] <= claim[dispute]``
* ``revoked_total + reserved_total == live entitlement``
* releasing / withdrawing a dispute frees exactly that dispute's own share and
  never redistributes it to the other claimants.
* the agreed protocol version (first signed, covering approval) is never
  reinterpreted by an event that arrives or is accepted later.

Protocols
---------
``priority``  ("先受理优先"): ordered by ``(approved_at, approval_id)``; each
              dispute receives ``min(claim, remaining)`` in that order.
``equal``     ("平等分摊"): largest-remainder split of the available amount in
              proportion to each dispute's approved claim, in cents; ties are
              broken by protocol order so the leftover-cent rule is fixed and
              replayable.

A release only clears a share the dispute actually holds at the moment it is
accepted.  A release that arrives while the dispute holds nothing (for example
after a spray already zeroed the fee) is recorded as blocked and leaves the
freeze in place; a later release, after coverage returns, is the one that
clears it.  Repeating an effective release is a no-op.
"""

from decimal import Decimal

from .common import rows, latest, placement_hours

PROTOCOL_VERSIONS = {
    "priority": "precedence-v1",
    "equal": "equal-v1",
}

_ACTIONS = {
    "freeze": "freeze",
    "freeze_all": "freeze",
    "release": "release",
    "withdraw": "withdraw",
    "pay": "pay",
    "payment": "pay",
    "protocol": "protocol",
    "ignore": "ignore",
}


class LedgerError(ValueError):
    pass


def _cents(value):
    """Parse a money amount (yuan) into integer cents without float drift."""
    return int(Decimal(str(value)) * 100)


def cents_label(cents):
    return f"{cents / 100:.2f}"


def yuan(cents):
    """Backwards-compatible integer-yuan view; exact cents live in the ledger."""
    return cents // 100 if cents % 100 == 0 else round(cents / 100, 2)


def _claim(event):
    data = event["data"]
    return _cents(data.get("claim", data.get("amount", 0)))


def _action(event):
    data = event["data"]
    raw = data.get("action", "freeze" if data.get("disputed") else "ignore")
    try:
        return _ACTIONS[raw]
    except KeyError:
        raise LedgerError("unknown settlement action " + str(raw)) from None


def _approval_scope(event):
    """Return ``(is_approval, scope)`` for one settlement event.

    ``scope`` is ``None`` for a global (contract-level) approval, else the set
    of dispute ids the signed record names.  Both an explicit
    ``action='protocol'`` record and a ``freeze``/``freeze_all`` record carrying
    ``protocol`` are accepted; the latter exists because the approval and the
    freeze arrive as one signed record in this codebase.  A ``protocol`` with no
    ``disputes`` list is a contract-level rule for whatever disputes exist.
    """
    data = event["data"]
    protocol = data.get("protocol")
    if not protocol:
        return False, None
    action = _action(event)
    if action == "protocol":
        scope = data.get("disputes")
        return True, (None if not scope else set(scope))
    if action == "freeze":
        scope = {d.get("dispute_id", "D0") for d in data.get("disputes", [])}
        if data.get("disputes"):
            return True, scope
        # a bare protocol on a freeze is the contract-level rule, not a rule
        # scoped to only this dispute
        return True, None
    return False, None


def _protocol_for(events, claimed_ids):
    """Pin ``(version, protocol, approval_id)`` for the whole ledger.

    ``events`` is in canonical acceptance order and ``claimed_ids`` is the full
    set of disputes this ledger will ever contain.  The first signed approval
    that governs every one of them is pinned for the life of the ledger.  A
    contract-level approval (no ``disputes`` list) governs all disputes,
    present and future; a partial approval that names only some claimants can
    never leak its rule onto the others, and a later approval can never
    reinterpret an already-pinned version.
    """
    winners = set(claimed_ids)
    for event in events:
        is_approval, scope = _approval_scope(event)
        if not is_approval:
            continue
        if scope is not None and not winners <= scope:
            continue
        protocol = event["data"]["protocol"]
        if protocol not in PROTOCOL_VERSIONS:
            raise LedgerError("unknown allocation protocol " + str(protocol))
        return PROTOCOL_VERSIONS[protocol], protocol, event["id"]
    return PROTOCOL_VERSIONS["priority"], "priority", None


def allocate(fee_cents, claims, protocol, order):
    """Split ``fee_cents`` across ``claims`` (dict id -> cents).

    ``order`` maps a dispute id to its ``(approved_at, approval_id)`` sort key.
    Returns ``(shares, payable)``; conservation is asserted, not assumed.
    """
    fee_cents = max(0, int(fee_cents))
    active = {k: max(0, int(v)) for k, v in claims.items() if v > 0}
    if not active:
        return {}, fee_cents

    shares = {k: 0 for k in active}
    if protocol == "priority":
        remaining = fee_cents
        for key in sorted(active, key=lambda k: order[k]):
            take = min(active[key], remaining)
            shares[key] = take
            remaining -= take

    elif protocol == "equal":
        total_claim = sum(active.values())
        available = min(fee_cents, total_claim)
        by_order = {k: i for i, k in enumerate(sorted(active, key=lambda k: order[k]))}
        remainder = {}
        for key in active:
            product = available * active[key]
            shares[key] = product // total_claim
            remainder[key] = product % total_claim
        leftover = available - sum(shares.values())
        # fixed, replayable tie-break: largest remainder, then protocol order
        for key in sorted(active, key=lambda k: (-remainder[k], by_order[k]))[:leftover]:
            shares[key] += 1
    else:
        raise LedgerError("unknown allocation protocol " + str(protocol))

    payable = fee_cents - sum(shares.values())
    if payable < 0 or sum(shares.values()) + payable != fee_cents:
        raise AssertionError("money conservation broken")
    return shares, payable


def current_fee(ev):
    contract = latest(ev, "contract")
    hours, _zones, _ids = placement_hours(ev)
    if contract and hours >= int(contract["data"].get("min_hours", 0)):
        return _cents(contract["data"].get("fee", 0))
    return 0


def face_fee(ev):
    contract = latest(ev, "contract")
    return _cents(contract["data"].get("fee", 0)) if contract else 0


def ledger_events(ev):
    return sorted(
        rows(ev, "settlement"),
        key=lambda e: (e["received_at"], e["event_time"], e["id"]),
    )


def _funded_share(ev, dispute, protocol, order):
    """Whether ``dispute`` would hold money at ``ev`` under the pinned protocol.

    The release gate passes the *pinned* protocol (fixed from the full ledger)
    rather than re-deriving one from a prefix, so a release is judged against
    the same signed rule the rest of the ledger uses.
    """
    fee = current_fee(ev)
    claimed = {}
    for event in ledger_events(ev):
        if _action(event) != "freeze":
            continue
        claimed[event["data"].get("dispute_id", "D0")] = _claim(event)
    funded, _ = allocate(fee, claimed, protocol, order)
    return funded.get(dispute, 0)


def _state(ev):
    """Index one evidence snapshot into claims, releases, protocol and shares.

    A release only takes effect when, at the moment it is accepted, the
    releasing dispute actually holds a share of the then-current fee.  A release
    that arrives while the reservation is unfunded (for example after an earlier
    spray already zeroed the fee) is recorded as ``blocked`` and leaves the
    freeze in place; a later release, after coverage is restored, is then the
    one that clears it.  Repeating an effective release is a no-op.
    """
    fee = current_fee(ev)
    events = ledger_events(ev)

    first_claim = {}
    last_claim = {}
    claimed = {}
    status = {}
    stamped_at = {}
    blocked_release = {}
    payment_events = []
    # the protocol is pinned from the full ledger before any release is judged
    all_order = {}
    for event in events:
        if _action(event) == "freeze":
            all_order.setdefault(
                event["data"].get("dispute_id", "D0"),
                (event["data"].get("approved_at", event["event_time"]), event["id"]),
            )
    all_ids = {e["data"].get("dispute_id", "D0") for e in events if _action(e) == "freeze"}
    _version, pinned_protocol, _approval = _protocol_for(events, all_ids)

    for event in events:
        dispute = event["data"].get("dispute_id", "D0")
        action = _action(event)
        if action == "freeze":
            # a freeze (re)opens the dispute even after an earlier withdrawal;
            # the signed protocol itself still cannot change
            first_claim.setdefault(dispute, event)
            last_claim[dispute] = event
            claimed[dispute] = _claim(event)
            status[dispute] = "open"
            stamped_at.pop(dispute, None)
        elif action == "withdraw":
            if dispute not in first_claim:
                continue
            status[dispute] = "withdrawn"
            stamped_at[dispute] = event["event_time"]
        elif action == "release":
            if dispute not in first_claim or status.get(dispute) != "open":
                continue
            prefix = [e for e in ev if e["received_at"] <= event["received_at"]]
            if _funded_share(prefix, dispute, pinned_protocol, all_order) > 0:
                status[dispute] = "released"
                stamped_at[dispute] = event["event_time"]
            else:
                # arrived while the dispute held nothing: there is no occupied
                # share to release, so the freeze stays in place.  The attempt is
                # recorded for audit; coverage that returns later still needs a
                # fresh release to clear it.
                blocked_release[dispute] = event["event_time"]
        elif action == "pay":
            amount = _cents(event["data"].get("amount", 0))
            if amount <= 0:
                raise LedgerError("payment amount must be positive")
            payment_events.append((event, amount))

    released_at = {
        d: stamped_at[d] for d in status if status[d] == "released" and d in stamped_at
    }
    withdrawn_at = {
        d: stamped_at[d] for d in status if status[d] == "withdrawn" and d in stamped_at
    }

    # a later, different claim for the same dispute must be a signed revision
    for dispute, event in last_claim.items():
        if _claim(event) != _claim(first_claim[dispute]) and not event["data"].get(
            "claim_revision"
        ):
            raise LedgerError("conflicting claim for " + str(dispute))

    order = {
        dispute: (event["data"].get("approved_at", event["event_time"]), event["id"])
        for dispute, event in first_claim.items()
    }
    version, protocol, approval_id = _protocol_for(events, set(claimed))
    funded, _ = allocate(fee, claimed, protocol, order)
    released = set(released_at) | set(withdrawn_at)
    secured = {d: (0 if d in released else funded.get(d, 0)) for d in claimed}
    payable = fee - sum(secured.values())
    return {
        "fee": fee,
        "events": events,
        "first_claim": first_claim,
        "last_claim": last_claim,
        "claimed": claimed,
        "released_at": released_at,
        "blocked_release": blocked_release,
        "withdrawn_at": withdrawn_at,
        "order": order,
        "protocol": protocol,
        "protocol_version": version,
        "approval_id": approval_id,
        "funded": funded,
        "payable": payable,
        "secured": secured,
        "payment_events": payment_events,
    }


def _accepted_payments(ev):
    """Accept each payment against the payable known when it arrived.

    Replaying against the then-current evidence means a signature-verified
    spray that shows up late cannot silently rewrite a payment that was settled
    on the then-complete record; the excess is reported as overpayment instead.
    """
    out = []
    for event, amount in _state(ev)["payment_events"]:
        moment = event["received_at"]
        history = [e for e in ev if e["received_at"] <= moment]
        payable_then = _state(history)["payable"]
        already = sum(p["amount"] for p in out if p["accepted"])
        accepted = already + amount <= payable_then
        out.append(
            {
                "event_id": event["id"],
                "dispute_id": event["data"].get("dispute_id"),
                "received_at": moment,
                "event_time": event["event_time"],
                "amount": amount,
                "amount_label": cents_label(amount),
                "accepted": accepted,
                "status": "accepted" if accepted else "rejected",
                "payable_then": payable_then,
                "payable_then_label": cents_label(payable_then),
            }
        )
    return out


def _evidence_ids(ev):
    contract = latest(ev, "contract")
    _hours, _zones, coverage_ids = placement_hours(ev)
    ids = set(coverage_ids or [])
    if contract:
        ids.add(contract["id"])
    ids.update(e["id"] for e in rows(ev, "settlement"))
    ids.update(e["id"] for e in rows(ev, "contract"))
    return sorted(ids)


def snapshot(ev):
    """Compute the full versioned dispute ledger for one subject."""
    state = _state(ev)
    fee = state["fee"]
    face = face_fee(ev)
    claimed = state["claimed"]
    funded = state["funded"]
    secured = state["secured"]
    reserved = sum(secured.values())

    entitlement, _ = allocate(face, claimed, state["protocol"], state["order"])

    payments = _accepted_payments(ev)
    paid_total = sum(p["amount"] for p in payments if p["accepted"])
    payable = state["payable"]
    available = max(0, payable - paid_total)
    overpaid = max(0, paid_total - payable)

    meta = {}
    revoked_total = 0
    target_total = 0
    for dispute in claimed:
        dismissed = (
            dispute in state["released_at"] or dispute in state["withdrawn_at"]
        )
        # a dismissed dispute has no live reservation left to revoke
        target = 0 if dismissed else entitlement.get(dispute, 0)
        revoked = max(0, target - secured[dispute]) if not dismissed else 0
        revoked_total += revoked
        target_total += target
        if dispute in state["withdrawn_at"]:
            status = "withdrawn"
        elif dismissed:
            status = "released"
        elif secured[dispute] > 0 and revoked > 0:
            status = "partially_funded"
        elif secured[dispute] > 0:
            status = "reserved"
        elif revoked > 0:
            status = "revoked"
        else:
            status = "unfunded"
        meta[dispute] = {
            "approved_at": state["order"][dispute][0],
            "approval_id": state["first_claim"][dispute]["id"],
            "last_event_id": state["last_claim"][dispute]["id"],
            "released": dispute in state["released_at"],
            "withdrawn": dispute in state["withdrawn_at"],
            "released_at": state["released_at"].get(
                dispute, state["withdrawn_at"].get(dispute)
            ),
            "status": status,
            "revoked": revoked,
        }

    result = _report(
        fee=fee,
        face=face,
        payable=payable,
        reserved=reserved,
        available=available,
        paid_total=paid_total,
        overpaid=overpaid,
        revoked_total=revoked_total,
        target_total=target_total,
        funded=funded,
        entitlement=entitlement,
        secured=secured,
        claimed=claimed,
        dispute_order=sorted(claimed, key=lambda d: state["order"][d]),
        protocol=state["protocol"],
        version=state["protocol_version"],
        approval_id=state["approval_id"],
        meta=meta,
        payments=payments,
        evidence_ids=_evidence_ids(ev),
    )
    result["recoverable_total"] = revoked_total + overpaid
    result["recoverable_total_label"] = cents_label(revoked_total + overpaid)
    return result


def _report(
    fee,
    face,
    payable,
    reserved,
    available,
    paid_total,
    overpaid,
    revoked_total,
    target_total,
    funded,
    entitlement,
    secured,
    claimed,
    dispute_order,
    protocol,
    version,
    approval_id,
    meta,
    payments,
    evidence_ids,
):
    rows_out = {}
    for dispute in dispute_order:
        rows_out[dispute] = {
            "claim": claimed[dispute],
            "claim_label": cents_label(claimed[dispute]),
            "entitlement": entitlement.get(dispute, 0),
            "entitlement_label": cents_label(entitlement.get(dispute, 0)),
            "funded": funded.get(dispute, 0),
            "funded_label": cents_label(funded.get(dispute, 0)),
            # ``share`` is the signed allocation of the current fee (the amount
            # held before any release/withdraw); ``secured`` is what is still
            # actually reserved after those instructions.
            "share": funded.get(dispute, 0),
            "share_label": cents_label(funded.get(dispute, 0)),
            "secured": secured.get(dispute, 0),
            "secured_label": cents_label(secured.get(dispute, 0)),
            "revoked": meta.get(dispute, {}).get("revoked", 0),
            "revoked_label": cents_label(meta.get(dispute, {}).get("revoked", 0)),
            **{k: v for k, v in meta.get(dispute, {}).items() if k != "revoked"},
        }

    if reserved + payable != fee:
        raise AssertionError("reserved + payable must equal fee")
    # available - overpaid == payable - paid  =>  the clean cash identity
    if reserved + paid_total + available - overpaid != fee:
        raise AssertionError("reserved + paid + available - overpaid must equal fee")
    if revoked_total + reserved != target_total:
        raise AssertionError("revoked + reserved must equal live entitlement")

    return {
        "fee": fee,
        "fee_label": cents_label(fee),
        "face_fee": face,
        "face_fee_label": cents_label(face),
        "payable": payable,
        "payable_label": cents_label(payable),
        "available_payable": available,
        "available_payable_label": cents_label(available),
        "reserved_total": reserved,
        "reserved_total_label": cents_label(reserved),
        "paid_total": paid_total,
        "paid_total_label": cents_label(paid_total),
        "overpaid_total": overpaid,
        "overpaid_total_label": cents_label(overpaid),
        "revoked_total": revoked_total,
        "revoked_total_label": cents_label(revoked_total),
        "dispute_order": dispute_order,
        "shares": rows_out,
        "protocol": protocol,
        "protocol_version": version,
        "approval_id": approval_id,
        "payments": payments,
        "evidence_ids": evidence_ids,
    }
