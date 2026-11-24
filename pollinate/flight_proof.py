"""Hive flight proof from sampled audio, array bearing and counters.

The legacy summary path (``pollinate.activity.assess``) can only say that a
clip was loud.  At 200 Hz sampling a 20 Hz irrigation pump and a 180 Hz hive
produce the *same* 200 samples, so loudness and even a spectrum of those
samples cannot decide whether bees left the box.  This module therefore never
turns an RMS ``active`` into a flight fact.  It reports only what the evidence
actually held supports:

* ``confirmed`` -- entrance-band sound inside a pump-free interval, or an
  array whose phase-delay bearing separates the entrance from the pump.
* ``pending``  -- material that cannot decide (entrance band above the sample
  Nyquist, bypassed anti-alias filter, pump that cannot be separated, broken
  frames, an open clock).  Pending is a re-measurement task, not silence.
* ``quiet``    -- a resolvable entrance band was monitored pump-free and no
  entrance source was present.

Infrared counters add entrance crossings *only* for the exact interval they
cover.  They never extrapolate to the rest of the shift, and no source here
writes acoustic activity as completed pollination.
"""
import json, math
from .store import save_flight_assessment, flight_history
from .audio import decode_pcm16

SPEED_OF_SOUND=343.0
ENTRANCE_HZ=180.0
PUMP_HZ=20.0
MIN_AMPLITUDE=0.02
BEARING_TOLERANCE_DEG=20.0
MIN_COHERENCE=0.8


def _now(t): return round(float(t),3)

def _merge(intervals):
    out=[]
    for a,b in sorted((float(x),float(y)) for x,y in intervals):
        if out and a<=out[-1][1]+1e-9: out[-1]=(out[-1][0],max(b,out[-1][1]))
        else: out.append((a,b))
    return out


def goertzel(samples,freq,sample_hz):
    """Single-bin (amplitude, phase) for one real tone; ``None`` if unresolvable."""
    if not samples or freq<=0 or sample_hz<=0 or freq>=sample_hz/2: return None
    w=2*math.pi*freq/sample_hz
    re=sum(x*math.cos(w*i) for i,x in enumerate(samples))
    im=sum(x*math.sin(w*i) for i,x in enumerate(samples))
    return 2*math.hypot(re,im)/len(samples), math.atan2(re,im)


def _bearing(channels,freq,sample_hz,positions,usable):
    """Least-squares plane-wave bearing from inter-channel phase delays."""
    phases=[]
    for i in usable:
        g=goertzel(channels[i],freq,sample_hz)
        if g is None: return None
        phases.append((i,g[1]))
    base=phases[0][0]
    k=2*math.pi*freq/SPEED_OF_SOUND
    A=[];rhs=[]
    for i,ph in phases[1:]:
        dx=positions[i][0]-positions[base][0];dy=positions[i][1]-positions[base][1]
        if k*math.hypot(dx,dy)>=math.pi: return None  # ambiguous phase wrap
        d=(ph-phases[0][1]+math.pi)%(2*math.pi)-math.pi
        A.append((k*dx,k*dy));rhs.append(d)
    a11=sum(r[0]*r[0] for r in A);a12=sum(r[0]*r[1] for r in A);a22=sum(r[1]*r[1] for r in A)
    b1=sum(r[0]*v for r,v in zip(A,rhs));b2=sum(r[1]*v for r,v in zip(A,rhs))
    det=a11*a22-a12*a12
    if abs(det)<1e-12: return None
    ux=(b1*a22-b2*a12)/det;uy=(a11*b2-a12*b1)/det
    return math.degrees(math.atan2(ux,uy))%360.0, math.hypot(ux,uy)


def _angle_gap(a,b): return abs((a-b+180.0)%360.0-180.0)


def _channels(row):
    n=int(row['n_samples']);flat=decode_pcm16(row['pcm16'])
    array=json.loads(row['raw']).get('array') or {}
    count=len(array.get('positions') or []) or 1
    if count==1: return [flat]
    return [flat[i*n:(i+1)*n] for i in range(count)]


def _array_reading(row):
    """Resolve 180 Hz entrance and 20 Hz pump sources for one array capture."""
    doc=json.loads(row['raw']);array=doc['array']
    positions=[[float(p[0]),float(p[1])] for p in array['positions']]
    entrance=float(array['entrance_bearing_deg'])
    channels=_channels(row)
    occlusion=doc.get('occlusion') or {}
    occluded=set(occlusion.get('channels') or ([occlusion['channel']] if occlusion.get('channel') is not None else []))
    usable=[i for i in range(len(channels)) if i not in occluded]
    details={'entrance_bearing_deg':entrance,'channels':len(channels),'occluded':sorted(occluded),'sources':{}}
    entrance_on=False;unresolved=False;pump_on=False
    for name,freq in (('entrance',ENTRANCE_HZ),('pump',PUMP_HZ)):
        mags=[goertzel(ch,freq,row['sample_hz'])[0] for ch in channels]
        amp=max(mags)
        bearing=_bearing(channels,freq,row['sample_hz'],positions,usable) if len(usable)>=2 else None
        entry={'amplitude':round(amp,4),'bearing':None if bearing is None else round(bearing[0],2),
               'coherence':None if bearing is None else round(bearing[1],4),'on_entrance':False}
        if bearing is not None:
            entry['on_entrance']=_angle_gap(bearing[0],entrance)<=BEARING_TOLERANCE_DEG and bearing[1]>=MIN_COHERENCE
        details['sources'][name]=entry
        present=amp>=MIN_AMPLITUDE
        if name=='entrance':
            if present and entry['on_entrance']: entrance_on=True
            elif present: unresolved=True          # loud 180 Hz, but not from the entrance
        elif present:
            pump_on=True
            # A pump whose bearing sits on the entrance makes the entrance ambiguous.
            if entry['on_entrance']: unresolved=True
    details['clock_epoch']=row['clock_epoch']
    details['seq']=[row['seq_start'],row['seq_end']]
    return entrance_on,pump_on,unresolved,details


def _pump_intervals(db,zone):
    rows=[dict(r) for r in db.execute('SELECT * FROM pump_event WHERE zone=? ORDER BY at,id',(zone,))]
    out=[];start=None
    for r in rows:
        if r['state']=='run' and start is None: start=r['at']
        elif r['state']=='stop' and start is not None:
            out.append((start,r['at']));start=None
    return out


def _hits(a,b,intervals): return any(max(a,x)<min(b,y) for x,y in intervals)


def _clock_closed(db,row):
    if not row['clock_ok'] or not row['time_closed']: return False
    clk=db.execute('SELECT * FROM hive_clock WHERE epoch_id=? AND mic_id=?',(row['clock_epoch'],row['mic_id'])).fetchone()
    return clk is not None


def _budget(entry,reason,capture,detail=None):
    item=dict(capture_id=capture['id'],mic_id=capture['mic_id'],kind=capture['kind'],
              start=_now(capture['coverage_start']),end=_now(capture['coverage_end']),
              reason=reason)
    if detail: item['detail']=detail
    return item


def assess_flight(db,hive_id):
    """Compute and version the flight assessment for one hive."""
    captures=[dict(r) for r in db.execute('SELECT * FROM hive_capture WHERE hive_id=? ORDER BY at,id',(hive_id,))]
    zones=sorted({c['zone'] for c in captures}) or ['Z1']
    pump={z:_pump_intervals(db,z) for z in zones}
    confirmed=[];pending=[];quiet=[];arrays=[]
    evidence=set()

    for row in captures:
        evidence.add('capture:'+row['id'])
        a=float(row['coverage_start']);b=float(row['coverage_end'])
        if not _clock_closed(db,row):
            pending.append(_budget('E','clock_not_closed',row));continue
        if row['kind']=='pump_control':
            continue
        if row['kind']=='array':
            entrance,pump_on,unresolved,details=_array_reading(row)
            details['coverage']=[_now(a),_now(b)]
            arrays.append(details)
            if details['channels']-len(details['occluded'])<2:
                pending.append(_budget('E','array_occluded',row));continue
            if entrance:
                confirmed.append((a,b));continue
            if unresolved:
                pending.append(_budget('E','source_not_at_entrance',row));continue
            if pump_on:
                # Pump run/stop is evidence, never a zero-filled "quiet" hive.
                pending.append(_budget('E','pump_run_masks_entrance',row));continue
            quiet.append((a,b));continue
        # Single-channel acoustic capture.
        nyquist=float(row['sample_hz'])/2.0
        if row['alias_suspect']:
            pending.append(_budget('E','alias_suspect',row));continue
        if nyquist<=ENTRANCE_HZ:
            # 180 Hz is above Nyquist; 20 Hz and 180 Hz fold to the same samples.
            # Neither a peak nor an RMS value can decide flight here.
            pending.append(_budget('E','entrance_band_above_nyquist',row));continue
        cutoff=row['antialias_cutoff_hz']
        if cutoff is None or float(cutoff)<ENTRANCE_HZ:
            pending.append(_budget('E','band_not_covered',row));continue
        pumped=_hits(a,b,pump.get(row['zone'],[])) or (row['pump_state'] or '').lower()=='run'
        g=goertzel(_channels(row)[0],ENTRANCE_HZ,row['sample_hz'])
        heard=bool(g and g[0]>=MIN_AMPLITUDE)
        if pumped:
            # Pump run/stop and broken frames are never re-written as quiet.
            pending.append(_budget('E','pump_confounds_entrance_band' if heard else 'pump_on_cannot_clear',row))
        elif heard:
            confirmed.append((a,b))
        else:
            quiet.append((a,b))

    confirmed_iv=_merge(confirmed)
    quiet_iv=_merge(quiet)
    pending_iv=_merge([(p['start'],p['end']) for p in pending])

    counters=_counter_evidence(db,hive_id)
    evidence.update(counters['evidence_ids'])
    for r in db.execute('SELECT id FROM pump_event ORDER BY id'):
        evidence.add('pump_event:'+r['id'])
    for r in db.execute('SELECT a.id FROM hive_audio a JOIN hive_mic m ON a.mic_id=m.id WHERE m.hive_id=?',(hive_id,)):
        evidence.add('audio:'+r['id'])
    for r in db.execute('SELECT id FROM hive_mic WHERE hive_id=?',(hive_id,)):
        evidence.add('mic:'+r['id'])

    provisional=[dict(x) for x in db.execute(
        'SELECT l.* FROM hive_activity_log l JOIN hive_mic m ON l.mic_id=m.id WHERE m.hive_id=? ORDER BY l.mic_id,l.version',(hive_id,))]
    result={
        'hive_id':hive_id,
        'confirmed_acoustic':[{'start':_now(a),'end':_now(b),'seconds':_now(b-a)} for a,b in confirmed_iv],
        'confirmed_acoustic_seconds':_now(sum(b-a for a,b in confirmed_iv)),
        'pending':pending,
        'pending_windows':[[_now(a),_now(b)] for a,b in pending_iv],
        'quiet_acoustic':[{'start':_now(a),'end':_now(b)} for a,b in quiet_iv],
        'array_readings':arrays,
        'provisional':provisional,
        'provisional_latest':provisional[-1] if provisional else None,
        'provisional_is_flight_fact':False,
        'infrared':counters['counters'],
        'contract_pollination_claim':'none',
        'by_hive':{hive_id:{
            'confirmed_acoustic_seconds':_now(sum(b-a for a,b in confirmed_iv)),
            'counter_covered_seconds':_now(sum(c['covered_seconds'] for c in counters['counters'])),
        }},
        'hives_in_evidence':sorted({hive_id}),
        # Which hives each route can actually vouch for, not how many clips exist.
        'hives_with_confirmed_acoustic':[hive_id] if confirmed_iv else [],
        'hives_with_counter_coverage':[hive_id] if counters['counters'] else [],
        'retest_capture_ids':sorted({p['capture_id'] for p in pending}),
        'evidence_ids':sorted(evidence),
    }
    result['version']=save_flight_assessment(db,hive_id,result,result['evidence_ids'])
    return result


def _counter_evidence(db,hive_id):
    counters=[];ids=[]
    for c in db.execute('SELECT * FROM infrared_counter WHERE hive_id=? ORDER BY coverage_start,id',(hive_id,)):
        ids.append('counter:'+c['id'])
        pulses=[dict(r) for r in db.execute('SELECT * FROM infrared_pulse WHERE counter_id=? ORDER BY seq',(c['id'],))]
        ids.extend('pulse:'+p['id'] for p in pulses)
        ins=sum(1 for p in pulses if p['direction']=='in')
        outs=sum(1 for p in pulses if p['direction']=='out')
        counters.append({
            'counter_id':c['id'],
            'coverage':[_now(c['coverage_start']),_now(c['coverage_end'])],
            'covered_seconds':_now(c['coverage_end']-c['coverage_start']),
            'crossings_in':ins,'crossings_out':outs,
            'complete_within_coverage':True,
            'extrapolated_beyond_coverage':False,
            'pollen_delivery_inferred':False,
        })
    return {'counters':counters,'evidence_ids':ids}


def confirmed_flight_seconds(db,hive_id):
    """Confirmed entrance-active seconds; never the provisional loudness status."""
    return assess_flight(db,hive_id)['confirmed_acoustic_seconds']


def explain(db,hive_id):
    """Audit trail: raw captures, provisional versions, flight versions, sources."""
    current=assess_flight(db,hive_id)
    return {
        'hive_id':hive_id,
        'raw_captures':[dict(id=r['id'],kind=r['kind'],sha256=r['sha256'],raw=r['raw'])
                        for r in db.execute('SELECT id,kind,sha256,raw FROM hive_capture WHERE hive_id=? ORDER BY at,id',(hive_id,))],
        'rejections':[dict(reason=r['reason'],payload=r['payload'])
                      for r in db.execute('SELECT reason,payload FROM rejection ORDER BY id')],
        'provisional_versions':[dict(r) for r in db.execute(
            'SELECT l.* FROM hive_activity_log l JOIN hive_mic m ON l.mic_id=m.id WHERE m.hive_id=? ORDER BY l.mic_id,l.version',(hive_id,))],
        'flight_versions':flight_history(db,hive_id),
        'current':current,
    }
