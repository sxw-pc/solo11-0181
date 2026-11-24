"""Microphone captures for orchard hives.

Two import paths share the same store:

* :func:`import_mic` / :func:`import_audio` -- the original summary path.  It
  records ``sample_hz`` and the RMS of a clip and leaves
  :func:`pollinate.activity.assess` to write the provisional ``active`` row.
* :func:`import_capture` / :func:`import_pump_events` / :func:`import_infrared`
  -- raw captures carrying sample sequence numbers, anti-alias state, array
  geometry, box-entrance occlusion and a single power-on clock epoch.

Every raw byte is hashed before it enters the database.  Material that cannot
be trusted (broken frames, an open clock, a clip that disagrees with its
epoch's clock, an impossible sequence) is written to ``rejection`` and *never*
stored as a capture.  That way a missing frame is never zero-filled into
"the hive was quiet".
"""
import hashlib, json, math
from pathlib import Path
from .store import connect

def _sha(text): return hashlib.sha256(text.encode('utf8')).hexdigest()

def _read(file):
    raw=Path(file).read_text(encoding='utf8')
    return raw,json.loads(raw),_sha(raw)

def write_raw(path,doc):
    """Write canonical JSON and return (raw text, sha256)."""
    raw=json.dumps(doc,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(raw,encoding='utf8')
    return raw,_sha(raw)


# ---------------------------------------------------------------------------
# Original summary path

def import_mic(db_path,file):
    raw,_doc,_digest=_read(file);doc=json.loads(raw)
    db=connect(db_path)
    try:
        with db:
            if not doc['mic_id'] or doc['sample_hz']<=0:raise ValueError('invalid_mic')
            existing=db.execute('SELECT raw FROM hive_mic WHERE id=?',(doc['mic_id'],)).fetchone()
            if existing is None:
                db.execute('INSERT INTO hive_mic VALUES(?,?,?,?)',(doc['mic_id'],doc['hive_id'],doc['sample_hz'],raw))
            elif existing['raw']!=raw: raise ValueError('conflicting_mic '+doc['mic_id'])
        return {'mic_id':doc['mic_id']}
    finally:db.close()

def import_audio(db_path,file):
    raw,doc,_digest=_read(file)
    db=connect(db_path)
    try:
        with db:
            if doc['rms']<0:raise ValueError('invalid_rms')
            existing=db.execute('SELECT raw FROM hive_audio WHERE id=?',(doc['audio_id'],)).fetchone()
            if existing is None:
                db.execute('INSERT INTO hive_audio VALUES(?,?,?,?,?)',(doc['audio_id'],doc['mic_id'],doc['at'],doc['rms'],raw))
            elif existing['raw']!=raw: raise ValueError('conflicting_audio '+doc['audio_id'])
            from .activity import assess
            return assess(db,doc['mic_id'])
    finally:db.close()


# ---------------------------------------------------------------------------
# Sampled capture path

def rms(samples):
    return math.sqrt(sum(float(x)*float(x) for x in samples)/len(samples)) if samples else 0.0

def encode_pcm16(samples):
    """Quantise [-1,1] to signed 16-bit little-endian bytes."""
    out=bytearray()
    for x in samples:
        v=int(round(max(-1.0,min(1.0,float(x)))*32767))
        out+=v.to_bytes(2,'little',signed=True)
    return bytes(out)

def decode_pcm16(blob):
    if isinstance(blob,str): blob=bytes.fromhex(blob)
    return [int.from_bytes(blob[i:i+2],'little',signed=True)/32767 for i in range(0,len(blob),2)]


def import_capture(db_path,file):
    """Validate and store one raw capture.  Returns an acceptance record."""
    raw,doc,digest=_read(file)
    db=connect(db_path)
    result={'capture_id':doc.get('id'),'accepted':False}
    try:
        with db:
            reason=_capture_rejection(db,doc)
            if reason:
                db.execute('INSERT OR REPLACE INTO rejection VALUES(?,?,?)',(doc.get('id') or digest,reason,raw))
                result['reason']=reason
                return result
            n=int(doc['n_samples'])
            aa=doc['antialias'];cutoff=aa.get('cutoff_hz')
            alias_suspect=bool(aa['state']=='bypassed' or cutoff is None or float(cutoff)>=float(doc['sample_hz'])/2)
            if doc['kind']=='pump_control':
                measured=float(doc.get('rms') or 0.0);pcm=b''
            else:
                if doc.get('samples') is not None:
                    pcm=encode_pcm16(doc['samples']);measured=rms(doc['samples'])
                else:
                    pcm=bytes.fromhex(doc['pcm16_hex']);samples=decode_pcm16(pcm)
                    measured=rms(samples[:int(doc['n_samples'])]) if doc['kind']=='array' else rms(samples)
            stored=dict(doc);stored.pop('samples',None);stored['pcm16']=pcm.hex()
            canonical=json.dumps(stored,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            coverage=doc.get('coverage') or [float(doc['at']),float(doc['at'])+n/max(1,int(doc['sample_hz']))]
            existing=db.execute('SELECT sha256 FROM hive_capture WHERE id=?',(doc['id'],)).fetchone()
            if existing is not None and existing['sha256']!=_sha(canonical):
                db.execute('INSERT OR REPLACE INTO rejection VALUES(?,?,?)',(doc['id'],'conflicting_capture_id',raw))
                result['reason']='conflicting_capture_id'
                return result
            db.execute('INSERT OR REPLACE INTO hive_capture VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(
                doc['id'],doc['mic_id'],doc['hive_id'],doc.get('zone','Z1'),doc['kind'],float(doc['at']),
                float(coverage[0]),float(coverage[1]),
                int(doc['sample_hz']),int(doc['seq_start']),int(doc['seq_end']),n,
                json.dumps(doc.get('missing') or []),aa['state'],cutoff,doc['clock_epoch'],
                int(bool(doc['clock_ok'])),int(bool(doc['time_closed'])),doc.get('pump_state'),
                measured,int(alias_suspect),_sha(canonical),canonical,pcm.hex()))
            result.update(accepted=True,cap_id=doc['id'],rms=measured,sha256=_sha(canonical),
                          n_samples=n,alias_suspect=alias_suspect,
                          channels=len((doc.get('array') or {}).get('positions') or []) if doc['kind']=='array' else 1)
            return result
    finally:db.close()

def _capture_rejection(db,doc):
    for key in ('id','mic_id','hive_id','kind','at','sample_hz','seq_start','seq_end','n_samples','clock_epoch','antialias'):
        if key not in doc: return 'missing_'+key
    if doc['kind'] not in ('acoustic','array','pump_control'): return 'unknown_kind'
    if int(doc['sample_hz'])<=0: return 'invalid_rate'
    if doc['kind']!='pump_control':
        samples=doc.get('samples')
        if samples is None and not doc.get('pcm16_hex'): return 'no_samples'
        if samples is not None:
            expected=int(doc['n_samples'])
            if doc['kind']=='array': expected*=len((doc.get('array') or {}).get('positions') or [1])
            if expected!=len(samples): return 'length_mismatch'
            for x in samples:
                if not isinstance(x,(int,float)) or not math.isfinite(float(x)): return 'non_finite_sample'
                if abs(float(x))>1.0: return 'clipping'
        else:
            try: blob=bytes.fromhex(doc['pcm16_hex'])
            except (TypeError,ValueError): return 'invalid_pcm16'
            if doc['kind']=='array': total=int(doc['n_samples'])*len((doc.get('array') or {}).get('positions') or [1])
            else: total=int(doc['n_samples'])
            if len(blob)!=total*2: return 'length_mismatch'
        if int(doc['seq_end'])-int(doc['seq_start'])+1!=int(doc['n_samples']): return 'sequence_gap'
        if doc.get('missing'): return 'broken_frames'
    aa=doc['antialias']
    if not isinstance(aa,dict) or aa.get('state') not in ('engaged','bypassed'): return 'invalid_antialias'
    cutoff=aa.get('cutoff_hz')
    if cutoff is not None:
        if not isinstance(cutoff,(int,float)) or not math.isfinite(float(cutoff)) or float(cutoff)<=0:
            return 'invalid_antialias_cutoff'
    mic=db.execute('SELECT sample_hz,hive_id FROM hive_mic WHERE id=?',(doc['mic_id'],)).fetchone()
    if mic is None: return 'unknown_mic'
    if int(mic['sample_hz'])!=int(doc['sample_hz']): return 'rate_mismatch'
    if mic['hive_id']!=doc['hive_id']: return 'hive_mismatch'
    if doc['kind']=='array':
        geometry=doc.get('array') or {}
        positions=geometry.get('positions') or []
        if len(positions)<3: return 'array_incomplete'
        for p in positions:
            if len(p)<2 or not all(isinstance(v,(int,float)) and math.isfinite(float(v)) for v in p[:2]): return 'array_incomplete'
        if doc.get('samples') is not None and len(doc['samples'])!=len(positions)*int(doc['n_samples']): return 'channel_mismatch'
        if geometry.get('entrance_bearing_deg') is None: return 'entrance_bearing_required'
        occlusion=doc.get('occlusion')
        if occlusion:
            ch=occlusion.get('channel')
            if not isinstance(ch,int) or not 0<=ch<len(positions): return 'invalid_occlusion'
    return _clock_rejection(db,doc)

def _clock_rejection(db,doc):
    if not doc.get('clock_ok') or not doc.get('time_closed'): return 'clock_not_closed'
    rate=int(doc['sample_hz'])
    clock=db.execute('SELECT * FROM hive_clock WHERE epoch_id=?',(doc['clock_epoch'],)).fetchone()
    if clock is None:
        ref=doc.get('clock_ref')
        if not ref or ref.get('ref_time') is None: return 'clock_epoch_unknown'
        db.execute('INSERT INTO hive_clock VALUES(?,?,?,?,?,?)',(doc['clock_epoch'],doc['mic_id'],rate,
            float(ref['ref_time']),int(ref.get('offset_samples',0)),float(ref.get('ppm',0.0))))
        return None
    if clock['mic_id']!=doc['mic_id'] or int(clock['sample_hz'])!=rate: return 'clock_epoch_mismatch'
    ref=doc.get('clock_ref')
    if ref is not None:
        # The whole epoch shares one mapping.  A clip that claims a different
        # reference for the same epoch is rejected rather than phase-shifted.
        if (float(ref.get('ref_time',-1))!=float(clock['ref_time'])
                or int(ref.get('offset_samples',0))!=int(clock['offset_samples'])
                or float(ref.get('ppm',0.0))!=float(clock['ppm'])): return 'clock_epoch_mismatch'
    return None

def clock_of(db,epoch_id):
    row=db.execute('SELECT * FROM hive_clock WHERE epoch_id=?',(epoch_id,)).fetchone()
    return dict(row) if row else None

def clock_sample_time(db,epoch_id,seq):
    """Map a sample sequence to record time through the shared epoch mapping."""
    clk=clock_of(db,epoch_id)
    if clk is None: raise ValueError('clock_epoch_unknown')
    return clk['ref_time']+(seq+clk['offset_samples'])/(clk['sample_hz']*(1+clk['ppm']*1e-6))


# ---------------------------------------------------------------------------
# Pump telemetry and infrared entrance counters

def import_pump_events(db_path,file):
    """Import pump run/stop markers.  A pump stop is not hive silence."""
    raw,doc,digest=_read(file)
    db=connect(db_path)
    try:
        with db:
            accepted=0
            for event in doc['events']:
                if event.get('state') not in ('run','stop'): raise ValueError('invalid_pump_state')
                if 'at' not in event: raise ValueError('pump_event_without_time')
                db.execute('INSERT OR IGNORE INTO pump_event VALUES(?,?,?,?,?,?)',
                    (event['id'],event.get('zone','Z1'),float(event['at']),event['state'],digest,raw))
                accepted+=1
            return {'pump_events':accepted}
    finally:db.close()


def import_infrared(db_path,file):
    """Import signed entrance/exit pulses with a declared coverage window."""
    raw,doc,digest=_read(file)
    db=connect(db_path)
    try:
        with db:
            counter=doc['counter']
            coverage=doc.get('coverage')
            if not coverage or len(coverage)!=2: raise ValueError('coverage_required')
            start,end=map(float,coverage)
            if start>=end: raise ValueError('coverage_empty')
            if not counter.get('hive_id'): raise ValueError('counter_hive_required')
            if not doc.get('complete'): raise ValueError('counter_incomplete')
            db.execute('INSERT OR IGNORE INTO infrared_counter VALUES(?,?,?,?,?,?)',
                (counter['id'],counter['hive_id'],start,end,digest,raw))
            accepted=0;seen=set()
            for pulse in doc['pulses']:
                if pulse.get('direction') not in ('in','out'): raise ValueError('invalid_pulse_direction')
                at=float(pulse['at'])
                if not (start<=at<end): raise ValueError('pulse_outside_coverage')
                if int(pulse['seq']) in seen: raise ValueError('duplicate_pulse_sequence')
                seen.add(int(pulse['seq']))
                db.execute('INSERT OR IGNORE INTO infrared_pulse VALUES(?,?,?,?,?,?)',
                    (pulse['id'],counter['id'],int(pulse['seq']),at,pulse['direction'],
                     json.dumps(pulse,sort_keys=True)))
                accepted+=1
            return {'counter_id':counter['id'],'pulses':accepted,'coverage':[start,end],'complete':True}
    finally:db.close()
