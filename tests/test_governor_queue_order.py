#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CM- 队列 governor 顺序回归：内存库 + mock，不启服务不真提交。"""
import json
import os
import sqlite3
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server  # noqa: E402


def snapshot(**updates):
    value = {
        "physical_cores": 192,
        "cores_reliable": True,
        "cores_in_use": 0,
        "mem_total_gb": 376.0,
        "mem_available_gb": 300.0,
        "platform_running_jobs": 0,
        "platform_running_nproc": 0,
        "max_running": 1,
        "max_total_nproc": 96,
        "visibility_reliable": True,
        "visibility_reason": "fake idle host",
    }
    value.update(updates)
    return value


def admitted():
    return server._resource_governor.evaluate(
        {"nproc": 8, "omp_threads": 1}, snapshot())


def fake_submit_data(name="queued", nproc=8, omp_threads=1):
    return {
        "pid": 424242,
        "dir": "/tmp/fake-cp2k-run",
        "inp": "/tmp/fake-cp2k-run/%s.inp" % name,
        "out": "/tmp/fake-cp2k-run/%s.out" % name,
        "nproc": nproc,
        "omp_threads": omp_threads,
        "command": "fake mpirun",
        "mpirun": "/fake/mpirun",
        "mpi_flavor": "fake",
    }


class GovernorQueueOrderTest(unittest.TestCase):
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

    def _insert_queue(self, code, status="queued", nproc=8, omp_threads=1):
        cur = self.conn.execute(
            "INSERT INTO queue(cm_code,name,nproc,payload,status) VALUES(?,?,?,?,?)",
            (code, "queued", nproc,
             json.dumps({"name": "queued", "inp": "&GLOBAL\n&END GLOBAL",
                         "omp_threads": omp_threads}), status))
        self.conn.commit()
        return cur.lastrowid

    def test_historical_impossible_item_is_rejected_before_legacy_nproc_guard(self):
        qid = self._insert_queue("CM-20990101-0001", nproc=200)
        with mock.patch.object(server, "_governor_snapshot",
                               return_value=snapshot()), \
             mock.patch.object(server, "QUEUE_MAX_RUNNING", 1), \
             mock.patch.object(server, "QUEUE_MAX_TOTAL_NPROC", 96), \
             mock.patch.object(server, "submit_cp2k_direct") as submit:
            got_qid, err = server._queue_try_start_one(self.conn)

        self.assertEqual(got_qid, qid)
        self.assertIn("EXCEEDS_HOST_CORES", err)
        submit.assert_not_called()
        row = self.conn.execute(
            "SELECT status,error FROM queue WHERE id=?", (qid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("EXCEEDS_HOST_CORES", row["error"])
        audit_row = self.conn.execute(
            "SELECT action,ok FROM audit WHERE action='governor_reject' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(audit_row)
        self.assertEqual((audit_row["action"], audit_row["ok"]),
                         ("governor_reject", 0))

    def test_queue_decision_keeps_item_queued_without_submit(self):
        qid = self._insert_queue("CM-20990101-0002", nproc=32)
        busy = snapshot(cores_in_use=150)
        with mock.patch.object(server, "_governor_snapshot", return_value=busy), \
             mock.patch.object(server, "QUEUE_MAX_RUNNING", 2), \
             mock.patch.object(server, "QUEUE_MAX_TOTAL_NPROC", 96), \
             mock.patch.object(server, "submit_cp2k_direct") as submit:
            got_qid, err = server._queue_try_start_one(self.conn)

        self.assertIsNone(got_qid)
        self.assertIn("NO_CORE_HEADROOM", err)
        submit.assert_not_called()
        status = self.conn.execute(
            "SELECT status FROM queue WHERE id=?", (qid,)).fetchone()[0]
        self.assertEqual(status, "queued")

    def test_shadow_mode_still_honors_legacy_running_guard(self):
        self._insert_queue("CM-20990101-0003", status="running", nproc=1)
        qid = self._insert_queue("CM-20990101-0004", nproc=8)
        full = snapshot(platform_running_jobs=1, platform_running_nproc=1)
        with mock.patch.dict(os.environ, {"CATMAT_GOVERNOR_ENFORCE": "0"}), \
             mock.patch.object(server, "_governor_snapshot", return_value=full), \
             mock.patch.object(server, "QUEUE_MAX_RUNNING", 1), \
             mock.patch.object(server, "QUEUE_MAX_TOTAL_NPROC", 96), \
             mock.patch.object(server, "submit_cp2k_direct") as submit:
            got_qid, err = server._queue_try_start_one(self.conn)

        self.assertIsNone(got_qid)
        self.assertEqual(err, "busy")
        submit.assert_not_called()
        status = self.conn.execute(
            "SELECT status FROM queue WHERE id=?", (qid,)).fetchone()[0]
        self.assertEqual(status, "queued")
        audit_row = self.conn.execute(
            "SELECT action,ok FROM audit WHERE action='governor_shadow' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(audit_row)
        self.assertEqual((audit_row["action"], audit_row["ok"]),
                         ("governor_shadow", 1))

    def test_admit_calls_fake_executor_and_marks_item_running(self):
        qid = self._insert_queue("CM-20990101-0005")
        with mock.patch.object(server, "_governor_snapshot",
                               return_value=snapshot()), \
             mock.patch.object(server, "QUEUE_MAX_RUNNING", 1), \
             mock.patch.object(server, "QUEUE_MAX_TOTAL_NPROC", 96), \
             mock.patch.object(server, "submit_cp2k_direct",
                               return_value=(fake_submit_data(), None)) as submit:
            got_qid, err = server._queue_try_start_one(self.conn)

        self.assertEqual(got_qid, qid)
        self.assertIsNone(err)
        submit.assert_called_once()
        row = self.conn.execute(
            "SELECT status,pid FROM queue WHERE id=?", (qid,)).fetchone()
        self.assertEqual((row["status"], row["pid"]), ("running", 424242))

    def test_governor_check_precedes_executor(self):
        self._insert_queue("CM-20990101-0006")
        calls = []

        def check(*args):
            calls.append("governor")
            return admitted()

        def submit(*args, **kwargs):
            calls.append("submit")
            return fake_submit_data(), None

        with mock.patch.object(server, "_governor_check", side_effect=check), \
             mock.patch.object(server, "QUEUE_MAX_RUNNING", 1), \
             mock.patch.object(server, "QUEUE_MAX_TOTAL_NPROC", 96), \
             mock.patch.object(server, "submit_cp2k_direct", side_effect=submit):
            server._queue_try_start_one(self.conn)

        self.assertEqual(calls, ["governor", "submit"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
