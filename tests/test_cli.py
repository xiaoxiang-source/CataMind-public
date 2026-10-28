#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CataMind 统一命令入口的参数兼容性测试。"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import catmat  # noqa: E402


# CLI 对外承诺的命令集。新增/删除功能时**同步维护本清单**——
# 此前这里写的是 `assertEqual(len(COMMANDS), 15)`，新增 pdos-site-softness 后
# 报错信息只有 "16 != 15"，既看不出多了什么、也容易让人直接改成数字了事。
EXPECTED_COMMANDS = {
    "postprocess-tools",
    "aiida-node", "aiida-probe", "aiida-task-files", "al-train", "bader-charge",
    "build-param-db", "build-param-refs", "chart-gen", "load-structure",
    "load-workflow-results", "ml-train", "pdos-site-softness", "postprocess-pdos",
    "store-structures", "submit-catmat-workflow", "submit-cp2k-verdi",
}


class TestUnifiedCli(unittest.TestCase):
    def test_all_commands_point_to_existing_scripts(self):
        missing = EXPECTED_COMMANDS - set(catmat.COMMANDS)
        unexpected = set(catmat.COMMANDS) - EXPECTED_COMMANDS
        self.assertFalse(missing, "CLI 缺少已承诺的命令：%s" % sorted(missing))
        self.assertFalse(unexpected, "CLI 新增了未登记的导出命令（请在 EXPECTED_COMMANDS 中登记）：%s"
                         % sorted(unexpected))
        for command, script in catmat.COMMANDS.items():
            with self.subTest(command=command):
                self.assertTrue(os.path.isfile(os.path.join(catmat.TOOLS_DIR, script)))

    def test_setup_is_colocated_with_tools(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "setup.py")))
        self.assertTrue(os.path.isfile(os.path.join(catmat.TOOLS_DIR, "setup.py")))

    def test_subcommand_forwards_arguments_without_reordering(self):
        original = list(sys.argv)
        try:
            for command, script in catmat.COMMANDS.items():
                with self.subTest(command=command):
                    sys.argv = [os.path.join(ROOT, "catmat.py"), command,
                                "alpha", "--flag", "value", "two words"]
                    with mock.patch.object(catmat.runpy, "run_path") as runner:
                        rc = catmat.main()
                    self.assertEqual(rc, 0)
                    expected_path = os.path.join(catmat.TOOLS_DIR, script)
                    runner.assert_called_once_with(expected_path, run_name="__main__")
                    self.assertEqual(sys.argv,
                                     [expected_path, "alpha", "--flag", "value", "two words"])
        finally:
            sys.argv = original

    def test_unknown_command_returns_usage_error(self):
        original = list(sys.argv)
        try:
            sys.argv = [os.path.join(ROOT, "catmat.py"), "not-a-command"]
            self.assertEqual(catmat.main(), 2)
        finally:
            sys.argv = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
