import json

def import_ticket(db, ticket):
    db.execute('CREATE TABLE IF NOT EXISTS weigh_ticket (id TEXT PRIMARY KEY, raw TEXT NOT NULL)')
    raw=json.dumps(ticket,ensure_ascii=False,sort_keys=True)
    old=db.execute('SELECT raw FROM weigh_ticket WHERE id=?',(ticket['id'],)).fetchone()
    if old and old[0]!=raw: raise ValueError('conflicting ticket')
    if not old: db.execute('INSERT INTO weigh_ticket VALUES (?,?)',(ticket['id'],raw))
    db.commit()
    from .load import assess
    return assess(db,ticket['id'])
