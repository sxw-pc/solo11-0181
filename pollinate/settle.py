from .common import rows,latest,proof,placement_hours
from pathlib import Path
COMMAND='settle';KINDS=['contract','settlement','incident']
NORMAL=str(Path(__file__).resolve().parent.parent/'fixtures/settle_ok.json')
FAILURE='fixtures/settle_dispute.json'
def decide(ev):
    contract=latest(ev,'contract');hours,zones,ids=placement_hours(ev)
    fee=int(contract['data'].get('fee',0)) if contract and hours>=int(contract['data'].get('min_hours',0)) else 0
    incidents=rows(ev,'incident');sprays=rows(ev,'spray_notice')
    incident=latest(ev,'incident')
    responsibility='无事故'
    if incident:
        t=incident['data'].get('time',incident['event_time']);zone=incident['data'].get('zone','Z1')
        responsibility='果园方' if any(s['data'].get('delivered') and s['data'].get('zone','Z1')==zone and s['data'].get('start',s['event_time'])<=t<s['data'].get('end',10**12) for s in sprays) else '待调查'
    ledger={}
    for row in sorted(rows(ev,'settlement'),key=lambda x:(x['event_time'],x['received_at'],x['id'])):
        d=row['data'];key=d.get('dispute_id',row['id']);state=d.get('action','freeze' if d.get('disputed') else 'ignore')
        if state=='freeze':ledger[key]=int(d.get('amount',0))
        elif state=='release' and key in ledger and fee>=ledger[key]:ledger[key]=0
    return [('合同最低覆盖结算',fee,ids+proof([contract])),('事故赔付责任裁定',responsibility,proof(incidents,sprays) or ids),('争议款冻结与释放',sum(ledger.values()),proof(rows(ev,'settlement')) or ids)]
