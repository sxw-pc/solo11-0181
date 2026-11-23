from pathlib import Path
from collections import defaultdict

def rows(ev,kind): return [e for e in ev if e['kind']==kind]
def latest(ev,kind):
    choices=rows(ev,kind)
    return max(choices,key=lambda e:(e['event_time'],e['received_at'],e['id'])) if choices else None
def proof(*groups):
    return [e['id'] for group in groups for e in group if e]
def overlap(a,b,c,d): return max(0,min(b,d)-max(a,c))

def placement_hours(ev):
    health=latest(ev,'health_check')
    bloom=latest(ev,'bloom_observation')
    placements=rows(ev,'placement')
    orchard=latest(ev,'orchard')
    sprays=rows(ev,'spray_notice')
    if not health or not health['data'].get('healthy') or health['data'].get('dead') or not bloom or not orchard:
        return 0,{},proof([health,bloom,orchard],placements,sprays)
    window=bloom['data'].get('window') or [0,10**12]
    allowed_zones=set(orchard['data'].get('zones') or [orchard['data'].get('zone','Z1')])
    by_zone=defaultdict(list)
    for p in placements:
        d=p['data'];zone=d.get('zone','Z1')
        if zone not in allowed_zones: continue
        start=max(float(d.get('start',0)),float(window[0]),float(health['data'].get('valid_from',-10**12)))
        end=min(float(d.get('end',0)),float(window[1]),float(health['data'].get('valid_to',10**12)))
        if start>=end:continue
        cuts=[(start,end)]
        for notice in sprays:
            sd=notice['data']
            if not sd.get('delivered') or sd.get('zone','Z1')!=zone: continue
            a=max(float(sd.get('start',-10**12)),float(sd.get('delivered_at',sd.get('start',-10**12))))
            b=float(sd.get('end',10**12))
            next_cuts=[]
            for x,y in cuts:
                if overlap(x,y,a,b)==0:next_cuts.append((x,y))
                else:
                    if x<a:next_cuts.append((x,min(y,a)))
                    if b<y:next_cuts.append((max(x,b),y))
            cuts=next_cuts
        allocation=d.get('replaces') if d.get('replacement') else d.get('allocation',p['id'])
        by_zone[(zone,allocation)].extend(cuts)
    # A replaced hive inherits one allocation. Union overlapping intervals to avoid double counting.
    result={}
    for (zone,allocation),intervals in by_zone.items():
        merged=[]
        for a,b in sorted(intervals):
            if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
            else:merged.append((a,b))
        result[zone]=result.get(zone,0)+sum(b-a for a,b in merged)
    return sum(result.values()),result,proof([health,bloom,orchard],placements,sprays)
