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
        self.add('spray_notice',{'delivered':True},91)
        self.assertEqual(self.result('合同最低覆盖结算'),0)
        self.add('incident',{'loss':1},92)
        self.assertEqual(self.result('事故赔付责任裁定'),'果园方')
    def test_health_change_removes_coverage(self):
        self.assertEqual(self.result('果园分区覆盖认定'),2)
        self.add('health_check',{'healthy':False},93)
        self.assertEqual(self.result('果园分区覆盖认定'),0)
    def test_dispute_freezes_payment(self):
        self.add('settlement',{'amount':13,'disputed':True},94)
        self.assertEqual(self.result('争议款冻结与释放'),13)
