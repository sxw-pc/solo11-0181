import tempfile, unittest
from pathlib import Path
import pollinate
from pollinate.store import connect, ingest, evidence, save_decisions, report
from pollinate.__main__ import run_fixture

class BusinessDecisions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.temp.name)/'case.db')
        for module in pollinate.MODULES: run_fixture(self.db,module.NORMAL)
    def tearDown(self): self.db.close(); self.temp.cleanup()
    def add(self,kind,data,time=90):
        subject='case-1'
        with self.db:
            ingest(self.db,dict(id=f'extra-{kind}-{time}',kind=kind,subject=subject,event_time=time,received_at=time+100,data=data))
            records=evidence(self.db,subject)
            results=[]
            for module in pollinate.MODULES: results.extend(module.decide(records))
            save_decisions(self.db,subject,results)
    def result(self,name): return report(self.db,'case-1')[name]['result']
    def test_spray_changes_fee_and_accident_responsibility(self):
        self.assertEqual(self.result('合同最低覆盖结算'),20)
        self.add('spray_notice',{'delivered':True,'zone':'Z1','start':1,'end':3},91)
        self.assertEqual(self.result('合同最低覆盖结算'),0)
        self.add('incident',{'loss':1,'time':2,'zone':'Z1'},92)
        self.assertEqual(self.result('事故赔付责任裁定'),'果园方')
    def test_health_change_removes_coverage(self):
        self.assertEqual(self.result('果园分区覆盖认定'),2)
        self.add('health_check',{'healthy':False},93)
        self.assertEqual(self.result('果园分区覆盖认定'),0)
        self.assertEqual(self.result('合同最低覆盖结算'),0)
    def test_dispute_freezes_payment(self):
        self.add('settlement',{'amount':13,'disputed':True},94)
        ledger=self.result('争议款冻结与释放')
        self.assertEqual(ledger['reserved_total'],1300)
        self.assertEqual(ledger['payable'],700)
        self.assertEqual(ledger['shares']['D0']['status'],'reserved')
    def test_partial_spray_intersects_bloom_window(self):
        self.assertEqual(self.result('果园分区覆盖认定'),2)
        self.add('spray_notice',{'delivered':True,'zone':'Z1','start':2,'end':3},95)
        self.assertEqual(self.result('果园分区覆盖认定'),1)
        self.assertEqual(self.result('合同最低覆盖结算'),0)
    def test_replacement_overlap_is_not_double_billed(self):
        self.add('placement',{'zone':'Z1','start':2,'end':3,'replacement':True,'replaces':'pollinate-placement-ok'},96)
        self.assertEqual(self.result('驻场时段认定'),3)
        self.assertEqual(self.result('果园分区覆盖认定'),2)
        self.assertEqual(self.result('蜂箱替补额度分配'),'继承')
    def test_dispute_release_is_idempotent_balance(self):
        self.add('settlement',{'dispute_id':'D1','action':'freeze','amount':13},97)
        self.assertEqual(self.result('争议款冻结与释放')['reserved_total'],1300)
        self.add('settlement',{'dispute_id':'D1','action':'release'},98)
        ledger=self.result('争议款冻结与释放')
        self.assertEqual(ledger['reserved_total'],0)
        self.assertEqual(ledger['shares']['D1']['status'],'released')
    def test_release_waits_for_coverage_after_revision(self):
        self.add('settlement',{'dispute_id':'D1','action':'freeze','amount':13},97)
        self.add('spray_notice',{'delivered':True,'zone':'Z1','start':1,'end':3},99)
        self.add('settlement',{'dispute_id':'D1','action':'release'},100)
        self.assertEqual(self.result('合同最低覆盖结算'),0)
        ledger=self.result('争议款冻结与释放')
        self.assertEqual(ledger['reserved_total'],0)
        self.assertEqual(ledger['shares']['D1']['status'],'revoked')
        self.assertEqual(ledger['recoverable_total'],1300)
    def test_late_delivery_and_expired_health_have_distinct_results(self):
        self.add('spray_notice',{'delivered':True,'zone':'Z1','start':1,'end':3,'delivered_at':3},101)
        self.assertEqual(self.result('果园分区覆盖认定'),2)
        self.add('health_check',{'healthy':True,'valid_from':0,'valid_to':2},102)
        self.assertEqual(self.result('果园分区覆盖认定'),1)
    def test_bloom_replacement_dispute_release_path(self):
        self.add('placement',{'zone':'Z1','start':2,'end':3,'replacement':True,'replaces':'pollinate-placement-ok'},103)
        self.add('bloom_observation',{'window':[1,2]},104)
        self.assertEqual(self.result('果园分区覆盖认定'),1)
        self.add('settlement',{'dispute_id':'D2','action':'freeze','amount':13},105)
        self.add('settlement',{'dispute_id':'D2','action':'release'},106)
        # coverage is still short, so the freeze is not payable: it is revoked,
        # not silently released, and the release is recorded as blocked
        blocked=self.result('争议款冻结与释放')
        self.assertEqual(blocked['reserved_total'],0)
        self.assertEqual(blocked['shares']['D2']['status'],'revoked')
        self.assertEqual(blocked['recoverable_total'],1300)
        self.add('bloom_observation',{'window':[1,3]},107)
        restored=self.result('争议款冻结与释放')
        self.assertEqual(restored['reserved_total'],1300)
        self.assertEqual(restored['shares']['D2']['status'],'reserved')
        self.add('settlement',{'dispute_id':'D2','action':'release'},108)
        self.assertEqual(self.result('果园分区覆盖认定'),2)
        self.assertEqual(self.result('争议款冻结与释放')['reserved_total'],0)

    def test_independent_hives_add_coverage(self):
        self.add('placement',{'zone':'Z1','start':1,'end':3,'allocation':'independent-hive'},109)
        self.assertEqual(self.result('果园分区覆盖认定'),4)
