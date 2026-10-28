#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资源准入探测、快照和九条纯策略规则测试。"""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import catmat_resource_governor as governor  # noqa: E402


def snapshot(**updates):
    value = {
        "physical_cores": 192,
        "cores_reliable": True,
        "cores_in_use": 0,
        "mem_total_gb": 376.0,
        "mem_available_gb": 300.0,
        "platform_running_jobs": 0,
        "platform_running_nproc": 0,
        "max_running": 2,
        "max_total_nproc": 96,
        "visibility_reliable": True,
        "visibility_reason": "测试快照可见",
    }
    value.update(updates)
    return value


class TestProbeLayer(unittest.TestCase):
    def test_visibility_rejects_non_host_pid_one(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="python\n")):
            reliable, reason = governor.host_process_visibility()
        self.assertIs(reliable, False)
        self.assertIn("python", reason)
        self.assertIn("PID namespace", reason)

    def test_visibility_rejects_too_few_processes(self):
        entries = []
        for number in range(4):
            entry = mock.Mock()
            entry.name = str(number + 1)
            entry.is_dir.return_value = True
            entries.append(entry)
        context = mock.MagicMock()
        context.__enter__.return_value = iter(entries)
        with mock.patch("builtins.open", mock.mock_open(read_data="systemd\n")), \
             mock.patch.object(governor.os, "scandir", return_value=context):
            reliable, reason = governor.host_process_visibility()
        self.assertIs(reliable, False)
        self.assertIn("4", reason)
        self.assertIn("<32", reason)

    def test_pgrep_exit_one_with_zero_is_valid_zero(self):
        completed = subprocess.CompletedProcess([], 1, stdout="0\n", stderr="")
        with mock.patch.object(governor, "host_process_visibility",
                               return_value=(True, "可信")), \
             mock.patch.object(governor.subprocess, "run", return_value=completed):
            count, reason = governor.cp2k_process_count()
        self.assertEqual(count, 0)
        self.assertIn("count=0", reason)

    def test_cp2k_probe_stops_when_visibility_is_unreliable(self):
        with mock.patch.object(governor, "host_process_visibility",
                               return_value=(False, "仅 4 个进程")), \
             mock.patch.object(governor.subprocess, "run") as run:
            count, reason = governor.cp2k_process_count()
        self.assertIsNone(count)
        self.assertIn("不可信", reason)
        run.assert_not_called()

    def test_meminfo_is_converted_from_kib_to_gib(self):
        data = "MemTotal:       2097152 kB\nMemAvailable:   1048576 kB\n"
        with mock.patch("builtins.open", return_value=io.StringIO(data)):
            self.assertEqual(governor.mem_available_gb()[0], 1.0)
        with mock.patch("builtins.open", return_value=io.StringIO(data)):
            self.assertEqual(governor.mem_total_gb()[0], 2.0)

    def test_snapshot_preserves_unknown_probe_values(self):
        with mock.patch.object(governor, "host_process_visibility",
                               return_value=(False, "隔离")), \
             mock.patch.object(governor, "mem_available_gb",
                               return_value=(None, "不可读")), \
             mock.patch.object(governor, "mem_total_gb",
                               return_value=(None, "不可读")), \
             mock.patch.object(governor, "cp2k_process_count") as cp2k:
            result = governor.build_snapshot(192, True, 0, 0, 2, 96)
        self.assertIsNone(result["cores_in_use"])
        self.assertIsNone(result["mem_available_gb"])
        self.assertIsNone(result["mem_total_gb"])
        cp2k.assert_not_called()

    def test_snapshot_marks_budget_unreliable_when_cp2k_count_fails(self):
        with mock.patch.object(governor, "host_process_visibility",
                               return_value=(True, "宿主可见")), \
             mock.patch.object(governor, "cp2k_process_count",
                               return_value=(None, "pgrep 失败")), \
             mock.patch.object(governor, "mem_available_gb",
                               return_value=(300.0, "正常")), \
             mock.patch.object(governor, "mem_total_gb",
                               return_value=(376.0, "正常")):
            result = governor.build_snapshot(192, True, 0, 0, 2, 96)
        self.assertIs(result["visibility_reliable"], False)
        self.assertIsNone(result["cores_in_use"])
        decision = governor.evaluate({"nproc": 32}, result)
        self.assertEqual((decision["action"], decision["code"]),
                         ("REJECT", "BUDGET_UNKNOWN"))


class TestPolicyLayer(unittest.TestCase):
    def assert_decision(self, request, snap, action, code):
        result = governor.evaluate(request, snap)
        self.assertEqual((result["action"], result["code"]), (action, code))
        self.assertEqual([item["rule"] for item in result["checks"]],
                         list(governor._RULES))
        self.assertEqual(len(result["checks"]), 9)
        return result

    def test_invalid_request(self):
        for request in ({"nproc": 0}, {"nproc": True},
                        {"nproc": 1, "omp_threads": False}):
            with self.subTest(request=request):
                self.assert_decision(request, snapshot(), "REJECT", "INVALID_REQUEST")

    def test_budget_unknown_for_unreliable_visibility(self):
        self.assert_decision({"nproc": 32}, snapshot(visibility_reliable=False),
                             "REJECT", "BUDGET_UNKNOWN")

    def test_budget_unknown_for_each_required_safety_input(self):
        cases = (
            ("cores_in_use", {}),
            ("platform_running_jobs", {}),
            ("max_running", {}),
            ("platform_running_nproc", {"max_total_nproc": 96}),
        )
        for field, extra in cases:
            with self.subTest(field=field):
                updates = dict(extra)
                updates[field] = None
                self.assert_decision({"nproc": 32}, snapshot(**updates),
                                     "REJECT", "BUDGET_UNKNOWN")

    def test_unknown_platform_nproc_is_allowed_without_total_nproc_limit(self):
        for max_total_nproc in (0, None):
            with self.subTest(max_total_nproc=max_total_nproc):
                snap = snapshot(max_total_nproc=max_total_nproc,
                                platform_running_nproc=None)
                self.assert_decision({"nproc": 32}, snap, "ADMIT", "OK")

    def test_exceeds_host_cores(self):
        self.assert_decision({"nproc": 200}, snapshot(max_total_nproc=0),
                             "REJECT", "EXCEEDS_HOST_CORES")

    def test_exceeds_platform_budget_two_144_core_jobs_case(self):
        self.assert_decision({"nproc": 144}, snapshot(max_total_nproc=96),
                             "REJECT", "EXCEEDS_PLATFORM_BUDGET")

    def test_exceeds_host_memory(self):
        snap = snapshot(mem_total_gb=200.0, mem_available_gb=180.0,
                        max_total_nproc=0)
        self.assert_decision({"nproc": 96}, snap,
                             "REJECT", "EXCEEDS_HOST_MEMORY")

    def test_platform_concurrency_full(self):
        snap = snapshot(platform_running_jobs=2, max_running=2)
        self.assert_decision({"nproc": 32}, snap,
                             "QUEUE", "PLATFORM_CONCURRENCY_FULL")

    def test_external_64_cores_leave_no_headroom_for_144(self):
        snap = snapshot(cores_in_use=64, max_total_nproc=0, max_running=2)
        self.assert_decision({"nproc": 144}, snap,
                             "QUEUE", "NO_CORE_HEADROOM")

    def test_low_mem_available_has_no_memory_headroom(self):
        snap = snapshot(mem_total_gb=376.0, mem_available_gb=100.0)
        self.assert_decision({"nproc": 32}, snap,
                             "QUEUE", "NO_MEMORY_HEADROOM")

    def test_ok(self):
        result = self.assert_decision({"nproc": 32}, snapshot(), "ADMIT", "OK")
        self.assertAlmostEqual(result["est_mem_gb"], 44.16)
        self.assertEqual(result["request"]["cores"], 32)
        self.assertEqual(set(result["budget"]), {
            "physical_cores", "cores_reliable", "cores_in_use", "cores_free",
            "core_reserve", "mem_total_gb", "mem_available_gb", "min_avail_gb",
            "mem_per_rank_gb", "mem_reserve_ratio", "platform_running_jobs",
            "platform_running_nproc", "max_running", "max_total_nproc",
            "visibility_reliable", "visibility_reason",
        })

    def test_rule_order_uses_first_match(self):
        snap = snapshot(physical_cores=16, mem_total_gb=1,
                        platform_running_jobs=9, max_running=1)
        self.assert_decision({"nproc": 32}, snap,
                             "REJECT", "EXCEEDS_HOST_CORES")

    def test_omp_threads_count_toward_cores_but_not_rank_memory(self):
        result = self.assert_decision({"nproc": 32, "omp_threads": 4},
                                      snapshot(max_total_nproc=0),
                                      "ADMIT", "OK")
        self.assertEqual(result["request"]["cores"], 128)
        self.assertAlmostEqual(result["est_mem_gb"], 44.16)

    def test_custom_policy_does_not_mutate_defaults(self):
        custom = {"min_avail_gb": 100}
        before_default = dict(governor.DEFAULT_POLICY)
        before_custom = dict(custom)
        governor.evaluate({"nproc": 32}, snapshot(), custom)
        self.assertEqual(governor.DEFAULT_POLICY, before_default)
        self.assertEqual(custom, before_custom)

    def test_evaluate_is_pure_and_repeatable(self):
        request = {"nproc": 32}
        snap = snapshot()
        request_before = dict(request)
        snap_before = dict(snap)
        environment_before = dict(os.environ)
        with tempfile.TemporaryDirectory() as temporary:
            old_cwd = os.getcwd()
            try:
                os.chdir(temporary)
                files_before = os.listdir(".")
                with mock.patch("builtins.open", side_effect=AssertionError("file access")), \
                     mock.patch.object(governor.subprocess, "run",
                                       side_effect=AssertionError("process access")):
                    first = governor.evaluate(request, snap)
                    second = governor.evaluate(request, snap)
                self.assertEqual(os.listdir("."), files_before)
            finally:
                os.chdir(old_cwd)
        self.assertEqual(first, second)
        self.assertEqual(request, request_before)
        self.assertEqual(snap, snap_before)
        self.assertEqual(dict(os.environ), environment_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
