#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CataMind 隔离回环 HTTP 端到端测试，不读取或修改生产数据库与账号文件。"""
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


class TestLoopbackHttpE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        td = cls.tempdir.name
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.admin_token = "isolated-bootstrap-token"
        cls.admin_password = "Admin-password-123"
        cls.user_password = "Member-password-123"
        env = dict(os.environ)
        env.update({
            "CATMAT_DB_PATH": os.path.join(td, "catmat-e2e.db"),
            "CATMAT_BACKUP_DIR": os.path.join(td, "backups"),
            "CATMAT_LOG_DIR": os.path.join(td, "logs"),
            "CATMAT_TMP_DIR": os.path.join(td, "tmp"),
            "CATMAT_ACCOUNT_DATA_DIR": os.path.join(td, "accounts"),
            "CATMAT_AUTH_ENABLED": "1",
            "CATMAT_LOCAL_AUTH_ENABLED": "1",
            "CATMAT_WECHAT_CHALLENGE_ENABLED": "0",
            "CATMAT_WECHAT_TEST_MODE": "0",
            "CATMAT_JWT_SECRET": "isolated-e2e-jwt-secret-at-least-32-bytes",
            "CATMAT_ADMIN_TOKEN": cls.admin_token,
            "CATMAT_AII_PY": sys.executable,
            "CATMAT_ML_PY": sys.executable,
        })
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "catmat.py"), "serve",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                out, err = cls.proc.communicate(timeout=2)
                raise RuntimeError("隔离服务启动失败: %s" % (err or out).decode("utf-8", "replace")[-1000:])
            try:
                status, _, body = cls.request("GET", "/api/health")
                if status == 200 and body.get("ok"):
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("隔离服务健康检查超时")

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
    def request(cls, method, path, body=None, token=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(cls.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                raw = res.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                return res.status, dict(res.headers), payload
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            return exc.code, dict(exc.headers), payload

    def test_full_local_auth_and_role_flow(self):
        status, headers, health = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

        status, _, auth = self.request("GET", "/api/auth/status")
        self.assertEqual(status, 200)
        self.assertTrue(auth["data"]["enabled"])
        self.assertTrue(auth["data"]["channels"]["local"])
        self.assertFalse(auth["data"]["channels"]["wechat"])
        self.assertTrue(auth["data"]["accounts"]["bootstrap_required"])

        status, _, _ = self.request("POST", "/api/auth/local/bootstrap", {
            "admin_token": self.admin_token,
            "username": "admin-e2e",
            "password": self.admin_password,
            "display_name": "E2E Admin",
        })
        self.assertEqual(status, 200)

        status, _, login = self.request("POST", "/api/auth/local/login", {
            "username": "admin-e2e", "password": self.admin_password,
        })
        self.assertEqual(status, 200)
        admin_access = login["data"]["access_token"]

        status, _, created = self.request("POST", "/api/auth/users", {
            "username": "member-e2e",
            "password": self.user_password,
            "display_name": "E2E Member",
            "role": "user",
        }, admin_access)
        self.assertEqual(status, 200)
        member_id = created["data"]["id"]

        status, _, users = self.request("GET", "/api/auth/users", token=admin_access)
        self.assertEqual(status, 200)
        self.assertEqual(len(users["data"]), 2)
        self.assertTrue(all("password_hash" not in item for item in users["data"]))

        status, _, member_login = self.request("POST", "/api/auth/local/login", {
            "username": "member-e2e", "password": self.user_password,
        })
        self.assertEqual(status, 200)
        member_access = member_login["data"]["access_token"]

        status, _, _ = self.request("GET", "/api/systems", token=member_access)
        self.assertEqual(status, 200)
        status, _, denied = self.request("DELETE", "/api/systems/1", token=member_access)
        self.assertEqual(status, 403)
        self.assertEqual(denied["code"], "PERMISSION_DENIED")

        # ---- ML 数据集 CSV 表头自动识别与映射导入 ----
        csv_text = "material,work_function,eps_d,target\n" \
                   "Pt_111,5.6,-2.4,0.42\nNi_111,5.3,-1.42,0.51\n"
        status, _, preview = self.request("POST", "/api/ml/import", {
            "csv": csv_text, "dry_run": True,
        }, admin_access)
        self.assertEqual(status, 200)
        p = preview["data"]
        self.assertTrue(p["dry_run"])
        self.assertEqual(p["name_col"], "material")
        self.assertEqual(p["target_col"], "target")
        self.assertEqual(sorted(p["feature_cols"]), ["eps_d", "work_function"])
        self.assertEqual(p["row_count"], 2)
        status, _, imp = self.request("POST", "/api/ml/import", {
            "csv": csv_text, "mapping": {"name_col": "material",
                                         "target_col": "target",
                                         "feature_cols": ["work_function", "eps_d"]},
        }, admin_access)
        self.assertEqual(status, 200)
        self.assertEqual(imp["data"]["imported"], 2)
        # AL 导入：ε_d 特征列名任意（eps_d）也可自动识别
        al_csv = "体系,eps_d,eta_oer\nPt_111,-2.4,0.42\nFe_111,-1.2,\n"
        status, _, alimp = self.request("POST", "/api/al/import", {"csv": al_csv}, admin_access)
        self.assertEqual(status, 200)
        self.assertEqual(alimp["data"]["train"], 1)
        self.assertEqual(alimp["data"]["candidates"], 1)

        # ---- CM- 队列与任务归属（清客只能删改/下载自己的任务）----
        # 清客提交独立 CP2K → 入 CM- 队列（排队中，未真启动）
        status, _, queued = self.request("POST", "/api/cp2k-submit", {
            "name": "guest-cp2k", "inp": "&GLOBAL\nPROJECT demo\n&END GLOBAL",
            "nproc": 1, "omp_threads": 1,
        }, member_access)
        self.assertEqual(status, 200)
        self.assertTrue(queued["data"]["cm_code"].startswith("CM-"))
        queue_id = queued["data"]["id"]
        # 清客列表只见自己
        status, _, qlist = self.request("GET", "/api/queue", token=member_access)
        self.assertEqual(status, 200)
        self.assertEqual(len(qlist["data"]), 1)
        # 东翁可见全部
        status, _, qall = self.request("GET", "/api/queue", token=admin_access)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(qall["data"]), 1)
        # 清客撤销自己的排队项（移出队列）
        status, _, canceled = self.request("DELETE", "/api/queue/%d" % queue_id,
                                           token=member_access)
        self.assertEqual(status, 200)
        self.assertEqual(canceled["data"]["cm_code"], queued["data"]["cm_code"])
        status, _, qlist2 = self.request("GET", "/api/queue", token=member_access)
        self.assertEqual(status, 200)
        self.assertTrue(all(item["status"] != "queued" for item in qlist2["data"]))
        # 东翁新建一个任务；清客不能删除他人任务
        status, _, owner_task = self.request("POST", "/api/tasks", {
            "name": "owner-manual-task", "status": "queued",
        }, admin_access)
        self.assertEqual(status, 200)
        owner_task_id = owner_task["data"]["id"]
        status, _, owned_denied = self.request("DELETE", "/api/tasks/%d" % owner_task_id,
                                               token=member_access)
        self.assertEqual(status, 403)
        self.assertEqual(owned_denied["code"], "OWNERSHIP_DENIED")
        # 清客可以删除自己的任务
        status, _, own_task = self.request("POST", "/api/tasks", {
            "name": "guest-manual-task", "status": "queued",
        }, member_access)
        self.assertEqual(status, 200)
        status, _, _ = self.request("DELETE", "/api/tasks/%d" % own_task["data"]["id"],
                                    token=member_access)
        self.assertEqual(status, 200)

        status, _, _ = self.request("PATCH", "/api/auth/users/%d" % member_id, {
            "role": "steward", "disabled": False,
        }, admin_access)
        self.assertEqual(status, 200)
        status, _, _ = self.request("PATCH", "/api/auth/users/%d" % member_id, {
            "role": "guest", "disabled": True,
        }, admin_access)
        self.assertEqual(status, 200)

        account_dir = os.path.join(self.tempdir.name, "accounts")
        with open(os.path.join(account_dir, "admins.json"), encoding="utf-8") as f:
            admins = json.load(f)
        with open(os.path.join(account_dir, "users.json"), encoding="utf-8") as f:
            users = json.load(f)
        ids_admin = {item["id"] for item in admins}
        ids_user = {item["id"] for item in users}
        self.assertFalse(ids_admin & ids_user)
        self.assertEqual(len(ids_admin | ids_user), 2)
        self.assertTrue(all(not item.get("password_hash") or item["password_hash"].startswith("scrypt$")
                            for item in admins + users))
        self.assertEqual(oct(os.stat(account_dir).st_mode & 0o777), "0o700")
        self.assertEqual(oct(os.stat(os.path.join(account_dir, "admins.json")).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(os.path.join(account_dir, "users.json")).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(os.path.join(self.tempdir.name, "catmat-e2e.db")).st_mode & 0o777), "0o600")

        status, _, disabled = self.request("POST", "/api/auth/local/login", {
            "username": "member-e2e", "password": self.user_password,
        })
        self.assertEqual(status, 403)
        self.assertEqual(disabled["code"], "ACCOUNT_DISABLED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
