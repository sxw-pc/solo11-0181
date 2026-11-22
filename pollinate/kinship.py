"""Provisional mating decision from shared direct parents."""
import json

def assess(db,pair_id):
    row=db.execute('SELECT * FROM queen_pair WHERE id=?',(pair_id,)).fetchone()
    doc=json.loads(row['raw_json'])
    shared=bool(set(doc['queen_parents']) & set(doc['drone_parents']))
    status='hold' if shared else 'allow'
    db.execute('INSERT INTO queen_pair_decision VALUES(?,?,?)',(pair_id,int(shared),status))
    return {'pair_id':pair_id,'shared_parent':shared,'status':status}
