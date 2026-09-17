import sqlite3
from contextlib import contextmanager
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from cleaner.maintenance import DatabaseMaintenance, DATABASES


@contextmanager
def database(path):
    conn=sqlite3.connect(path)
    try:
        with conn: yield conn
    finally: conn.close()


class DatabaseMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = SimpleNamespace(home=self.home, operation=threading.Lock(), worker=None)
        self.service = DatabaseMaintenance(self.store)
        for name in DATABASES:
            with database(self.home / name) as c:
                if name == 'state_5.sqlite':
                    c.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,title TEXT)')
                    c.execute("INSERT INTO threads VALUES('live','keep this session')")
                elif name == 'logs_2.sqlite':
                    c.execute('CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,body TEXT)')
                    c.executemany('INSERT INTO logs(ts,body) VALUES(?,?)', [(int(time.time())-40*86400,'old'),(int(time.time()),'recent')])
                else:
                    c.execute('CREATE TABLE thread_items(thread_id TEXT,item_id TEXT PRIMARY KEY,item_json TEXT)')
                    c.executemany('INSERT INTO thread_items VALUES(?,?,?)',[('live','a','keep'),('unmatched','b','also keep')])
                c.execute('CREATE TABLE scratch(id INTEGER PRIMARY KEY,payload BLOB)')
                c.executemany('INSERT INTO scratch(payload) VALUES(?)',[(b'x'*8192,)]*100)
                c.commit()
                c.execute('DELETE FROM scratch')
    def tearDown(self): self.temp.cleanup()
    def run_plan(self, plan):
        self.service.start(plan['token'])
        deadline=time.monotonic()+10
        while self.service.progress['running'] and time.monotonic()<deadline: time.sleep(.01)
        self.assertFalse(self.service.progress['running'])
        return self.service.progress['results']
    def test_compact_preserves_all_history_and_reports_unused_space(self):
        report=self.service.inspect()['databases']
        self.assertEqual(report[0]['unmatched_sessions'],1)
        self.assertGreater(report[0]['free_bytes'],0)
        result=self.run_plan(self.service.plan(list(DATABASES)))
        self.assertTrue(all(r['status']=='success' for r in result),result)
        self.assertTrue(all(r['released']>0 for r in result))
        with database(self.home/DATABASES[0]) as c:
            self.assertEqual(c.execute('select count(*) from thread_items').fetchone()[0],2)
        with database(self.home/DATABASES[1]) as c:
            self.assertEqual(c.execute('select count(*) from logs').fetchone()[0],2)
        with database(self.home/DATABASES[2]) as c:
            self.assertEqual(c.execute('select title from threads').fetchone()[0],'keep this session')
    def test_retention_deletes_only_old_logs(self):
        plan=self.service.plan(['logs_2.sqlite'],30)
        self.assertEqual(plan['entries'][0]['logs_to_delete'],1)
        result=self.run_plan(plan)
        self.assertEqual(result[0]['deleted_logs'],1)
        with database(self.home/'logs_2.sqlite') as c:
            self.assertEqual(c.execute('select body from logs').fetchall(),[('recent',)])
        with self.assertRaises(ValueError): self.service.start(plan['token'])
    def test_busy_database_does_not_delete_or_force_unlock(self):
        plan=self.service.plan(['logs_2.sqlite'],0)
        with database(self.home/'logs_2.sqlite') as held:
            held.execute('BEGIN IMMEDIATE')
            result=self.run_plan(plan)
            self.assertEqual(result[0]['status'],'failed')
            self.assertEqual(held.execute('select count(*) from logs').fetchone()[0],2)
    def test_shared_operation_lock_and_invalid_inputs(self):
        self.store.operation.acquire()
        try:
            with self.assertRaises(ValueError): self.service.plan(list(DATABASES))
        finally: self.store.operation.release()
        with self.assertRaises(ValueError): self.service.plan(['../state_5.sqlite'])
        with self.assertRaises(ValueError): self.service.plan(['state_5.sqlite'],0)
    def test_stop_before_first_database_preserves_data(self):
        plan=self.service.plan(list(DATABASES),0)
        self.service.stop.set()
        self.store.operation.acquire()
        self.service._run(plan)
        self.assertTrue(all(r['status']=='skipped' for r in self.service.progress['results']))
        with database(self.home/'logs_2.sqlite') as c:
            self.assertEqual(c.execute('select count(*) from logs').fetchone()[0],2)

if __name__=='__main__': unittest.main()
