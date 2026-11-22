from pathlib import Path
COMMAND='dispatch'
KINDS=['hive', 'placement', 'health_check']
FAILURE='fixtures/dispatch_sick.json'
NORMAL=str(Path(__file__).resolve().parent.parent / 'fixtures/dispatch_ok.json')

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
    h=latest(ev,'health_check'); status='放行' if h and h['data'].get('healthy') and h['event_time']<=now else '拒绝'; out('蜂箱检疫/健康放行',status,[h])
    placements=allof(ev,'placement'); hours=sum(max(0,int(x['data'].get('end',now))-int(x['data'].get('start',now))) for x in placements if x['data'].get('end',now)>=x['data'].get('start',now)); out('驻场时段认定',hours,placements)
    replacements=[x for x in placements if x['data'].get('replacement')]; valid=len({x['data'].get('replaces') for x in replacements})==len(replacements); out('蜂箱替补额度分配','继承' if valid else '拒绝双计',replacements)
    dead=[x for x in allof(ev,'health_check') if x['data'].get('dead')]; sprays=allof(ev,'spray_notice'); out('蜂群损失事故归因','果园方' if dead and sprays and min(x['event_time'] for x in sprays)<=dead[-1]['event_time'] else '待调查' if dead else '无事故',dead+sprays)
    blooms=allof(ev,'bloom_observation')
    b=latest(ev,'bloom_observation')
    sprays=allof(ev,'spray_notice')
    orchard=latest(ev,'orchard')
    eligible=hours if status=='放行' and b and not any(x['data'].get('delivered') for x in sprays) else 0
    contract=latest(ev,'contract')
    minimum=int(contract['data'].get('min_hours',0)) if contract else 0
    fee=int(contract['data'].get('fee',0)) if contract else 0
    incident=latest(ev,'incident')
    settlements=allof(ev,'settlement')
    return result
