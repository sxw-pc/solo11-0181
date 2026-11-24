import json, math, tempfile, unittest
from pathlib import Path
from pollinate.audio import (import_mic, import_audio, import_capture,
                              import_pump_events, import_infrared,
                              encode_pcm16, decode_pcm16)
from pollinate.flight_proof import assess_flight, goertzel, explain
from pollinate.store import connect, activity_history, flight_history

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=ROOT/'fixtures'/'hive-audio-alias.json'


def expected_world_a():
    """sqrt(2)*0.5*sin(2*pi*20*n/200), evaluated exactly on the alias grid."""
    out=[]
    for n in range(200):
        p=n%10                       # 2*pi*20*n/200 = pi*p/5
        sign=1 if p<=5 else -1
        k=min(p,10-p)
        out.append(math.sqrt(2)*0.5*sign*math.sin(math.pi*min(k,5-k)/5))
    return out

def expected_world_b():
    """sqrt(2)*0.5*sin(2*pi*180*n/200+pi) = -sqrt(2)*0.5*sin(9pi*n/5)."""
    out=[]
    for n in range(200):
        p=(9*n+5)%10                 # add pi as a half-turn, keep the grid
        sign=1 if p<=5 else -1
        k=min(p,10-p)
        out.append(math.sqrt(2)*0.5*sign*math.sin(math.pi*min(k,5-k)/5))
    return out

def rms(values): return math.sqrt(sum(v*v for v in values)/len(values))


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.db_path=self.root/'case.db'
        self.fx=json.loads(FIXTURE.read_text(encoding='utf8'))
        self._seq=0
    def tearDown(self):
        self.tmp.cleanup()
    def write(self,doc):
        self._seq+=1
        p=self.root/f'doc{self._seq}.json'
        p.write_text(json.dumps(doc),encoding='utf8')
        return p
    def mic(self,mic_id='M1',hive='H1',hz=200):
        return import_mic(self.db_path,self.write({'mic_id':mic_id,'hive_id':hive,'sample_hz':hz}))
    def db(self): return connect(self.db_path)
    def load_world(self,world,mic_id='M1'):
        for cap in self.fx['worlds'][world]['captures']:
            doc=json.loads(json.dumps(cap));doc['mic_id']=mic_id
            res=import_capture(self.db_path,self.write(doc))
            self.assertTrue(res['accepted'],res)
        import_pump_events(self.db_path,self.write(self.fx['pump_events'][world]))
    def load_array(self,key,mic_id='M2'):
        doc=json.loads(json.dumps(self.fx['array_captures'][key]));doc['mic_id']=mic_id
        res=import_capture(self.db_path,self.write(doc))
        self.assertTrue(res['accepted'],res)
        return res
    def load_infrared(self): return import_infrared(self.db_path,self.write(self.fx['infrared']))


class AliasWorldTests(FixtureCase):
    def test_enhanced_start_reproduces_legacy_active(self):
        self.mic()
        res=import_audio(self.db_path,self.write({'audio_id':'A1','mic_id':'M1','at':10,'rms':0.5}))
        self.assertEqual(res['status'],'active')
        self.assertTrue(res['provisional'])
        conn=self.db()
        try:
            self.assertEqual(conn.execute('SELECT status FROM hive_activity').fetchone()[0],'active')
            hist=activity_history(conn,'M1')
            self.assertEqual([(h['status'],h['rms']) for h in hist],[('active',0.5)])
        finally:conn.close()

    def test_two_worlds_are_pointwise_identical(self):
        world_a=expected_world_a();world_b=expected_world_b()
        self.assertEqual(len(world_a),200)
        self.assertTrue(all(a==b for a,b in zip(world_a,world_b)),
                        '20 Hz and 180 Hz+pi must agree at every n=0..199')
        self.assertAlmostEqual(rms(world_a),0.5,places=12)
        self.assertEqual(encode_pcm16(world_a),encode_pcm16(world_b))

    def test_fixture_matches_the_two_formulas(self):
        self.assertEqual(self.fx['samples']['world_a'],expected_world_a())
        self.assertEqual(self.fx['samples']['world_b'],expected_world_b())
        self.assertTrue(all(c in self.fx['samples']['world_a'] for c in self.fx['samples']['world_b']))

    def test_worlds_are_identical_at_200hz_but_truth_is_opposite(self):
        results={};blobs_by_world={}
        for world in ('world_a','world_b'):
            self.tmp.cleanup();self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
            self.db_path=self.root/'case.db';self._seq=0
            self.mic()
            self.load_world(world)
            conn=self.db()
            try:
                first=conn.execute('SELECT pcm16 FROM hive_capture ORDER BY at').fetchall()
                self.assertEqual(len(first),4)
                blobs=[bytes.fromhex(r[0]) for r in first]
                self.assertTrue(all(b==blobs[0] for b in blobs))
                res=assess_flight(conn,'H1')
            finally:conn.close()
            results[world]=res;blobs_by_world[world]=blobs[0]
        # The stored byte streams of both worlds are identical.
        self.assertEqual(blobs_by_world['world_a'],blobs_by_world['world_b'])
        for world in ('world_a','world_b'):
            self.assertEqual(results[world]['confirmed_acoustic_seconds'],0.0)
            self.assertEqual(len(results[world]['retest_capture_ids']),4)
            self.assertFalse(results[world]['provisional_is_flight_fact'])
            self.assertEqual(results[world]['contract_pollination_claim'],'none')
        self.assertEqual(len(results['world_a']['retest_capture_ids']),
                         len(results['world_b']['retest_capture_ids']))

    def test_spectrum_only_sees_20hz_so_neither_world_is_flight(self):
        for world in ('world_a','world_b'):
            samples=decode_pcm16(encode_pcm16(self.fx['samples'][world]))
            at20=goertzel(samples,20,200)
            self.assertIsNotNone(at20)
            self.assertAlmostEqual(at20[0],math.sqrt(2)*0.5,places=3)
            # 180 Hz is above the 100 Hz Nyquist: no bin exists to report it.
            self.assertIsNone(goertzel(samples,180,200))

    def test_shared_clock_rejects_per_clip_phase_shift(self):
        self.mic()
        cap=json.loads(json.dumps(self.fx['worlds']['world_b']['captures'][0]))
        cap['clock_ref']={'ref_time':0.0,'offset_samples':0,'ppm':40.0}
        self.assertTrue(import_capture(self.db_path,self.write(cap))['accepted'])
        shifted=json.loads(json.dumps(cap));shifted['id']='cap-shifted'
        shifted['clock_ref']={'ref_time':0.0,'offset_samples':7,'ppm':40.0}
        res=import_capture(self.db_path,self.write(shifted))
        self.assertFalse(res['accepted'])
        self.assertEqual(res['reason'],'clock_epoch_mismatch')
        other=json.loads(json.dumps(cap));other['id']='cap-no-clock';other['time_closed']=False
        self.assertEqual(import_capture(self.db_path,self.write(other))['reason'],'clock_not_closed')

    def test_broken_frames_are_rejected_not_zero_filled(self):
        self.mic()
        broken=json.loads(json.dumps(self.fx['worlds']['world_a']['captures'][0]))
        broken['id']='cap-broken';broken['missing']=[3,4]
        res=import_capture(self.db_path,self.write(broken))
        self.assertFalse(res['accepted']);self.assertEqual(res['reason'],'broken_frames')
        gap=json.loads(json.dumps(broken));gap.pop('missing');gap['id']='cap-gap'
        gap['seq_end']+=2
        self.assertEqual(import_capture(self.db_path,self.write(gap))['reason'],'sequence_gap')
        conn=self.db()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM hive_capture').fetchone()[0],0)
            reasons={r[0] for r in conn.execute('SELECT reason FROM rejection')}
            self.assertEqual(reasons,{'broken_frames','sequence_gap'})
            res=assess_flight(conn,'H1')
            self.assertEqual(res['confirmed_acoustic_seconds'],0.0)
            self.assertEqual(res['retest_capture_ids'],[])
        finally:conn.close()

    def test_pump_never_becomes_quiet_evidence(self):
        self.mic()
        self.load_world('world_a')
        conn=self.db()
        try:
            res=assess_flight(conn,'H1')
        finally:conn.close()
        self.assertEqual(res['quiet_acoustic'],[])
        self.assertTrue(all(p['reason']=='entrance_band_above_nyquist' for p in res['pending']))
        # A pump-running clip at a rate that can see 180 Hz still cannot be "quiet".
        self.tmp.cleanup();self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db_path=self.root/'case.db';self._seq=0
        self.mic(hz=1000)
        flow=[0.2*math.sin(2*math.pi*20*n/1000) for n in range(1000)]
        cap=dict(id='cap-1k-pump',mic_id='M1',hive_id='H1',zone='Z1',kind='acoustic',at=0.0,
                 coverage=[0.0,1.0],sample_hz=1000,seq_start=0,seq_end=999,n_samples=1000,
                 missing=[],antialias={'state':'engaged','cutoff_hz':400.0},
                 clock_epoch='E1',clock_ref={'ref_time':0.0,'offset_samples':0,'ppm':0.0},
                 clock_ok=True,time_closed=True,pump_state='run',samples=flow)
        self.assertTrue(import_capture(self.db_path,self.write(cap))['accepted'])
        import_pump_events(self.db_path,self.write({'events':[{'id':'p1','zone':'Z1','at':0.0,'state':'run'}]}))
        conn=self.db()
        try:res=assess_flight(conn,'H1')
        finally:conn.close()
        self.assertEqual(res['confirmed_acoustic_seconds'],0.0)
        self.assertEqual(res['quiet_acoustic'],[])
        self.assertEqual([p['reason'] for p in res['pending']],['pump_on_cannot_clear'])

    def test_true_signal_does_confirm_when_band_is_covered(self):
        self.mic(hz=1000)
        tone=[0.2*math.sin(2*math.pi*180*n/1000) for n in range(1000)]
        cap=dict(id='cap-1k-bees',mic_id='M1',hive_id='H1',zone='Z1',kind='acoustic',at=100.0,
                 coverage=[100.0,101.0],sample_hz=1000,seq_start=0,seq_end=999,n_samples=1000,
                 missing=[],antialias={'state':'engaged','cutoff_hz':400.0},
                 clock_epoch='E1',clock_ref={'ref_time':100.0,'offset_samples':0,'ppm':0.0},
                 clock_ok=True,time_closed=True,pump_state='stop',samples=tone)
        self.assertTrue(import_capture(self.db_path,self.write(cap))['accepted'])
        conn=self.db()
        try:res=assess_flight(conn,'H1')
        finally:conn.close()
        self.assertEqual(res['confirmed_acoustic_seconds'],1.0)
        self.assertEqual(res['confirmed_acoustic'][0]['start'],100.0)


class ArrayAndCounterTests(FixtureCase):
    def test_1khz_array_separates_pump_from_entrance(self):
        self.mic(mic_id='M1',hz=200);self.mic(mic_id='M2',hz=1000)
        import_pump_events(self.db_path,self.write(self.fx['pump_events']['world_a']))
        a=self.load_array('world_a')
        b=self.load_array('world_b')
        conn=self.db()
        try:res=assess_flight(conn,'H1')
        finally:conn.close()
        self.assertEqual(res['evidence_ids'],sorted(set(res['evidence_ids'])))
        self.assertIn('capture:'+a['cap_id'],res['evidence_ids'])
        self.assertIn('capture:'+b['cap_id'],res['evidence_ids'])
        readings={r['coverage'][0]:r for r in res['array_readings']}
        self.assertEqual(len(readings),2)
        # 1 kHz resolves the pump at 210 deg away from the 30 deg entrance.
        pump=readings[200.0]['sources']['pump']
        self.assertAlmostEqual(pump['bearing'],210.0,places=1)
        self.assertFalse(pump['on_entrance'])
        self.assertEqual(res['pending'][0]['reason'],'pump_run_masks_entrance')
        # The pump-free array window is confirmed; the pump window is pending.
        self.assertEqual(res['confirmed_acoustic_seconds'],1.0)
        self.assertEqual(res['confirmed_acoustic'][0]['start'],300.0)
        self.assertEqual(res['quiet_acoustic'],[])

    def test_array_with_pump_on_entrance_bearing_is_pending(self):
        self.mic(mic_id='M1',hz=200);self.mic(mic_id='M2',hz=1000)
        import_pump_events(self.db_path,self.write(self.fx['pump_events']['world_a']))
        doc=json.loads(json.dumps(self.fx['array_captures']['both']))
        # Force the pump source onto the entrance bearing: the array can no
        # longer separate source by direction.
        doc['array']['entrance_bearing_deg']=210.0
        doc['occlusion']={'channel':2,'gain_db':-21.9}
        res=import_capture(self.db_path,self.write(doc))
        self.assertTrue(res['accepted'])
        conn=self.db()
        try:out=assess_flight(conn,'H1')
        finally:conn.close()
        self.assertEqual(out['confirmed_acoustic_seconds'],0.0)
        self.assertEqual(out['quiet_acoustic'],[])
        self.assertEqual([p['reason'] for p in out['pending']],['source_not_at_entrance'])

    def test_infrared_confirm_covers_only_its_two_hours(self):
        self.mic()
        self.load_world('world_b')
        info=self.load_infrared()
        conn=self.db()
        try:
            res=assess_flight(conn,'H1')
        finally:conn.close()
        self.assertEqual(info['coverage'],[200.0,7400.0])
        counter=res['infrared'][0]
        self.assertEqual(counter['coverage'],[200.0,7400.0])
        self.assertEqual(counter['covered_seconds'],7200.0)
        self.assertEqual(counter['crossings_in'],12)
        self.assertEqual(counter['crossings_out'],12)
        self.assertFalse(counter['extrapolated_beyond_coverage'])
        self.assertFalse(counter['pollen_delivery_inferred'])
        # Two hours of counter coverage, not the whole shift.
        self.assertLess(counter['covered_seconds'],8*3600)
        self.assertEqual(res['contract_pollination_claim'],'none')

    def test_counter_for_another_hive_does_not_cover_this_hive(self):
        self.mic()                                   # hive H1
        self.load_world('world_b')
        self.load_infrared()                         # IR-H1 covers H1 only
        import_infrared(self.db_path,self.write(self.fx['infrared_other']))  # IR-H2 -> H2
        conn=self.db()
        try:
            h1=assess_flight(conn,'H1')
            h2=assess_flight(conn,'H2')
        finally:conn.close()
        self.assertEqual([c['counter_id'] for c in h1['infrared']],['IR-H1'])
        self.assertEqual([c['counter_id'] for c in h2['infrared']],['IR-H2'])
        self.assertEqual(h1['hives_in_evidence'],['H1'])
        self.assertEqual(h2['hives_in_evidence'],['H2'])
        # Different hives, different confirmed coverage windows.
        self.assertNotEqual(h1['infrared'][0]['coverage'],h2['infrared'][0]['coverage'])
        self.assertEqual(h2['confirmed_acoustic_seconds'],0.0)

    def test_infrared_pulse_outside_coverage_is_rejected(self):
        self.mic()
        doc=json.loads(json.dumps(self.fx['infrared']))
        doc['pulses'][0]['at']=100.0
        with self.assertRaises(ValueError):
            import_infrared(self.db_path,self.write(doc))
        doc=json.loads(json.dumps(self.fx['infrared']))
        doc['complete']=False
        with self.assertRaises(ValueError):
            import_infrared(self.db_path,self.write(doc))

    def test_infrared_does_not_invent_acoustic_evidence(self):
        self.mic()
        cap=json.loads(json.dumps(self.fx['worlds']['world_b']['captures'][0]))
        cap['antialias']={'state':'bypassed','cutoff_hz':90.0}
        self.assertTrue(import_capture(self.db_path,self.write(cap))['accepted'])
        self.load_infrared()
        conn=self.db()
        try:res=assess_flight(conn,'H1')
        finally:conn.close()
        self.assertEqual(res['confirmed_acoustic_seconds'],0.0)
        self.assertEqual(res['pending'][0]['reason'],'alias_suspect')
        self.assertEqual(res['infrared'][0]['crossings_in'],12)


class VersionTraceTests(FixtureCase):
    def test_raw_audio_provisional_and_flight_ranges_are_versioned(self):
        self.mic()
        self.load_world('world_b')
        import_audio(self.db_path,self.write({'audio_id':'A1','mic_id':'M1','at':10,'rms':0.5}))
        conn=self.db()
        try:
            first=assess_flight(conn,'H1')
        finally:conn.close()
        # A later, louder-but-uninformative clip revises only the provisional row.
        import_audio(self.db_path,self.write({'audio_id':'A2','mic_id':'M1','at':11,'rms':0.9}))
        conn=self.db()
        try:
            second=assess_flight(conn,'H1')
            audit=explain(conn,'H1')
            hist=flight_history(conn,'H1')
        finally:conn.close()
        self.assertEqual(first['version'],1)
        self.assertEqual(second['version'],2)
        self.assertEqual(hist[0]['version'],1)
        self.assertEqual(hist[1]['version'],2)
        self.assertEqual([(h['status'],h['rms'],h['audio_id']) for h in audit['provisional_versions']],
                         [('active',0.5,'A1'),('active',0.9,'A2')])
        self.assertEqual(sorted(c['id'] for c in audit['raw_captures']),
                         sorted(self.fx['worlds']['world_b'] and c['id'] for c in self.fx['worlds']['world_b']['captures']))
        self.assertTrue(all(len(c['sha256'])==64 for c in audit['raw_captures']))
        self.assertTrue(second['evidence_ids'])
        self.assertEqual(second['provisional_latest']['status'],'active')
        self.assertFalse(second['provisional_is_flight_fact'])

    def test_recovered_1khz_buffer_and_counter_change_coverage_differently(self):
        # Recovered 1 kHz buffer path: direction separates pump from entrance.
        self.mic(mic_id='M1',hz=200);self.mic(mic_id='M2',hz=1000)
        import_pump_events(self.db_path,self.write(self.fx['pump_events']['world_a']))
        self.load_array('world_a');self.load_array('world_b')
        conn=self.db()
        try:
            acoustic=assess_flight(conn,'H1')
            acoustic_ev=acoustic['evidence_ids']
        finally:conn.close()
        self.assertEqual(acoustic['confirmed_acoustic_seconds'],1.0)
        self.assertIn('capture:cap-Q-array-B',acoustic_ev)
        # Corrupt-buffer path: same 200 Hz original, only the counter remains.
        self.tmp.cleanup();self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db_path=self.root/'case.db';self._seq=0
        self.mic()
        cap=json.loads(json.dumps(self.fx['worlds']['world_b']['captures'][0]))
        cap['missing']=[7]
        self.assertFalse(import_capture(self.db_path,self.write(cap))['accepted'])
        self.load_infrared()
        conn=self.db()
        try:
            counter=assess_flight(conn,'H1')
            audit=explain(conn,'H1')
        finally:conn.close()
        self.assertEqual(counter['confirmed_acoustic_seconds'],0.0)
        self.assertEqual(counter['infrared'][0]['covered_seconds'],7200.0)
        self.assertEqual(len(audit['rejections']),1)
        self.assertEqual(audit['rejections'][0]['reason'],'broken_frames')
        # Both paths change confirmed coverage, hive count and retest list
        # differently; neither reports contract pollination.
        self.assertEqual(acoustic['hives_with_confirmed_acoustic'],['H1'])
        self.assertEqual(counter['hives_with_confirmed_acoustic'],[])
        self.assertEqual(counter['hives_with_counter_coverage'],['H1'])
        self.assertNotEqual(acoustic['retest_capture_ids'],counter['retest_capture_ids'])
        for res in (acoustic,counter): self.assertEqual(res['contract_pollination_claim'],'none')

    def test_conflicting_identity_is_not_silently_overwritten(self):
        self.mic()
        cap=json.loads(json.dumps(self.fx['worlds']['world_b']['captures'][0]))
        self.assertTrue(import_capture(self.db_path,self.write(cap))['accepted'])
        clone=json.loads(json.dumps(cap));clone['at']+=5.0
        res=import_capture(self.db_path,self.write(clone))
        self.assertFalse(res['accepted'])
        self.assertEqual(res['reason'],'conflicting_capture_id')
        import_audio(self.db_path,self.write({'audio_id':'A1','mic_id':'M1','at':10,'rms':0.5}))
        with self.assertRaises(ValueError):
            import_audio(self.db_path,self.write({'audio_id':'A1','mic_id':'M1','at':10,'rms':0.7}))
        with self.assertRaises(ValueError):
            import_mic(self.db_path,self.write({'mic_id':'M1','hive_id':'H1','sample_hz':400}))

    def test_rejection_is_reachable_for_audit(self):
        self.mic()
        bad=json.loads(json.dumps(self.fx['worlds']['world_a']['captures'][0]))
        bad['antialias']={'state':'bypassed','cutoff_hz':90.0}
        bad['id']='cap-bad'
        self.assertTrue(import_capture(self.db_path,self.write(bad))['alias_suspect'])
        conn=self.db()
        try:res=assess_flight(conn,'H1')
        finally:conn.close()
        self.assertEqual(res['pending'][0]['reason'],'alias_suspect')
