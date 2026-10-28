#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资源默认值推导与物理核探测的隔离测试。"""
import io
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server  # noqa: E402


class TestResourceDefaults(unittest.TestCase):
    def setUp(self):
        self._physical_core_count = server._PHYSICAL_CORE_COUNT
        self._physical_core_count_reliable = server._PHYSICAL_CORE_COUNT_RELIABLE

    def tearDown(self):
        server._PHYSICAL_CORE_COUNT = self._physical_core_count
        server._PHYSICAL_CORE_COUNT_RELIABLE = self._physical_core_count_reliable

    def test_reliable_resource_defaults(self):
        cases = [
            (192, (32, 96)),
            (384, (32, 192)),
            (24, (4, 12)),
            (6, (1, 3)),
            (1, (1, 0)),
        ]
        for physical_cores, expected in cases:
            with self.subTest(physical_cores=physical_cores):
                self.assertEqual(
                    server._derive_resource_defaults(physical_cores, True), expected)

    def test_non_positive_core_counts_are_treated_as_one(self):
        # 防御规则先将 0/-4 规范化为 1，所以队列默认值为 1 // 2 == 0。
        self.assertEqual(server._derive_resource_defaults(0, True), (1, 0))
        self.assertEqual(server._derive_resource_defaults(-4, True), (1, 0))

    def test_unreliable_detection_uses_conservative_defaults(self):
        self.assertEqual(server._derive_resource_defaults(192, False), (8, 16))

    def test_derivation_is_pure(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            environment_before = dict(os.environ)
            with mock.patch("builtins.open", side_effect=AssertionError("unexpected file access")):
                self.assertEqual(server._derive_resource_defaults(192, True), (32, 96))
            self.assertEqual(dict(os.environ), environment_before)

    def test_physical_core_count_on_this_host(self):
        server._PHYSICAL_CORE_COUNT = None
        server._PHYSICAL_CORE_COUNT_RELIABLE = False
        count = server._physical_core_count()
        if not server._PHYSICAL_CORE_COUNT_RELIABLE:
            print("SKIP: /proc/cpuinfo 拓扑字段缺失，无法可靠核验本机物理核数")
            self.skipTest("本机 /proc/cpuinfo 拓扑字段缺失")
        # 不绑定具体核数：原断言写死 192，只对作者服务器成立，通用 CI runner 必然失败。
        # 改为校验探测结果的合理性——为正整数，且不超过逻辑核数（SMT 下物理核 ≤ 逻辑核）。
        logical = os.cpu_count() or count
        self.assertGreaterEqual(count, 1)
        self.assertLessEqual(count, logical)
        self.assertIs(server._PHYSICAL_CORE_COUNT_RELIABLE, True)

    def test_missing_cpuinfo_falls_back_without_error(self):
        server._PHYSICAL_CORE_COUNT = None
        server._PHYSICAL_CORE_COUNT_RELIABLE = True
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            count = server._physical_core_count()
        self.assertGreaterEqual(count, 1)
        self.assertIs(server._PHYSICAL_CORE_COUNT_RELIABLE, False)

    def test_missing_core_id_does_not_count_processor_records(self):
        cpuinfo = """processor : 0
physical id : 0

processor : 1
physical id : 0

"""
        server._PHYSICAL_CORE_COUNT = None
        server._PHYSICAL_CORE_COUNT_RELIABLE = True
        with mock.patch("builtins.open", return_value=io.StringIO(cpuinfo)), \
             mock.patch.object(server.os, "cpu_count", return_value=12):
            count = server._physical_core_count()
        self.assertEqual(count, 12)
        self.assertNotEqual(count, 2)
        self.assertIs(server._PHYSICAL_CORE_COUNT_RELIABLE, False)

    def test_env_int_override_semantics(self):
        key = "CATMAT_QUEUE_MAX_TOTAL_NPROC"
        for raw, expected in (("0", 0), ("144", 144), ("abc", 16)):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {key: raw}, clear=True):
                self.assertEqual(server._env_int(key, 16), expected)


if __name__ == "__main__":
    unittest.main()
