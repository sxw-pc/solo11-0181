from .common import rows,latest,proof,placement_hours
from . import joint
from pathlib import Path
COMMAND='dispatch';KINDS=['hive','placement','health_check']
NORMAL=str(Path(__file__).resolve().parent.parent/'fixtures/dispatch_ok.json')
FAILURE='fixtures/dispatch_sick.json'
def decide(ev):
    h=latest(ev,'health_check');placements=rows(ev,'placement')
    healthy=bool(h and h['data'].get('healthy') and not h['data'].get('dead'))
    status='放行' if healthy else '拒绝'
    raw=sum(max(0,float(p['data'].get('end',0))-float(p['data'].get('start',0))) for p in placements)
    total,zones,ids=placement_hours(ev)
    replacements=[p for p in placements if p['data'].get('replacement')]
    parent=[p['data'].get('replaces') for p in replacements]
    valid=bool(not replacements or (all(parent) and len(parent)==len(set(parent)) and all(x in {p['id'] for p in placements} for x in parent)))
    # Same sampler as settle: batch dose, counterfactuals, joint damage and the
    # signed recovery shares.  With no death this stays "无事故" exactly as before.
    responsibility,responsibility_ids=joint.snapshot(ev)
    return [('蜂箱检疫/健康放行',status,proof([h])),('驻场时段认定',raw,proof(placements)),('蜂箱替补额度分配','继承' if valid else '拒绝双计',proof(replacements) or ids),('蜂群损失事故归因',responsibility,responsibility_ids or ids)]
