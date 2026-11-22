from .common import rows,latest,proof,placement_hours
from pathlib import Path
COMMAND='dispatch';KINDS=['hive','placement','health_check']
NORMAL=str(Path(__file__).resolve().parent.parent/'fixtures/dispatch_ok.json')
FAILURE='fixtures/dispatch_sick.json'
def decide(ev):
    h=latest(ev,'health_check');placements=rows(ev,'placement');sprays=rows(ev,'spray_notice')
    healthy=bool(h and h['data'].get('healthy') and not h['data'].get('dead'))
    status='放行' if healthy else '拒绝'
    raw=sum(max(0,float(p['data'].get('end',0))-float(p['data'].get('start',0))) for p in placements)
    total,zones,ids=placement_hours(ev)
    replacements=[p for p in placements if p['data'].get('replacement')]
    parent=[p['data'].get('replaces') for p in replacements]
    valid=bool(not replacements or (all(parent) and len(parent)==len(set(parent)) and all(x in {p['id'] for p in placements} for x in parent)))
    dead=[x for x in rows(ev,'health_check') if x['data'].get('dead')]
    responsible=any(s['data'].get('delivered') and d['data'].get('death_time',d['event_time'])>=s['data'].get('start',s['event_time']) and d['data'].get('death_time',d['event_time'])<s['data'].get('end',10**12) for d in dead for s in sprays)
    return [('蜂箱检疫/健康放行',status,proof([h])),('驻场时段认定',raw,proof(placements)),('蜂箱替补额度分配','继承' if valid else '拒绝双计',proof(replacements) or ids),('蜂群损失事故归因','果园方' if responsible else '待调查' if dead else '无事故',proof(dead,sprays) or ids)]
