#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AiiDA 批量工作流 governor 接线测试：仅使用内存库、临时目录和 mock。"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server  # noqa: E402


def snapshot():
    return {
        "physical_cores": 192,
        "cores_reliable": True,
        "cores_in_use": 0,
        "mem_total_gb": 376.0,
        "mem_available_gb": 300.0,
        "platform_running_jobs": 0,
        "platform_running_nproc": 0,
        "max_running": 6,
        "max_total_nproc": 96,
        "visibility_reliable": True,
        "visibility_reason": "test snapshot",
    }


def decision(action, code, message="test decision"):
    return {
        "action": action,
        "code": code,
        "message": message,
        "request": {"nproc": 32, "omp_threads": 1, "cores": 32},
        "est_mem_gb": 44.16,
        "budget": {"physical_cores": 192, "max_total_nproc": 96},
        "checks": [],
    }


class GovernorBatchWiringTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(server.SCHEMA)
        self.env = mock.patch.dict(
            os.environ,
            {"CATMAT_GOVERNOR_ENFORCE": "1",
             "CATMAT_GOVERNOR_BATCH_MODE": "strict"})
        self.env.start()
        self.structs = [
            {"label": "s%03d" % i, "aiida_pk": i + 1}
            for i in range(128)
        ]

    def tearDown(self):
        self.env.stop()
        self.conn.close()

    def _real_decision(self, _conn, nproc, omp_threads):
        return server._resource_governor.evaluate(
            {"nproc": nproc, "omp_threads": omp_threads}, snapshot())

    def test_mode_a_large_batch_uses_per_calculation_cores_and_admits(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(server, "TMP_DIR", tmp), \
             mock.patch.object(server.os.path, "isfile", return_value=True), \
             mock.patch.object(server, "_governor_check",
                               side_effect=self._real_decision) as check, \
             mock.patch.object(server.subprocess, "run") as run:
            data, err = server.submit_catmat_workchain(
                self.conn, "mode-a", self.structs, mode="A",
                resources={"num_mpiprocs_per_machine": 32}, dry_run=True)
        self.assertIsNone(err)
        self.assertEqual((data["governor"]["action"], data["governor"]["code"]),
                         ("ADMIT", "OK"))
        check.assert_called_once_with(self.conn, 32, 1)
        run.assert_not_called()

    def test_mode_b_large_batch_is_rejected_before_file_or_subprocess(self):
        original = self._real_decision(self.conn, 4096, 1)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(server, "TMP_DIR", tmp), \
             mock.patch.object(server.os.path, "isfile", return_value=True), \
             mock.patch.object(server, "_governor_check",
                               return_value=original) as check, \
             mock.patch.object(server.subprocess, "run") as run, \
             mock.patch.object(server.subprocess, "Popen") as popen:
            data, err = server.submit_catmat_workchain(
                self.conn, "mode-b", self.structs, mode="B",
                resources={"num_mpiprocs_per_machine": 32})
            files = os.listdir(tmp)
        self.assertIsNone(data)
        self.assertIsInstance(err, server._GovernorAdmissionError)
        # 4096 > 192，先命中顺序更靠前的“超宿主物理核”规则。
        self.assertEqual(
            (err.decision["action"], err.decision["code"]),
            ("REJECT", "EXCEEDS_HOST_CORES"))
        self.assertEqual(err.decision["code"], original["code"])
        self.assertIn("批计划：128 结构 × 32 核 = 4096 核", str(err))
        self.assertIsNot(err.decision, original)
        self.assertNotIn("批计划", original["message"])
        check.assert_called_once_with(self.conn, 4096, 1)
        self.assertEqual(files, [])
        run.assert_not_called()
        popen.assert_not_called()

    def test_mode_b_batch_only_exceeding_platform_budget_is_rejected(self):
        structs = self.structs[:4]
        original = self._real_decision(self.conn, 128, 1)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(server, "TMP_DIR", tmp), \
             mock.patch.object(server.os.path, "isfile", return_value=True), \
             mock.patch.object(server, "_governor_check",
                               return_value=original) as check, \
             mock.patch.object(server.subprocess, "run") as run, \
             mock.patch.object(server.subprocess, "Popen") as popen:
            data, err = server.submit_catmat_workchain(
                self.conn, "mode-b-platform-budget", structs, mode="B",
                resources={"num_mpiprocs_per_machine": 32})
            files = os.listdir(tmp)
        self.assertIsNone(data)
        self.assertIsInstance(err, server._GovernorAdmissionError)
        self.assertEqual(
            (err.decision["action"], err.decision["code"]),
            ("REJECT", "EXCEEDS_PLATFORM_BUDGET"))
        self.assertIn("批计划：4 结构 × 32 核 = 128 核", str(err))
        check.assert_called_once_with(self.conn, 128, 1)
        self.assertEqual(files, [])
        run.assert_not_called()
        popen.assert_not_called()

    def test_per_calc_mode_b_uses_only_one_calculation(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"CATMAT_GOVERNOR_BATCH_MODE": "per_calc"}), \
             mock.patch.object(server, "TMP_DIR", tmp), \
             mock.patch.object(server.os.path, "isfile", return_value=True), \
             mock.patch.object(server, "_governor_check",
                               side_effect=self._real_decision) as check, \
             mock.patch.object(server.subprocess, "run") as run:
            data, err = server.submit_catmat_workchain(
                self.conn, "mode-b-per-calc", self.structs, mode="B",
                resources={"num_mpiprocs_per_machine": 32}, dry_run=True)
        self.assertIsNone(err)
        self.assertEqual(data["governor"]["action"], "ADMIT")
        check.assert_called_once_with(self.conn, 32, 1)
        run.assert_not_called()

    def test_dry_run_reports_reject_without_blocking_or_verdi(self):
        rejected = decision("REJECT", "EXCEEDS_PLATFORM_BUDGET")
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(server, "TMP_DIR", tmp), \
             mock.patch.object(server.os.path, "isfile", return_value=True), \
             mock.patch.object(server, "_governor_check", return_value=rejected), \
             mock.patch.object(server.subprocess, "run") as run:
            data, err = server.submit_catmat_workchain(
                self.conn, "preview", [{"label": "one", "aiida_pk": 1}],
                mode="B", dry_run=True)
            self.assertTrue(os.path.isfile(data["cfg_path"]))
        self.assertIsNone(err)
        self.assertIs(data["governor"], rejected)
        run.assert_not_called()

    def test_queue_error_has_decision_and_required_advice(self):
        queued = decision("QUEUE", "NO_CORE_HEADROOM", "no headroom")
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(server, "TMP_DIR", tmp), \
             mock.patch.object(server.os.path, "isfile", return_value=True), \
             mock.patch.object(server, "_governor_check", return_value=queued), \
             mock.patch.object(server.subprocess, "run") as run:
            data, err = server.submit_catmat_workchain(
                self.conn, "queued", [{"label": "one", "aiida_pk": 1}])
            files = os.listdir(tmp)
        self.assertIsNone(data)
        self.assertIsInstance(err, server._GovernorAdmissionError)
        self.assertEqual(err.decision["code"], "NO_CORE_HEADROOM")
        self.assertIn("结构", str(err))
        self.assertIn("核", str(err))
        self.assertIn(
            "请减小批大小、降低 num_mpiprocs_per_machine，"
            "或改用 mode A（逐结构链式）", str(err))
        self.assertEqual(queued["message"], "no headroom")
        self.assertEqual(files, [])
        run.assert_not_called()

    def test_shadow_mode_does_not_block_and_records_audit(self):
        rejected = decision("REJECT", "BUDGET_UNKNOWN")
        completed = mock.Mock(stdout="SUBMITTED_PK=123\n", stderr="")
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"CATMAT_GOVERNOR_ENFORCE": "0"}), \
             mock.patch.object(server, "TMP_DIR", tmp), \
             mock.patch.object(server.os.path, "isfile", return_value=True), \
             mock.patch.object(server, "_governor_check", return_value=rejected), \
             mock.patch.object(server.subprocess, "run", return_value=completed) as run, \
             self.assertLogs("catmat", level="WARNING"):
            data, err = server.submit_catmat_workchain(
                self.conn, "shadow", [{"label": "one", "aiida_pk": 1}])
        self.assertIsNone(err)
        self.assertEqual(data["pk"], 123)
        run.assert_called_once()
        audit_row = self.conn.execute(
            "SELECT action,ok FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((audit_row["action"], audit_row["ok"]),
                         ("governor_shadow", 1))

    def test_hts_governor_error_branch_uses_409_helper(self):
        source = __import__("inspect").getsource(server.Handler._hts)
        expected = (
            'if getattr(werr, "decision", None):\n'
            '                        return self._governor_err(werr)')
        self.assertIn(expected, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
