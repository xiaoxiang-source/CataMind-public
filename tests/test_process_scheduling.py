"""Isolated regressions: no host submissions and no production database writes."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from catmat_process_monitor import aggregate, parse_ps, ProcessSampler


class ProcessMonitorTests(unittest.TestCase):
    def test_exact_names_and_recursive_mpi(self):
        rows = parse_ps('''10 1 0 0 1024 2048 01:00 mpirun mpirun cp2k.psmp -i a.inp
11 10 0 0 1024 2048 01:00 prted prted
12 11 90 1 2048 4096 01:00 cp2k.psmp cp2k.psmp -i a.inp
13 1 0 0 1024 2048 01:00 bash bash -c echo cp2k mpirun
20 1 20 1 1024 2048 01:00 python python /x/ml_train.py db cfg
21 20 30 1 2048 4096 01:00 python python -m joblib.worker''')
        result = aggregate(rows)
        self.assertEqual(result['summary']['cp2k_procs'], 1)
        self.assertEqual(result['jobs'][0]['launcher_pid'], 10)
        self.assertEqual(result['modules'][0]['nprocs'], 2)
        self.assertEqual(result['modules'][0]['total_cpu'], 50)
        self.assertNotIn('args', result['cp2k'][0])

    def test_cache_and_failed_probe(self):
        sampler = ProcessSampler()
        with mock.patch('catmat_process_monitor.subprocess.run', return_value=mock.Mock(
                stdout='1 0 0 0 1024 1024 01:00 init init')) as run:
            a, err = sampler.snapshot()
            a['jobs'].append('corruption')
            b, err = sampler.snapshot()
            self.assertEqual(b['jobs'], [])
            self.assertEqual(run.call_count, 1)
        sampler.sampled = 0
        with mock.patch('catmat_process_monitor.subprocess.run', side_effect=OSError('failed')):
            self.assertIsNone(sampler.snapshot()[0])


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name)/'test.db')
        self.patch = mock.patch.object(server, 'DB_PATH', self.db)
        self.patch.start()
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(server.SCHEMA)

    def tearDown(self):
        self.conn.close()
        self.patch.stop()
        self.tmp.cleanup()

    def test_shared_training_admission(self):
        self.conn.execute("INSERT INTO ml_tasks(name,status,pid) VALUES('busy','running',123)")
        self.conn.commit()
        start = mock.Mock(return_value=(42, None))
        with mock.patch.object(server, 'proc_alive', return_value=True), mock.patch.dict(
                os.environ, {'CATMAT_ML_MAX_RUNNING':'1'}):
            tid, err = server._limited_training_start(start)()
        self.assertIsNone(tid)
        self.assertIn('并发已满', err)
        start.assert_not_called()

    def test_training_thread_limits(self):
        cfg = {'n_jobs':-1, 'params':{'rf':{'n_jobs':384}, 'xgb':{'nthread':2}}}
        with mock.patch.dict(os.environ, {'CATMAT_ML_THREADS':'4'}):
            env = server._training_env(cfg)
        self.assertEqual(cfg['n_jobs'], 4)
        self.assertEqual(cfg['params']['rf']['n_jobs'], 4)
        self.assertEqual(cfg['params']['xgb']['nthread'], 2)
        self.assertEqual(env['OPENBLAS_NUM_THREADS'], '4')

    def test_partial_scf_is_not_completed(self):
        out = Path(self.tmp.name)/'cp2k.out'
        out.write_text('SCF run converged\nGEOMETRY OPTIMIZATION COMPLETED\n')
        self.assertIsNone(server.cp2k_out_status(str(out)))
        out.write_text('PROGRAM ENDED AT now\nABORT\n')
        self.assertEqual(server.cp2k_out_status(str(out)), 'failed')

    def test_only_latest_attempt_determines_status(self):
        out = Path(self.tmp.name)/'cp2k.out'
        out.write_text('PROGRAM STARTED AT old\nABORT\nPROGRAM STARTED AT new\nPROGRAM ENDED AT now\n')
        self.assertEqual(server.cp2k_out_status(str(out)), 'done')
        with out.open('a') as stream:
            stream.write('PROGRAM STARTED AT interrupted\nSCF run converged\n')
        self.assertIsNone(server.cp2k_out_status(str(out)))

    def test_queue_nonzero_exit_not_done_even_with_end_marker(self):
        out = Path(self.tmp.name)/'cp2k.out'
        out.write_text('PROGRAM ENDED AT now\n')
        self.conn.execute("INSERT INTO queue(cm_code,name,status,pid,out_log) VALUES('CM-test','test','running',123,?)", (str(out),))
        proc = mock.Mock(returncode=1)
        proc.poll.return_value = 1
        with mock.patch.dict(server.SUBMITTED_PROCS, {123:proc}, clear=True), mock.patch.object(
                server, '_queue_try_start_one', return_value=(None,None)):
            server.queue_scheduler_once(self.conn)
        self.assertEqual(self.conn.execute('SELECT status FROM queue').fetchone()[0], 'failed')

    def test_reader_owns_finalization(self):
        cur = self.conn.execute("INSERT INTO ml_tasks(name,status,pid,log) VALUES('test','running',123,'[]')")
        self.conn.commit()
        with mock.patch.object(server, '_ML_READERS', {cur.lastrowid}), mock.patch.object(server, 'proc_alive', return_value=False):
            server._sync_ml_tasks(self.conn)
        self.assertEqual(self.conn.execute('SELECT status FROM ml_tasks').fetchone()[0], 'running')

    def test_success_json_cannot_override_bad_exit(self):
        cur = self.conn.execute("INSERT INTO ml_tasks(name,status) VALUES('test','running')")
        self.conn.commit()
        self.assertTrue(server._finish_task_once('ml_tasks', cur.lastrowid, 1, '{"ok":true,"result":{}}'))
        self.assertEqual(self.conn.execute('SELECT status FROM ml_tasks').fetchone()[0], 'failed')

if __name__ == '__main__':
    unittest.main()
