#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""势函数延拓（DPGEN）API 集成测试：隔离服务端 + MOCK 模式。

MOCK 模式（CATMAT_DPGEN_MOCK=1）下，编排线程的 train/explore 返回样本数据，
标注阶段仅返回模拟结果，不允许进入真实 CP2K 队列。鉴权在生产中启用，本测试以 CATMAT_AUTH_ENABLED=0 简化（与平台支持一致）。
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestDpgenApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        td = cls.tempdir.name
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        env = dict(os.environ)
        env.update({
            "CATMAT_DB_PATH": os.path.join(td, "catmat-dpgen.db"),
            "CATMAT_BACKUP_DIR": os.path.join(td, "backups"),
            "CATMAT_LOG_DIR": os.path.join(td, "logs"),
            "CATMAT_TMP_DIR": os.path.join(td, "tmp"),
            "CATMAT_ACCOUNT_DATA_DIR": os.path.join(td, "accounts"),
            "CATMAT_AUTH_ENABLED": "0",
            "CATMAT_DPGEN_MOCK": "1",
            "CATMAT_AII_PY": sys.executable,
            "CATMAT_ML_PY": sys.executable,
        })
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "catmat.py"), "serve",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 25
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                out, err = cls.proc.communicate(timeout=2)
                raise RuntimeError("DPGEN 隔离服务启动失败: %s" % (err or out).decode("utf-8", "replace")[-1500:])
            try:
                st, _ = cls.request("GET", "/api/health")
                if st == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            try:
                out, err = cls.proc.communicate(timeout=2)
                diag = (err or out).decode("utf-8", "replace")[-2000:]
            except Exception as exc:
                diag = "（无法读取子进程输出: %s）" % exc
                try:
                    cls.proc.terminate()
                except Exception:
                    pass
            raise RuntimeError("DPGEN 隔离服务健康检查超时。子进程输出:\n%s" % diag)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "proc", None):
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=5)
        if getattr(cls, "tempdir", None):
            cls.tempdir.cleanup()

    @classmethod
    def request(cls, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(cls.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                raw = res.read()
                body = json.loads(raw.decode("utf-8")) if raw else {}
                # 平台统一信封 {"ok": True, "data": ...}；解包 data 便于断言
                return res.status, body.get("data", body)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            body = json.loads(raw.decode("utf-8")) if raw else {}
            return exc.code, body.get("data", body)

    def _wait_done(self, run_id, timeout=40):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, data = self.request("GET", "/api/dpgen/%d" % run_id)
            if st == 200 and data.get("status") in ("done", "failed", "cancelled"):
                return data
            time.sleep(0.3)
        return None

    def test_01_create_and_run_loop(self):
        cfg = {
            "run_name": "DPGEN测试延拓",
            "seed": "/tmp/dummy-seed",
            "type_map": ["Ir", "Ru"],
            "max_iter": 3,
            "trust_lo": 0.05, "trust_hi": 0.15, "stride": 1,
            "cp2k_nproc": 8,
            "converge": {"rmse_e_max": 0.001, "rmse_f_max": 0.001, "min_iters": 2, "patience": 3},
        }
        st, data = self.request("POST", "/api/dpgen", {"name": cfg["run_name"], "config": cfg})
        self.assertEqual(st, 200, data)
        self.assertIn("run_id", data)
        run_id = data["run_id"]
        self.assertEqual(data["status"], "running")

        final = self._wait_done(run_id, timeout=40)
        self.assertIsNotNone(final, "任务未在超时内结束")
        self.assertEqual(final["status"], "done", final.get("error"))
        self.assertEqual(final["current_iter"], 3)

        # 迭代明细存在；模拟标注绝不创建真实 CP2K 队列
        st, detail = self.request("GET", "/api/dpgen/%d" % run_id)
        self.assertEqual(st, 200)
        iters = detail.get("iterations") or []
        self.assertEqual(len(iters), 3)
        queue_ids = [q for it in iters for q in (it.get("cp2k_queue_ids") or [])]
        self.assertEqual(queue_ids, [], "模拟标注不得创建真实计算")
        self.assertTrue(final["result"]["simulation"])
        # 每轮应解析到训练误差指标
        self.assertTrue(any((it.get("metrics") or {}).get("rmse_e_train") for it in iters))

    def test_02_list_and_iterations(self):
        st, data = self.request("GET", "/api/dpgen")
        self.assertEqual(st, 200)
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 1)
        run = data[0]
        st, iters = self.request("GET", "/api/dpgen/%d/iterations" % run["id"])
        self.assertEqual(st, 200)
        self.assertIsInstance(iters, list)

    def test_03_assess(self):
        st, data = self.request("GET", "/api/dpgen")
        run_id = data[0]["id"]
        st, res = self.request("POST", "/api/dpgen/%d/assess" % run_id, {})
        self.assertEqual(st, 200)
        self.assertIn("converged", res)
        self.assertIn("reasons", res)

    def test_04_export_csv(self):
        st, data = self.request("GET", "/api/dpgen")
        run_id = data[0]["id"]
        # 直接打 export 端点（JSON 形态验证，CSV 二进制另测）
        st, res = self.request("POST", "/api/dpgen/%d/export?format=json" % run_id)
        self.assertEqual(st, 200)
        self.assertIn("iterations", res)

    def test_05_invalid_config_rejected(self):
        st, data = self.request("POST", "/api/dpgen",
                                {"name": "bad", "config": {"run_name": "bad", "seed": "x", "max_iter": 99}})
        self.assertEqual(st, 400)
        self.assertIn("error", data)

    def test_06_get_missing_404(self):
        st, _ = self.request("GET", "/api/dpgen/999999")
        self.assertEqual(st, 404)

    def test_07_delete_after_done(self):
        st, data = self.request("GET", "/api/dpgen")
        run_id = data[0]["id"]
        st, res = self.request("DELETE", "/api/dpgen/%d" % run_id)
        self.assertEqual(st, 200, res)
        st, _ = self.request("GET", "/api/dpgen/%d" % run_id)
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
