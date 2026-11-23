"""Ingest laboratory receipts: per-jar sealed-sample results and revocations.

A lab result decides exactly one jar (ppm = residue_ug / mass_g of the
sealed representative sample). It never rewrites the blend worksheet,
never proves pump-stroke flow edges, and never extrapolates to other
jars of the same truck. A revocation receipt voids an earlier report;
the jar then falls back to whatever evidence remains.
"""
import json
from pathlib import Path


def import_report(db, file):
    raw = Path(file).read_text(encoding='utf8')
    doc = json.loads(raw)
    report_id = doc.get('report_id')
    blend_id = doc.get('blend_id')
    kind = doc.get('kind')
    if not report_id or not blend_id or kind not in ('result', 'revocation'):
        raise ValueError('lab receipt needs report_id, blend_id and kind result|revocation')
    if db.execute('SELECT 1 FROM honey_blend WHERE id=?', (blend_id,)).fetchone() is None:
        raise ValueError('unknown blend ' + repr(blend_id))
    jar = doc.get('jar')
    if kind == 'result':
        sample = doc.get('sample') or {}
        if not jar:
            raise ValueError('result receipt missing jar')
        if not isinstance(sample.get('mass_g'), (int, float)) or sample['mass_g'] <= 0:
            raise ValueError('result receipt needs sample.mass_g > 0')
        if not isinstance(sample.get('residue_ug'), (int, float)) or sample['residue_ug'] < 0:
            raise ValueError('result receipt needs sample.residue_ug >= 0')
    elif not doc.get('revokes'):
        raise ValueError('revocation receipt missing revokes')
    old = db.execute('SELECT raw_json FROM lab_report WHERE id=?', (report_id,)).fetchone()
    if old is not None:
        if json.loads(old['raw_json']) != doc:
            raise ValueError('conflicting lab report id ' + report_id)
        return {'report_id': report_id, 'kind': kind, 'duplicate': True}
    from .jargrade import _decide_and_sync
    with db:
        db.execute('INSERT INTO lab_report VALUES (?,?,?,?,?,?)',
                   (report_id, blend_id, jar, kind, int(doc.get('received_at', 0)), raw))
        computed = _decide_and_sync(db, blend_id)
    summary = {'report_id': report_id, 'kind': kind, 'duplicate': False}
    if kind == 'result':
        summary['jar'] = jar
        summary['decision'] = computed.get(jar)
    return summary
