"""Provisional release from the blend-wide mean residue level."""
import json

def assess(db,blend_id):
    row=db.execute('SELECT * FROM honey_blend WHERE id=?',(blend_id,)).fetchone()
    doc=json.loads(row['raw_json'])
    comps=doc['components']
    ppm=round(sum(c['kg']*c['ppm'] for c in comps)/sum(c['kg'] for c in comps),4)
    status='release' if ppm<=3 else 'hold'
    db.execute('INSERT INTO honey_grade VALUES(?,?,?)',(blend_id,ppm,status))
    return {'blend_id':blend_id,'mean_ppm':ppm,'status':status}
