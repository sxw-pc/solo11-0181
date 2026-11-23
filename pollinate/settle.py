from .common import rows, latest, proof
from . import disputes

COMMAND = 'settle'
KINDS = ['contract', 'settlement', 'incident']
NORMAL = 'fixtures/settle_ok.json'
FAILURE = 'fixtures/settle_dispute.json'


def decide(ev):
    ledger = disputes.snapshot(ev)
    incidents = rows(ev, 'incident')
    sprays = rows(ev, 'spray_notice')
    incident = latest(ev, 'incident')

    responsibility = '无事故'
    if incident:
        t = incident['data'].get('time', incident['event_time'])
        zone = incident['data'].get('zone', 'Z1')
        covered = any(
            s['data'].get('delivered')
            and s['data'].get('zone', 'Z1') == zone
            and s['data'].get('start', s['event_time']) <= t < s['data'].get('end', 10 ** 12)
            for s in sprays
        )
        responsibility = '果园方' if covered else '待调查'

    return [
        ('合同最低覆盖结算', disputes.yuan(ledger['fee']), ledger['evidence_ids']),
        ('事故赔付责任裁定', responsibility, proof(incidents, sprays) or ledger['evidence_ids']),
        # the disputed-amount decision is the versioned ledger itself: the
        # per-dispute table, not a scalar that could hide an over-reservation
        ('争议款冻结与释放', ledger, ledger['evidence_ids']),
    ]
