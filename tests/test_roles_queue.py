#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三角色迁移、解读归档标签、CM- 队列编号的隔离测试（不触碰生产数据）。"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server  # noqa: E402

server.AUTH_ENABLED = True
server.WECHAT_TEST_MODE = True
server.JWT_SECRET = "unit-test-jwt-secret"
server.ADMIN_TOKEN = "unit-admin-token"


class BaseIsolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        td = self.tmp.name
        self._old_db = server.DB_PATH
        self._old_dir = os.environ.get("CATMAT_ACCOUNT_DATA_DIR")
        server.DB_PATH = os.path.join(td, "iso.db")
        os.environ["CATMAT_ACCOUNT_DATA_DIR"] = os.path.join(td, "accounts")
        os.environ["CATMAT_BACKUP_DIR"] = os.path.join(td, "backups")
        os.environ["CATMAT_LOG_DIR"] = os.path.join(td, "logs")
        os.environ["CATMAT_TMP_DIR"] = os.path.join(td, "tmp")
        server.init_db()
        self.conn = sqlite3.connect(server.DB_PATH)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        server.DB_PATH = self._old_db
        if self._old_dir is None:
            os.environ.pop("CATMAT_ACCOUNT_DATA_DIR", None)
        else:
            os.environ["CATMAT_ACCOUNT_DATA_DIR"] = self._old_dir
        # 异步任务/独立连接可能残留 -wal/-shm 文件；清理失败不应影响其它用例
        try:
            self.tmp.cleanup()
        except (OSError, PermissionError):
            pass


class TestRoleMigration(BaseIsolated):
    def test_legacy_roles_migrate_to_single_owner(self):
        # 手工制造旧库角色（admin ×2 + user ×1，含系统令牌账号）
        c = self.conn
        now = server.now_str()
        for i, (name, role) in enumerate([("oldadmin1", "admin"), ("oldadmin2", "admin"),
                                          ("olduser1", "user")], start=1):
            c.execute(
                "INSERT INTO auth_users(openid,username,display_name,role,created_at,last_login_at) "
                "VALUES(?,?,?,?,?,?)",
                ("local:%s" % name, name, name, role, now, now))
        c.execute(
            "INSERT INTO auth_users(openid,display_name,role,created_at) VALUES(?,?,?,?)",
            (server.ADMIN_HOLDER_OPENID, "系统令牌", "admin", now))
        c.commit()
        # 再次执行 init_db 触发兼容迁移
        server.init_db()
        rows = {r["username"]: dict(r) for r in c.execute(
            "SELECT * FROM auth_users WHERE username IS NOT NULL")}
        self.assertEqual(rows["oldadmin1"]["role"], "owner")
        self.assertEqual(rows["oldadmin2"]["role"], "steward")   # 其余旧管理员降为掌书
        self.assertEqual(rows["olduser1"]["role"], "guest")
        owner_count = c.execute(
            "SELECT COUNT(*) c FROM auth_users WHERE role='owner' AND username IS NOT NULL"
        ).fetchone()["c"]
        self.assertEqual(owner_count, 1)
        token = c.execute("SELECT role FROM auth_users WHERE openid=?",
                          (server.ADMIN_HOLDER_OPENID,)).fetchone()
        # 系统令牌通道对标掌书（平台管理员），不占用东翁名额
        self.assertEqual(token["role"], "steward")
        counts = server._sync_account_role_files(c)
        self.assertEqual(counts["admin"], 3)  # owner ×1 + steward ×1 + token steward ×1
        self.assertEqual(counts["user"], 1)


class TestInterpretArchiveLabel(BaseIsolated):
    def test_interpret_archives_with_user_label(self):
        c = self.conn
        c.execute(
            "INSERT INTO literature(title,authors,journal,year,doi,abstract,conclusion,tags,verified) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("A Paper", json.dumps(["X"], ensure_ascii=False), "J", 2026, "10.1/x",
             "abstract text", "待解读归档", "[]", 0))
        lid = c.execute("SELECT last_insert_rowid() i").fetchone()["i"]
        fake_json = json.dumps({
            "material_system": "NiFe/NF", "electrolyzer": "AEM",
            "overpotential_10": 240.0, "tafel_slope": None, "stability_hours": None,
            "dft_code": "CP2K", "uses_cp2k": True,
            "conclusion": "结论结论。", "highlights": ["亮点一", "亮点二"],
            "tags": ["电催化"],
        }, ensure_ascii=False)
        # 必须同时 mock 大模型配置：否则在未配置 API Key 的环境（CI / 新机器）
        # interpret_literature_core 会在配置校验处提前返回 error，测试变成"看环境脸色"。
        fake_cfg = {"base_url": "https://example.invalid/v1", "model": "test-model",
                    "api_key": "test-key"}
        with mock.patch.object(server, "load_llm_config", return_value=fake_cfg), \
             mock.patch.object(server, "llm_call", return_value=(fake_json, None)):
            res = server.interpret_literature_core(c, lid, archived_by="qinger")
        self.assertNotIn("error", res, res.get("error"))
        self.assertTrue(res["archived"])
        self.assertEqual(res["archived_by"], "qinger")
        lit = c.execute("SELECT interpreted_by, interpret_full FROM literature WHERE id=?",
                        (lid,)).fetchone()
        self.assertEqual(lit["interpreted_by"], "qinger")
        kb = c.execute("SELECT archived_by, category FROM knowledge WHERE category='文献解读' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(kb["archived_by"], "qinger")
        self.assertTrue(kb["category"], "文献解读")


class TestCMQueue(BaseIsolated):
    def test_cm_code_monotonic_and_cancel_flow(self):
        c = self.conn
        q1, err = server.queue_enqueue(c, 1, "t1", "&GLOBAL\n&END GLOBAL",
                                       nproc=1, omp_threads=1)
        self.assertIsNone(err)
        q2, err = server.queue_enqueue(c, 2, "t2", "&GLOBAL\n&END GLOBAL",
                                       nproc=1, omp_threads=1)
        self.assertIsNone(err)
        self.assertTrue(q1["cm_code"].startswith("CM-"))
        self.assertNotEqual(q1["cm_code"], q2["cm_code"])
        self.assertEqual(q1["status"], "queued")
        # 撤销排队项 → canceled（移出调度）
        c.execute("UPDATE queue SET status='canceled', finished_at=? WHERE id=?",
                  (server.now_str(), q1["id"]))
        c.commit()
        queued = c.execute("SELECT COUNT(*) c FROM queue WHERE status='queued'").fetchone()["c"]
        self.assertEqual(queued, 1)
        self.assertEqual(q2["owner_id"], 2)

    def test_queue_entry_view_hides_inp(self):
        c = self.conn
        q, err = server.queue_enqueue(c, 1, "secret-task",
                                      "&GLOBAL\nPROJECT top-secret-content\n&END GLOBAL",
                                      nproc=1)
        self.assertIsNone(err)
        view = server._queue_entry_view(q)
        self.assertNotIn("secret-content", json.dumps(view, ensure_ascii=False))
        self.assertTrue(view["payload"]["has_inp"])
        self.assertEqual(view["status_label"], "排队中")


class TestTaskOwnership(BaseIsolated):
    @mock.patch.object(server, "run_workflow_node")
    def test_create_workflow_records_owner(self, _run_node):
        c = self.conn
        owner, _, _ = server._create_local_user(
            c, "boss", "Boss-password-123", "博士", "owner")
        wf, err = server.create_workflow(
            c, {"name": "wf-1", "calc_type": "GEO_OPT"}, owner_id=owner["id"])
        self.assertIsNone(err)
        row = c.execute("SELECT owner_id FROM workflows WHERE id=?",
                        (wf["workflow_id"],)).fetchone()
        self.assertEqual(row["owner_id"], owner["id"])
        # AI/系统触发缺省归本地东翁（唯一 owner）
        wf2, err = server.create_workflow(c, {"name": "wf-2"}, owner_id=None)
        self.assertIsNone(err)
        row2 = c.execute("SELECT owner_id FROM workflows WHERE id=?",
                         (wf2["workflow_id"],)).fetchone()
        self.assertEqual(row2["owner_id"], owner["id"])

    def test_start_task_records_owner(self):
        c = self.conn
        owner, _, _ = server._create_local_user(
            c, "boss2", "Boss-password-123", "博士", "owner")
        c.commit()   # 让独立线程连接可写
        old_py = server.ML_PY
        server.ML_PY = "/bin/true"   # 轻量脚本，仅验证落库与子进程记录
        try:
            tid, err = server._start_task("ml", "t", "rf", "auto", {}, owner_id=owner["id"])
        finally:
            server.ML_PY = old_py
        self.assertIsNone(err)
        row = c.execute("SELECT owner_id, pid FROM ml_tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["owner_id"], owner["id"])
        try:
            if row["pid"]:
                import os
                os.kill(row["pid"], 15)
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
