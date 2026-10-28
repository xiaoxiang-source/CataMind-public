#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全局锁拆分测试：静态覆盖、锁语义与隔离 HTTP 延迟。"""
import ast
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PATH = os.path.join(ROOT, "server.py")
sys.path.insert(0, ROOT)

import server  # noqa: E402


class TestSubprocessCoverage(unittest.TestCase):
    def test_all_subprocess_calls_are_wrapped_or_explicitly_exempt(self):
        with open(SERVER_PATH, encoding="utf-8") as f:
            tree = ast.parse(f.read(), SERVER_PATH)
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        subprocess_methods = {"run", "Popen", "check_output", "check_call", "call"}
        exempt_functions = {
            "submit_cp2k_direct", "submit_verdi_calc", "submit_catmat_workchain",
        }
        wrapped = []
        exempt = []
        exceptions = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr in subprocess_methods
            ):
                continue
            function_name = "<module>"
            inside_unlocked = False
            ancestor = node
            while ancestor in parents:
                ancestor = parents[ancestor]
                if isinstance(ancestor, ast.With):
                    for item in ancestor.items:
                        expr = item.context_expr
                        if (isinstance(expr, ast.Call)
                                and isinstance(expr.func, ast.Name)
                                and expr.func.id == "_blocking_unlocked"):
                            inside_unlocked = True
                if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    function_name = ancestor.name
                    break
            item = "%s:%d subprocess.%s" % (
                function_name, node.lineno, node.func.attr)
            if inside_unlocked:
                wrapped.append(item)
            elif function_name in exempt_functions:
                exempt.append(item)
            else:
                exceptions.append(item)

        print("AST subprocess 覆盖：已包裹 %d 处 / 豁免 %d 处 / 例外 %d 处"
              % (len(wrapped), len(exempt), len(exceptions)))
        if exceptions:
            print("AST 例外清单：\n" + "\n".join(exceptions))
        self.assertEqual(exceptions, [])
        self.assertEqual(len(exempt), 3, exempt)
        self.assertEqual({item.split(":", 1)[0] for item in exempt}, exempt_functions)


class TestBlockingUnlockedSemantics(unittest.TestCase):
    def _acquire_in_other_thread(self, timeout=1.0):
        result = []

        def contender():
            acquired = server._lock.acquire(timeout=timeout)
            result.append(acquired)
            if acquired:
                server._lock.release()

        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout + 1.0)
        self.assertFalse(thread.is_alive())
        return result[0]

    def test_held_lock_is_released_then_reacquired(self):
        server._lock_acquire()
        try:
            with server._blocking_unlocked():
                self.assertTrue(self._acquire_in_other_thread(timeout=1.0))
            self.assertTrue(server._tls_lock.held)
            self.assertFalse(self._acquire_in_other_thread(timeout=1.0))
        finally:
            server._lock_release()

    def test_exception_propagates_and_lock_is_reacquired(self):
        server._lock_acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with server._blocking_unlocked():
                    raise RuntimeError("boom")
            self.assertTrue(server._tls_lock.held)
            self.assertFalse(self._acquire_in_other_thread(timeout=1.0))
        finally:
            server._lock_release()

    def test_unheld_lock_is_a_noop(self):
        server._tls_lock.held = False
        with server._blocking_unlocked():
            self.assertFalse(server._tls_lock.held)
        self.assertFalse(server._tls_lock.held)
        self.assertTrue(server._lock.acquire(timeout=1.0))
        server._lock.release()

    def test_nested_context_is_safe_with_non_reentrant_lock(self):
        server._lock_acquire()
        try:
            with server._blocking_unlocked():
                self.assertFalse(server._tls_lock.held)
                with server._blocking_unlocked():
                    self.assertFalse(server._tls_lock.held)
            self.assertTrue(server._tls_lock.held)
        finally:
            server._lock_release()


class _TestHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class TestFastPathsDuringBlockingCall(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        td = cls.tempdir.name
        cls.env_patch = mock.patch.dict(os.environ, {
            "CATMAT_DB_PATH": os.path.join(td, "catmat-lock-test.db"),
            "CATMAT_BACKUP_DIR": os.path.join(td, "backups"),
            "CATMAT_LOG_DIR": os.path.join(td, "logs"),
            "CATMAT_TMP_DIR": os.path.join(td, "tmp"),
            "CATMAT_ACCOUNT_DATA_DIR": os.path.join(td, "accounts"),
            "CATMAT_DATASETS_DIR": os.path.join(td, "datasets"),
        })
        cls.env_patch.start()
        cls.saved = {
            name: getattr(server, name) for name in (
                "DB_PATH", "BACKUP_DIR", "LOG_DIR", "LOG_FILE", "TMP_DIR",
                "DATASETS_DIR", "AUTH_ENABLED", "LOCAL_AUTH_ENABLED", "JWT_SECRET",
            )
        }
        server.DB_PATH = os.environ["CATMAT_DB_PATH"]
        server.BACKUP_DIR = os.environ["CATMAT_BACKUP_DIR"]
        server.LOG_DIR = os.environ["CATMAT_LOG_DIR"]
        server.LOG_FILE = os.path.join(server.LOG_DIR, "server.log")
        server.TMP_DIR = os.environ["CATMAT_TMP_DIR"]
        server.DATASETS_DIR = os.environ["CATMAT_DATASETS_DIR"]
        server.AUTH_ENABLED = True
        server.LOCAL_AUTH_ENABLED = True
        server.JWT_SECRET = "isolated-lock-test-secret-at-least-32-bytes"
        for path in (server.BACKUP_DIR, server.LOG_DIR, server.TMP_DIR,
                     server.DATASETS_DIR, os.environ["CATMAT_ACCOUNT_DATA_DIR"]):
            os.makedirs(path, exist_ok=True)
        server.init_db()
        conn = server.get_db()
        try:
            user, error, _ = server._create_local_user(
                conn, "locktest", "Lock-test-password-123", "Lock Test", "owner")
            if error:
                raise RuntimeError(error)
            conn.commit()
        finally:
            conn.close()
        server._RATE_BUCKETS.clear()

        try:
            cls.httpd = _TestHTTPServer(("127.0.0.1", 0), server.Handler)
        except (OSError, PermissionError) as exc:
            cls._restore()
            raise unittest.SkipTest("沙箱不允许创建回环 socket: %s" % exc)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        status, login = cls.request("POST", "/api/auth/local/login", {
            "username": "locktest", "password": "Lock-test-password-123",
        })
        if status != 200:
            raise RuntimeError("隔离登录失败: %r" % login)
        cls.token = login["data"]["access_token"]

    @classmethod
    def _restore(cls):
        if hasattr(cls, "saved"):
            for name, value in cls.saved.items():
                setattr(server, name, value)
        if hasattr(cls, "env_patch"):
            cls.env_patch.stop()
        if hasattr(cls, "tempdir"):
            cls.tempdir.cleanup()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "httpd"):
            cls.httpd.shutdown()
            cls.httpd.server_close()
        if hasattr(cls, "server_thread"):
            cls.server_thread.join(timeout=2)
        cls._restore()

    @classmethod
    def request(cls, method, path, body=None, token=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(
            cls.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                payload = (json.loads(raw.decode("utf-8"))
                           if "application/json" in content_type else raw)
                return response.status, payload
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw.decode("utf-8")) if raw else {}

    def test_health_tasks_login_and_static_remain_fast(self):
        entered = threading.Event()
        real_run = subprocess.run

        def fake_run(args, *pargs, **kwargs):
            if args and args[0] == "nvidia-smi":
                entered.set()
                time.sleep(5)
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real_run(args, *pargs, **kwargs)

        server._METRICS_CACHE.update({"ts": 0.0, "data": None})
        server._GPU_CACHE.update({"ts": 0.0, "data": None})
        long_result = []

        def long_request():
            long_result.append(self.request("GET", "/api/metrics", token=self.token))

        with mock.patch.object(server.subprocess, "run", side_effect=fake_run):
            thread = threading.Thread(target=long_request)
            thread.start()
            self.assertTrue(entered.wait(2), "假子进程未进入 5 秒阻塞段")
            probes = [
                ("GET", "/api/health", None, None),
                ("GET", "/api/tasks", None, self.token),
                ("POST", "/api/auth/local/login",
                 {"username": "locktest", "password": "Lock-test-password-123"}, None),
                ("GET", "/index.html", None, None),
            ]
            timings = []
            for method, path, body, token in probes:
                started = time.monotonic()
                status, _ = self.request(method, path, body, token)
                elapsed = time.monotonic() - started
                timings.append((path, elapsed))
                self.assertEqual(status, 200, path)
                self.assertLess(elapsed, 1.0, "%s took %.3fs" % (path, elapsed))
            thread.join(timeout=7)
            self.assertFalse(thread.is_alive())
        self.assertEqual(long_result[0][0], 200)
        print("E2E 快路径延迟：" + ", ".join(
            "%s=%.3fs" % item for item in timings))


if __name__ == "__main__":
    unittest.main(verbosity=2)
