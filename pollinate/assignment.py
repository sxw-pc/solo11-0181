"""Provisional first-available field inspection assignment."""
import json

def assign(db,day_id):
    row=db.execute('SELECT * FROM field_day WHERE id=?',(day_id,)).fetchone()
    day=json.loads(row['raw_json'])
    assigned=[]
    for task in day['tasks']:
        worker=next((w for w in day['workers'] if task['skill'] in w['skills'] and
            not any(a['worker']==w['id'] and a['start']<task['end'] and task['start']<a['end'] for a in assigned)),None)
        assigned.append({'task':task['id'],'worker':worker['id'] if worker else None,
            'start':task['start'],'end':task['end']})
    db.execute('INSERT INTO field_roster VALUES(?,?)',(day_id,json.dumps(assigned)))
    if day.get('publish_dir'):
        from .publish import write_two
        write_two(db,day_id,day['publish_dir'],day.get('fail_after_workers',False))
    return {'day_id':day_id,'assignments':assigned}
