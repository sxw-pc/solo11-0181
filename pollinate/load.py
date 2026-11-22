import json

def assess(db, ticket_id):
    db.execute('CREATE TABLE IF NOT EXISTS trailer_load (ticket_id TEXT PRIMARY KEY, net_kg REAL NOT NULL, front_kg REAL NOT NULL, rear_kg REAL NOT NULL)')
    raw=db.execute('SELECT raw FROM weigh_ticket WHERE id=?',(ticket_id,)).fetchone()
    if not raw: raise ValueError('unknown ticket')
    ticket=json.loads(raw[0])
    net=float(ticket['gross_kg'])-float(ticket['tare_kg'])
    db.execute('INSERT OR REPLACE INTO trailer_load VALUES (?,?,?,?)',(ticket_id,net,net/2,net/2))
    db.commit()
    return net
