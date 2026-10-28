#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用写请求审计的真实性、事务边界与降级行为。"""
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server


class TestAuditTruthfulnessHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        td = cls.tempdir.name
        try:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                cls.port = sock.getsockname()[1]
        except PermissionError as exc:
            cls.tempdir.cleanup()
            raise unittest.SkipTest("沙箱不允许回环 socket: %s" % exc)

        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.db_path = os.path.join(td, "catmat-audit-e2e.db")
        cls.admin_token = "audit-isolated-bootstrap-token"
        cls.password = "Audit-password-123"
        env = dict(os.environ)
        env.update({
            "CATMAT_DB_PATH": cls.db_path,
            "CATMAT_BACKUP_DIR": os.path.join(td, "backups"),
            "CATMAT_LOG_DIR": os.path.join(td, "logs"),
            "CATMAT_TMP_DIR": os.path.join(td, "tmp"),
            "CATMAT_ACCOUNT_DATA_DIR": os.path.join(td, "accounts"),
            "CATMAT_DATASETS_DIR": os.path.join(td, "datasets"),
            "CATMAT_AUTH_ENABLED": "1",
            "CATMAT_LOCAL_AUTH_ENABLED": "1",
            "CATMAT_WECHAT_CHALLENGE_ENABLED": "0",
            "CATMAT_WECHAT_TEST_MODE": "0",
            "CATMAT_JWT_SECRET": "audit-e2e-jwt-secret-at-least-32-bytes",
            "CATMAT_ADMIN_TOKEN": cls.admin_token,
            "CATMAT_AII_PY": sys.executable,
            "CATMAT_ML_PY": sys.executable,
        })
        try:
            cls.proc = subprocess.Popen(
                [sys.executable, os.path.join(ROOT, "catmat.py"), "serve",
                 "--host", "127.0.0.1", "--port", str(cls.port)],
                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except PermissionError as exc:
            cls.tempdir.cleanup()
            raise unittest.SkipTest("沙箱不允许启动回环服务: %s" % exc)

        deadline = time.time() + 20
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                out, err = cls.proc.communicate(timeout=2)
                detail = (err or out).decode("utf-8", "replace")[-1200:]
                cls.tempdir.cleanup()
                if "PermissionError" in detail or "Operation not permitted" in detail:
                    raise unittest.SkipTest("沙箱不允许回环服务: %s" % detail)
                raise RuntimeError("隔离服务启动失败: %s" % detail)
            try:
                status, _, payload = cls.request("GET", "/api/health")
                if status == 200 and payload.get("ok"):
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        else:
            cls.proc.terminate()
            cls.proc.wait(timeout=5)
            cls.tempdir.cleanup()
            raise RuntimeError("隔离服务健康检查超时")

        status, _, bootstrap = cls.request("POST", "/api/auth/local/bootstrap", {
            "admin_token": cls.admin_token,
            "username": "audit-owner",
            "password": cls.password,
            "display_name": "Audit Owner",
        })
        if status != 200:
            raise RuntimeError("隔离账号初始化失败: %r" % bootstrap)
        cls.user_id = bootstrap["data"]["id"]
        status, _, login = cls.request("POST", "/api/auth/local/login", {
            "username": "audit-owner", "password": cls.password,
        })
        if status != 200:
            raise RuntimeError("隔离账号登录失败: %r" % login)
        cls.token = login["data"]["access_token"]

    @classmethod
    def tearDownClass(cls):
        proc = getattr(cls, "proc", None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        tempdir = getattr(cls, "tempdir", None)
        if tempdir is not None:
            tempdir.cleanup()

    @classmethod
    def request(cls, method, path, body=None, token=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(cls.base + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                raw = response.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                return response.status, dict(response.headers), payload
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            return exc.code, dict(exc.headers), payload

    @classmethod
    def audit_rows(cls, action, target=None):
        conn = sqlite3.connect(cls.db_path)
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM audit WHERE action=?"
        args = [action]
        if target is not None:
            sql += " AND target=?"
            args.append(target)
        rows = conn.execute(sql + " ORDER BY id", args).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def wait_for_audit_rows(self, action, target, min_count,
                            timeout=3.0, interval=0.02):
        deadline = time.monotonic() + timeout
        while True:
            rows = self.audit_rows(action, target)
            if len(rows) >= min_count:
                return rows
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail("审计行等待超时: action=%s target=%s expected>=%d actual=%d"
                          % (action, target, min_count, len(rows)))
            time.sleep(min(interval, remaining))

    def test_real_http_results_actor_and_coverage(self):
        paired = []

        failed_before = len(self.audit_rows("post_ml", "/ml/import"))
        status, _, failed = self.request(
            "POST", "/api/ml/import", {}, self.token)
        self.assertEqual(status, 400)
        self.assertEqual(failed["code"], "INPUT_ERROR")
        failed_row = self.wait_for_audit_rows(
            "post_ml", "/ml/import", failed_before + 1)[-1]
        paired.append((status, failed_row))
        self.assertEqual(failed_row["ok"], 0)
        self.assertEqual(failed_row["actor"], "uid:%s" % self.user_id)
        self.assertIn("http=400", failed_row["result"])
        self.assertIn("code=INPUT_ERROR", failed_row["result"])
        self.assertIn("elapsed=", failed_row["result"])

        csv_text = "material,feature,target\nPt,1.0,0.2\nRu,2.0,0.3\n"
        success_before = len(self.audit_rows("post_ml", "/ml/import"))
        status, _, succeeded = self.request("POST", "/api/ml/import", {
            "csv": csv_text,
            "dataset_name": "audit-e2e",
            "mapping": {"name_col": "material", "target_col": "target",
                        "feature_cols": ["feature"]},
        }, self.token)
        self.assertEqual(status, 200)
        self.assertTrue(succeeded["ok"])
        success_row = self.wait_for_audit_rows(
            "post_ml", "/ml/import", success_before + 1)[-1]
        paired.append((status, success_row))
        self.assertEqual(success_row["ok"], 1)
        self.assertEqual(success_row["actor"], "uid:%s" % self.user_id)
        self.assertIn("http=200", success_row["result"])
        self.assertIn("elapsed=", success_row["result"])

        before = len(self.audit_rows("post_systems", "/systems"))
        status, _, unauthorized = self.request("POST", "/api/systems", {})
        self.assertEqual(status, 401)
        self.assertEqual(unauthorized["code"], "UNAUTHORIZED")
        after = len(self.audit_rows("post_systems", "/systems"))
        self.assertEqual(after, before)

        status, _, metrics = self.request("GET", "/api/metrics", token=self.token)
        self.assertEqual(status, 200)
        fail_count = metrics["data"]["audit"]["fail_count"]
        self.assertIsInstance(fail_count, int)
        self.assertGreaterEqual(fail_count, 0)

        for response_status, row in paired:
            self.assertEqual(row["ok"], int(response_status < 400),
                             "HTTP>=400 不得对应 ok=1")


class _FailingAuditConnection:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("INSERT INTO AUDIT"):
            raise sqlite3.OperationalError("mock audit insert failure")
        return self.conn.execute(sql, params)

    def commit(self):
        return self.conn.commit()

    def rollback(self):
        return self.conn.rollback()


class _MetricsCapture:
    def _ok(self, data=None, **kw):
        self.payload = {"ok": True, "data": data}
        self.payload.update(kw)
        return self.payload


class TestAuditTruthfulnessTransactions(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "audit-unit.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(server.SCHEMA)
        conn.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_unexpected_exception_rolls_back_business_but_commits_audit(self):
        handler = object.__new__(server.Handler)
        handler.path = "/api/systems"
        handler.headers = {}
        handler.rfile = io.BytesIO(b"{}")

        def exploding_systems(_handler, method, seg, qs, conn, body):
            conn.execute("INSERT INTO systems(name) VALUES(?)", ("must-rollback",))
            raise RuntimeError("mock business failure")

        with mock.patch.object(server, "DB_PATH", self.db_path), \
                mock.patch.object(server, "AUTH_ENABLED", False), \
                mock.patch.object(server.Handler, "_systems", exploding_systems):
            with self.assertRaisesRegex(RuntimeError, "mock business failure"):
                handler._route("POST")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM systems WHERE name='must-rollback'").fetchone()[0], 0)
        row = conn.execute(
            "SELECT * FROM audit WHERE action='post_systems' AND target='/systems' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["ok"], 0)
        self.assertEqual(row["actor"], "system")
        self.assertIn("http=500", row["result"])
        self.assertIn("code=INTERNAL_ERROR", row["result"])

    def test_audit_failure_is_counted_and_visible_in_metrics(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("INSERT INTO systems(name) VALUES(?)", ("business-survives",))
        before = server._audit_fail_count()
        result = server.audit(_FailingAuditConnection(conn), "forced_failure",
                              commit=False)
        self.assertFalse(result)
        self.assertEqual(server._audit_fail_count(), before + 1)
        conn.commit()
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM systems WHERE name='business-survives'").fetchone()[0], 1)

        capture = _MetricsCapture()
        with mock.patch.object(server, "host_metrics", return_value={}):
            server.Handler._dispatch(capture, "GET", ["metrics"], {}, conn, {})
        self.assertEqual(capture.payload["data"]["audit"]["fail_count"], before + 1)
        conn.close()

    def test_audit_default_still_commits_immediately(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO systems(name) VALUES(?)", ("committed-with-audit",))
        self.assertTrue(server.audit(conn, "compat_default"))

        observer = sqlite3.connect(self.db_path)
        self.assertEqual(observer.execute(
            "SELECT COUNT(*) FROM systems WHERE name='committed-with-audit'").fetchone()[0], 1)
        self.assertEqual(observer.execute(
            "SELECT COUNT(*) FROM audit WHERE action='compat_default'").fetchone()[0], 1)
        observer.close()
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
