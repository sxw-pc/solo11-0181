import sqlite3, unittest
from pollinate.weigh import import_ticket

class TrailerSeedTests(unittest.TestCase):
    def test_net_load_and_idempotent_ticket(self):
        db=sqlite3.connect(':memory:')
        ticket={'id':'W1','gross_kg':4000,'tare_kg':2200}
        self.assertEqual(import_ticket(db,ticket),1800)
        self.assertEqual(import_ticket(db,ticket),1800)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM weigh_ticket').fetchone()[0],1)
        self.assertEqual(tuple(db.execute('SELECT front_kg,rear_kg FROM trailer_load').fetchone()),(900,900))
