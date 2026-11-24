import argparse, json, sys
from pathlib import Path
from . import __file__ as module_file
from .store import connect, ingest, evidence, save_decisions, report
from . import MODULES
from .breeding_store import init_breeding, ingest_breeding
from .breeding import evaluate_all, replay as breeding_replay
from .queen import import_pair

BREEDING_COMMANDS = ('lineage', 'breeding-report', 'breeding-replay', 'import-pair')


def run_lineage(db_path, path, subject):
    """Ingest raw pedigree/genetic evidence, then rebuild graph and decisions."""
    doc = json.loads(Path(path).read_text(encoding='utf-8'))
    events = doc['events'] if isinstance(doc, dict) and 'events' in doc else doc
    db = connect(db_path)
    init_breeding(db)
    try:
        with db:
            for event in events:
                ingest_breeding(db, event)
    finally:
        db.close()
    return evaluate_all(db_path, subject)

def run_fixture(db, path, allowed=None):
    events=json.loads(__import__('pathlib').Path(path).read_text(encoding='utf-8'))['events']
    if allowed and any(e['kind'] not in allowed for e in events): raise ValueError('fixture contains another subsystem')
    subjects=set()
    with db:
        for event in events:
            ingest(db,event); subjects.add(event['subject'])
        for subject in subjects:
            records=evidence(db,subject)
            results=[]
            for module in MODULES: results.extend(module.decide(records))
            save_decisions(db,subject,results)
    return {s:report(db,s) for s in sorted(subjects)}

def _print(out):
    print(json.dumps(out,ensure_ascii=False,indent=2,default=str))

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['init','demo','report','replay']+[m.COMMAND for m in MODULES]+list(BREEDING_COMMANDS))
    p.add_argument('--db',default='work.db')
    p.add_argument('--fixture')
    p.add_argument('--subject',default='case-1')
    args=p.parse_args(argv)
    db=connect(args.db)
    if args.command=='init': out={'db':args.db,'module_file':module_file}
    elif args.command=='report': out=report(db,args.subject)
    elif args.command=='breeding-report':
        init_breeding(db)
        out={r['pair_id']:dict(version=r['version'],graph_version=r['graph_version'],
                               status=r['status'],result=json.loads(r['result']),
                               evidence_ids=json.loads(r['evidence_ids']))
             for r in db.execute('SELECT * FROM breeding_decision d WHERE version=(SELECT MAX(version) '
                                 'FROM breeding_decision WHERE pair_id=d.pair_id) ORDER BY pair_id')}
    elif args.command=='breeding-replay':
        db.close(); out=breeding_replay(args.db,args.subject); _print(out); return 0
    elif args.command=='import-pair':
        if not args.fixture: raise ValueError('import-pair requires --fixture')
        db.close(); out=import_pair(args.db,args.fixture); _print(out); return 0
    elif args.command=='lineage':
        if not args.fixture: raise ValueError('lineage requires --fixture')
        db.close(); out=run_lineage(args.db,args.fixture,args.subject); _print(out); return 0
    elif args.command=='replay':
        records=evidence(db,args.subject)
        computed={}
        for module in MODULES:
            for name,value,ids in module.decide(records): computed[name]={'result':value,'evidence_ids':sorted(set(ids))}
        stored=report(db,args.subject)
        mismatches={name:{'stored':stored.get(name),'recomputed':item} for name,item in computed.items() if not stored.get(name) or stored[name]['result']!=item['result'] or stored[name]['evidence_ids']!=item['evidence_ids']}
        out={'match':bool(records) and not mismatches,'mismatches':mismatches,'evidence_count':len(records)}
    elif args.command=='demo':
        out={}
        for module in MODULES:
            out.update(run_fixture(db,module.NORMAL))
    else:
        module=next(m for m in MODULES if m.COMMAND==args.command)
        out=run_fixture(db,args.fixture or module.NORMAL,set(module.KINDS))
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__': raise SystemExit(main())
