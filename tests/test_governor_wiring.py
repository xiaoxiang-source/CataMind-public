#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资源 governor 接线隔离测试：不启动服务、不访问生产库、不执行真实计算。"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 旧队列测试并不知道宿主进程可见性门禁；全量测试加载本模块时统一置于影子模式，
# 本模块每个强制模式用例会在 setUp 中显式切回 1。
os.environ["CATMAT_GOVERNOR_ENFORCE"] = "0"

import server  # noqa: E402

# test_core 的备份用例会真实生成后删除文件；把该测试产物定向到 /tmp，避免触碰生产备份目录。
server.BACKUP_DIR = os.path.join(
    tempfile.gettempdir(), "catmat-governor-tests-%s" % os.getpid())


def fake_decision(action, code, message="fake governor decision"):
    return {
        "action": action,
        "code": code,
        "message": message,
        "request": {"nproc": 8, "omp_threads": 1, "cores": 8},
        "est_mem_gb": 11.04,
        "budget": {"physical_cores": 192, "max_total_nproc": 96},
        "checks": [],
    }


class GovernorWiringTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(server.SCHEMA)
        self.enforce = mock.patch.dict(
            os.environ, {"CATMAT_GOVERNOR_ENFORCE": "1"})
        self.enforce.start()

    def tearDown(self):
        self.enforce.stop()
        self.conn.close()

    def _seed_queue(self, nproc=8, omp_threads=1):
        cur = self.conn.execute(
            "INSERT INTO queue(cm_code,name,nproc,payload,status) VALUES(?,?,?,?,?)",
            ("CM-20990101-0001", "queued", nproc,
             json.dumps({"name": "queued", "inp": "&GLOBAL\n&END GLOBAL",
                         "omp_threads": omp_threads}), "queued"))
        self.conn.commit()
        return cur.lastrowid

    def test_reject_queue_enqueue_does_not_insert(self):
        decision = fake_decision("REJECT", "EXCEEDS_HOST_CORES")
        with mock.patch.object(server, "_governor_check", return_value=decision):
            row, err = server.queue_enqueue(
                self.conn, 1, "blocked", "&GLOBAL\n&END GLOBAL", nproc=256)
        self.assertIsNone(row)
        self.assertIn("EXCEEDS_HOST_CORES: fake governor decision", str(err))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0], 0)

    def test_queue_state_enqueue_still_inserts(self):
        decision = fake_decision("QUEUE", "NO_MEMORY_HEADROOM")
        with mock.patch.object(server, "_governor_check", return_value=decision):
            row, err = server.queue_enqueue(
                self.conn, 1, "wait", "&GLOBAL\n&END GLOBAL", nproc=8)
        self.assertIsNone(err)
        self.assertEqual(row["status"], "queued")

    def test_queue_start_reject_fails_item_without_submit(self):
        qid = self._seed_queue()
        decision = fake_decision("REJECT", "EXCEEDS_HOST_CORES")
        with mock.patch.object(server, "_governor_check", return_value=decision), \
             mock.patch.object(server, "submit_cp2k_direct") as submit:
            got_qid, err = server._queue_try_start_one(self.conn)
        self.assertEqual(got_qid, qid)
        self.assertTrue(err.startswith("EXCEEDS_HOST_CORES:"))
        submit.assert_not_called()
        row = self.conn.execute(
            "SELECT status,error FROM queue WHERE id=?", (qid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["error"].startswith("EXCEEDS_HOST_CORES:"))
        audit_row = self.conn.execute(
            "SELECT action,ok FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((audit_row["action"], audit_row["ok"]),
                         ("governor_reject", 0))

    def test_unknown_probe_preserves_queued_item(self):
        qid = self._seed_queue()
        decision = fake_decision("REJECT", "BUDGET_UNKNOWN")
        with mock.patch.object(server, "_governor_check", return_value=decision), \
             mock.patch.object(server, "submit_cp2k_direct") as submit:
            got_qid, err = server._queue_try_start_one(self.conn)
        self.assertIsNone(got_qid)
        submit.assert_not_called()
        row = self.conn.execute("SELECT status,error FROM queue WHERE id=?", (qid,)).fetchone()
        self.assertEqual(row["status"], "queued")
        self.assertTrue(row["error"].startswith("BUDGET_UNKNOWN:"))

    def test_queue_start_queue_keeps_item_without_submit(self):
        qid = self._seed_queue()
        decision = fake_decision("QUEUE", "PLATFORM_CONCURRENCY_FULL")
        with mock.patch.object(server, "_governor_check", return_value=decision), \
             mock.patch.object(server, "submit_cp2k_direct") as submit:
            got_qid, err = server._queue_try_start_one(self.conn)
        self.assertIsNone(got_qid)
        self.assertTrue(err.startswith("PLATFORM_CONCURRENCY_FULL:"))
        submit.assert_not_called()
        status = self.conn.execute(
            "SELECT status FROM queue WHERE id=?", (qid,)).fetchone()[0]
        self.assertEqual(status, "queued")

    def test_queue_start_keeps_item_if_direct_recheck_queues(self):
        qid = self._seed_queue()
        decision = fake_decision("ADMIT", "OK")
        with mock.patch.object(server, "_governor_check", return_value=decision), \
             mock.patch.object(server, "submit_cp2k_direct",
                               return_value=(None, "GOVERNOR_QUEUE: became busy")):
            got_qid, err = server._queue_try_start_one(self.conn)
        self.assertIsNone(got_qid)
        self.assertEqual(err, "GOVERNOR_QUEUE: became busy")
        status = self.conn.execute(
            "SELECT status FROM queue WHERE id=?", (qid,)).fetchone()[0]
        self.assertEqual(status, "queued")

    def test_direct_queue_does_not_popen(self):
        decision = fake_decision("QUEUE", "NO_CORE_HEADROOM")
        with mock.patch.object(server, "_governor_check", return_value=decision), \
             mock.patch.object(server.subprocess, "Popen") as popen:
            data, err = server.submit_cp2k_direct(
                "&GLOBAL\n&END GLOBAL", "blocked", nproc=8, conn=self.conn)
        self.assertIsNone(data)
        self.assertTrue(err.startswith("GOVERNOR_QUEUE:"))
        popen.assert_not_called()

    def test_direct_reject_does_not_popen(self):
        decision = fake_decision("REJECT", "EXCEEDS_PLATFORM_BUDGET")
        with mock.patch.object(server, "_governor_check", return_value=decision), \
             mock.patch.object(server.subprocess, "Popen") as popen:
            data, err = server.submit_cp2k_direct(
                "&GLOBAL\n&END GLOBAL", "blocked", nproc=128, conn=self.conn)
        self.assertIsNone(data)
        self.assertTrue(err.startswith("EXCEEDS_PLATFORM_BUDGET:"))
        popen.assert_not_called()

    def test_direct_admit_calls_fake_executor(self):
        decision = fake_decision("ADMIT", "OK")

        class FakeProcess:
            pid = 424242

        with tempfile.TemporaryDirectory() as run_dir, \
             mock.patch.object(server, "_governor_check", return_value=decision), \
             mock.patch.object(server, "CP2K_RUN_DIR", run_dir), \
             mock.patch.object(server, "cp2k_runtime_env", return_value={"PATH": "/fake"}), \
             mock.patch.object(server, "resolve_mpirun",
                               return_value=("/fake/mpirun", "openmpi", None)), \
             mock.patch.object(server.shutil, "which", return_value="/fake/cp2k.psmp"), \
             mock.patch.object(server.subprocess, "Popen",
                               return_value=FakeProcess()) as popen:
            data, err = server.submit_cp2k_direct(
                "&GLOBAL\n&END GLOBAL", "admitted", nproc=8, conn=self.conn)
        server.SUBMITTED_PROCS.pop(FakeProcess.pid, None)
        self.assertIsNone(err)
        self.assertEqual(data["pid"], FakeProcess.pid)
        popen.assert_called_once()

    def test_verdi_queue_does_not_run_subprocess(self):
        decision = fake_decision("QUEUE", "NO_MEMORY_HEADROOM")
        with mock.patch.object(server, "_governor_check", return_value=decision), \
             mock.patch.object(server.os.path, "isfile", return_value=True), \
             mock.patch.object(server.subprocess, "run") as run:
            data, err = server.submit_verdi_calc(
                self.conn, "blocked", None, "ENERGY",
                resources={"num_mpiprocs_per_machine": 8})
        self.assertIsNone(data)
        self.assertTrue(err.startswith("NO_MEMORY_HEADROOM:"))
        self.assertIn("稍后重试或改用 CM- 队列", err)
        run.assert_not_called()

    def test_verdi_reject_does_not_run_subprocess(self):
        decision = fake_decision("REJECT", "BUDGET_UNKNOWN")
        with mock.patch.object(server, "_governor_check", return_value=decision), \
             mock.patch.object(server.os.path, "isfile", return_value=True), \
             mock.patch.object(server.subprocess, "run") as run:
            data, err = server.submit_verdi_calc(
                self.conn, "blocked", None, "ENERGY", resources=None)
        self.assertIsNone(data)
        self.assertTrue(err.startswith("BUDGET_UNKNOWN:"))
        run.assert_not_called()

    def test_shadow_mode_logs_audits_and_does_not_block(self):
        decision = fake_decision("REJECT", "BUDGET_UNKNOWN")
        with mock.patch.dict(os.environ, {"CATMAT_GOVERNOR_ENFORCE": "0"}), \
             mock.patch.object(server, "_governor_check", return_value=decision), \
             self.assertLogs("catmat", level="WARNING") as logs:
            row, err = server.queue_enqueue(
                self.conn, 1, "shadow", "&GLOBAL\n&END GLOBAL", nproc=8)
        self.assertIsNone(err)
        self.assertEqual(row["status"], "queued")
        self.assertIn("BUDGET_UNKNOWN", "\n".join(logs.output))
        audit_row = self.conn.execute(
            "SELECT action,params,ok FROM audit WHERE action='governor_shadow' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(audit_row)
        self.assertEqual(audit_row["ok"], 1)
        params = json.loads(audit_row["params"])
        self.assertEqual(params,
                         {"code": "BUDGET_UNKNOWN", "nproc": 8, "omp_threads": 1})

    def test_admit_with_fake_probes_enqueues(self):
        with mock.patch.object(server._resource_governor, "host_process_visibility",
                               return_value=(True, "fake host visible")), \
             mock.patch.object(server._resource_governor, "cp2k_process_count",
                               return_value=(0, "fake cp2k=0")), \
             mock.patch.object(server._resource_governor, "mem_available_gb",
                               return_value=(300.0, "fake available")), \
             mock.patch.object(server._resource_governor, "mem_total_gb",
                               return_value=(376.0, "fake total")), \
             mock.patch.object(server, "_physical_core_count", return_value=192), \
             mock.patch.object(server, "_PHYSICAL_CORE_COUNT_RELIABLE", True), \
             mock.patch.object(server, "QUEUE_MAX_RUNNING", 6), \
             mock.patch.object(server, "QUEUE_MAX_TOTAL_NPROC", 96):
            row, err = server.queue_enqueue(
                self.conn, 1, "admitted", "&GLOBAL\n&END GLOBAL", nproc=8)
        self.assertIsNone(err)
        self.assertEqual(row["status"], "queued")

    def test_platform_running_stats_counts_queue_and_tasks(self):
        self.conn.execute(
            "INSERT INTO queue(cm_code,name,nproc,status) VALUES(?,?,?,?)",
            ("CM-20990101-0002", "running-q", 4, "running"))
        self.conn.execute(
            "INSERT INTO tasks(name,resources,status) VALUES(?,?,?)",
            ("running-t", json.dumps({"num_mpiprocs_per_machine": 7}), "running"))
        self.conn.execute(
            "INSERT INTO tasks(name,resources,status) VALUES(?,?,?)",
            ("running-default", "{}", "running"))
        self.assertEqual(server._platform_running_stats(self.conn), (3, 12))

    def test_http_governor_error_has_code_budget_and_409(self):
        decision = fake_decision("REJECT", "EXCEEDS_HOST_CORES")
        err = server._GovernorAdmissionError(decision)
        response = {}

        class FakeHandler:
            def _send(self, status, body):
                response.update(status=status, body=body)

            def _err(self, status, message):  # pragma: no cover - fallback must not run
                self.fail("unexpected fallback")

        server.Handler._governor_err(FakeHandler(), err)
        self.assertEqual(response["status"], 409)
        self.assertEqual(response["body"]["code"], "EXCEEDS_HOST_CORES")
        self.assertEqual(response["body"]["budget"], decision["budget"])
        self.assertIn("EXCEEDS_HOST_CORES:", response["body"]["error"])


if __name__ == "__main__":
    unittest.main()
