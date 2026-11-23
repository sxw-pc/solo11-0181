from .common import rows,latest,proof,placement_hours
from . import joint
from pathlib import Path
COMMAND='settle';KINDS=['contract','settlement','incident']
NORMAL=str(Path(__file__).resolve().parent.parent/'fixtures/settle_ok.json')
FAILURE='fixtures/settle_dispute.json'
def decide(ev):
    contract=latest(ev,'contract');hours,zones,ids=placement_hours(ev)
    fee=int(contract['data'].get('fee',0)) if contract and hours>=int(contract['data'].get('min_hours',0)) else 0
    incidents=rows(ev,'incident')
    # Identical sampler as dispatch: the old same-zone time match is retained
    # only when no dose graph exists, so the two entry points cannot disagree.
    responsibility,responsibility_ids=joint.snapshot(ev)
    ledger={}
    for row in sorted(rows(ev,'settlement'),key=lambda x:(x['event_time'],x['received_at'],x['id'])):
        d=row['data'];key=d.get('dispute_id',row['id']);state=d.get('action','freeze' if d.get('disputed') else 'ignore')
        if state=='freeze':ledger[key]=int(d.get('amount',0))
        elif state=='release' and key in ledger and fee>=ledger[key]:ledger[key]=0
    return [('合同最低覆盖结算',fee,ids+proof([contract])),('事故赔付责任裁定',responsibility,responsibility_ids or proof(incidents) or ids),('争议款冻结与释放',sum(ledger.values()),proof(rows(ev,'settlement')) or ids)]
