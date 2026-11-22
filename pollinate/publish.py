"""Write two direct-read roster exports from a stored assignment."""
import csv,json
from pathlib import Path

def write_two(db,day_id,output_dir,fail_after_workers=False):
    row=db.execute('SELECT assignments_json FROM field_roster WHERE day_id=?',(day_id,)).fetchone()
    assigned=json.loads(row['assignments_json'])
    root=Path(output_dir);root.mkdir(parents=True,exist_ok=True)
    with (root/'workers.csv').open('w',newline='',encoding='utf8') as file:
        writer=csv.writer(file);writer.writerow(['day_id','worker','task'])
        for a in assigned:writer.writerow([day_id,a['worker'],a['task']])
    if fail_after_workers:
        raise RuntimeError('simulated failure after workers.csv')
    with (root/'orchard_jobs.csv').open('w',newline='',encoding='utf8') as file:
        writer=csv.writer(file);writer.writerow(['day_id','task','worker','start','end'])
        for a in assigned:writer.writerow([day_id,a['task'],a['worker'],a['start'],a['end']])
