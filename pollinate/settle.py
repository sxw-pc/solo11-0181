from pathlib import Path
COMMAND='settle'
KINDS=['contract', 'settlement', 'incident']
FAILURE='fixtures/settle_dispute.json'
NORMAL=str(Path(__file__).resolve().parent.parent / 'fixtures/settle_ok.json')

def allof(events,kind): return [e for e in events if e["kind"]==kind]
def latest(events,kind):
    rows=allof(events,kind)
    return max(rows,key=lambda e:(e["event_time"],e["received_at"],e["id"])) if rows else None

def decide(ev):
    result=[]
    now=max([e["event_time"] for e in ev],default=0)
    def out(name,value,proof):
        sources=[x["id"] for x in proof if x]
        result.append((name,value,sources if sources else [x["id"] for x in ev]))
    h=latest(ev,'health_check')
    status='放行' if h and h['data'].get('healthy') and h['event_time']<=now else '拒绝'
    placements=allof(ev,'placement')
    hours=sum(max(0,int(x['data'].get('end',now))-int(x['data'].get('start',now))) for x in placements if x['data'].get('end',now)>=x['data'].get('start',now))
    replacements=[x for x in placements if x['data'].get('replacement')]
    valid=len({x['data'].get('replaces') for x in replacements})==len(replacements)
    dead=[x for x in allof(ev,'health_check') if x['data'].get('dead')]
    sprays=allof(ev,'spray_notice')
    blooms=allof(ev,'bloom_observation')
    b=latest(ev,'bloom_observation')
    sprays=allof(ev,'spray_notice')
    orchard=latest(ev,'orchard')
    eligible=hours if status=='放行' and b and not any(x['data'].get('delivered') for x in sprays) else 0
    contract=latest(ev,'contract'); minimum=int(contract['data'].get('min_hours',0)) if contract else 0; fee=int(contract['data'].get('fee',0)) if contract else 0; out('合同最低覆盖结算',fee if contract and eligible>=minimum else 0,[contract]+placements+[h,b]+sprays)
    incident=latest(ev,'incident'); out('事故赔付责任裁定','果园方' if incident and sprays and any(x['data'].get('delivered') for x in sprays) else '待调查' if incident else '无事故',[incident]+sprays)
    settlements=allof(ev,'settlement'); frozen=sum(int(x['data'].get('amount',0)) for x in settlements if x['data'].get('disputed')); out('争议款冻结与释放',frozen,settlements)
    return result
