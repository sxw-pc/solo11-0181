from pathlib import Path
COMMAND='bloom'
KINDS=['orchard', 'bloom_observation', 'spray_notice']
FAILURE='fixtures/bloom_spray_late.json'
NORMAL=str(Path(__file__).resolve().parent.parent / 'fixtures/bloom_ok.json')

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
    blooms=allof(ev,'bloom_observation'); b=latest(ev,'bloom_observation'); out('花期版本选定',b['data'].get('window','待观察') if b else '待观察',blooms)
    sprays=allof(ev,'spray_notice'); out('喷药禁入发布','禁入' if any(x['data'].get('delivered') for x in sprays) else '未生效',sprays)
    orchard=latest(ev,'orchard'); eligible=hours if status=='放行' and b and not any(x['data'].get('delivered') for x in sprays) else 0; out('果园分区覆盖认定',eligible,placements+[h,b,orchard]+sprays)
    contract=latest(ev,'contract')
    minimum=int(contract['data'].get('min_hours',0)) if contract else 0
    fee=int(contract['data'].get('fee',0)) if contract else 0
    incident=latest(ev,'incident')
    settlements=allof(ev,'settlement')
    return result
