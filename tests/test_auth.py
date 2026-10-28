#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CataMind 本地账号、会话及兼容微信鉴权测试（标准库 unittest，零第三方依赖）。"""
import os
import sys
import sqlite3
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

# 单元测试直接覆盖模块配置，避免依赖执行测试命令时的外部环境变量。
server.AUTH_ENABLED = True
server.WECHAT_TEST_MODE = True
server.JWT_SECRET = "unit-test-jwt-secret"
server.WECHAT_ADMIN_OPENIDS = {"test_openid_admin"}
server.ADMIN_TOKEN = "unit-admin-token"


AUTH_SCHEMA = """
CREATE TABLE auth_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  openid TEXT NOT NULL UNIQUE,
  unionid TEXT,
  username TEXT UNIQUE,
  password_hash TEXT,
  password_updated_at TEXT,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'guest',
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT,
  last_login_at TEXT,
  capabilities TEXT DEFAULT '[]'
);
CREATE TABLE auth_sessions (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  refresh_token_hash TEXT NOT NULL UNIQUE,
  expires_at INTEGER NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0,
  created_at TEXT,
  last_used_at TEXT,
  scope TEXT DEFAULT 'platform'
);
"""


class TestLocalAuth(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(AUTH_SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_password_hash_is_salted_and_verifies(self):
        first = server._hash_password("A-strong-password-123")
        second = server._hash_password("A-strong-password-123")
        self.assertNotEqual(first, second)
        self.assertNotIn("A-strong-password-123", first)
        self.assertTrue(server._verify_password("A-strong-password-123", first))
        self.assertFalse(server._verify_password("wrong-password", first))

    def test_local_username_validation(self):
        username, error = server._validate_local_credentials("Alice.Admin", "0123456789")
        self.assertEqual(username, "alice.admin")
        self.assertIsNone(error)
        _, error = server._validate_local_credentials("a", "0123456789")
        self.assertIsNotNone(error)
        _, error = server._validate_local_credentials("alice", "short")
        self.assertIsNotNone(error)

    def test_create_local_user_and_public_view(self):
        # 首个本地账号 role=admin 归一为 owner（东翁）
        user, error, code = server._create_local_user(
            self.conn, "Alice", "A-strong-password-123", "Alice Admin", "admin")
        self.assertIsNone(error)
        self.assertEqual(user["openid"], "local:alice")
        self.assertEqual(user["role"], "owner")
        public = server._public_account(user)
        self.assertEqual(public["username"], "alice")
        self.assertEqual(public["role_label"], "东翁")
        self.assertNotIn("password_hash", public)
        # 东翁唯一：再创建一个 admin 应被拒绝
        _, error, code = server._create_local_user(
            self.conn, "Second", "Another-password-456", "Second Owner", "admin")
        self.assertIsNotNone(error)
        self.assertEqual(code, "OWNER_EXISTS")
        # 普通用户（清客）
        guest, error, code = server._create_local_user(
            self.conn, "Bob", "Another-password-456", "Bob Guest", "user")
        self.assertIsNone(error)
        self.assertEqual(guest["role"], "guest")
        duplicate, error, code = server._create_local_user(
            self.conn, "ALICE", "Another-password-456", "Duplicate", "guest")
        self.assertIsNone(duplicate)
        self.assertEqual(code, "USERNAME_EXISTS")

    def test_role_files_are_split_complete_and_repairable(self):
        old_db = server.DB_PATH
        old_dir = os.environ.get("CATMAT_ACCOUNT_DATA_DIR")
        with tempfile.TemporaryDirectory() as td:
            server.DB_PATH = os.path.join(td, "test.db")
            os.environ["CATMAT_ACCOUNT_DATA_DIR"] = os.path.join(td, "accounts")
            try:
                owner, _, _ = server._create_local_user(
                    self.conn, "admin1", "Admin-password-123", "Admin", "owner")
                steward, _, _ = server._create_local_user(
                    self.conn, "steward1", "Steward-password-123", "Steward", "steward")
                guest, _, _ = server._create_local_user(
                    self.conn, "guest1", "Member-password-123", "Guest", "guest")
                counts = server._sync_account_role_files(self.conn)
                self.assertEqual(counts, {"admin": 2, "user": 1})
                grouped = server._load_account_role_files(self.conn, repair=False)
                self.assertEqual({r["id"] for r in grouped["admin"]},
                                 {owner["id"], steward["id"]})
                self.assertEqual(grouped["user"][0]["id"], guest["id"])
                self.assertTrue(grouped["admin"][0]["password_hash"].startswith("scrypt$"))
                self.assertEqual(set(grouped["admin"][0]), set(server._ACCOUNT_FIELDS))
                self.assertIsNotNone(server._local_account_by_username(self.conn, "guest1"))
                with open(server._account_role_path("user"), "w", encoding="utf-8") as f:
                    f.write("[]\n")
                repaired = server._load_account_role_files(self.conn)
                self.assertEqual(len(repaired["user"]), 1)
            finally:
                server.DB_PATH = old_db
                if old_dir is None:
                    os.environ.pop("CATMAT_ACCOUNT_DATA_DIR", None)
                else:
                    os.environ["CATMAT_ACCOUNT_DATA_DIR"] = old_dir

    def test_local_session_mode_and_scope(self):
        user, _, _ = server._create_local_user(
            self.conn, "member", "Member-password-123", "Member", "guest")
        tokens = server._issue_session(self.conn, user, mode="local")
        claims, error = server.jwt_verify(tokens["access_token"])
        self.assertIsNone(error)
        self.assertEqual(claims["mode"], "local")
        self.assertEqual(claims["roles"], ["guest"])
        self.assertEqual(claims["scope"], "platform")

    def test_three_role_permission_matrix(self):
        # 东翁：全部放行
        for method, seg in [("DELETE", ["systems", "1"]), ("POST", ["ml", "train"]),
                            ("POST", ["workflow", "submit"]), ("POST", ["llm", "config"])]:
            self.assertTrue(server._business_allowed("owner", method, seg)[0], (method, seg))
        # 清客：浏览 + 全员写白名单（文献扩充/解读归档/CM 队列/问答/预览）放行；
        # 数据删改、批量流水线、账号与系统配置一律拒绝
        guest_allowed = [
            ("GET", ["systems"]),
            ("GET", ["literature"]),
            ("GET", ["knowledge"]),
            ("GET", ["tasks"]),
            ("GET", ["queue"]),
            ("POST", ["literature", "doi"]),
            ("POST", ["literature", "12", "interpret"]),
            ("POST", ["queue"]),
            ("POST", ["cp2k-submit"]),
            ("POST", ["llm", "chat"]),
            ("POST", ["workflow", "preview"]),
        ]
        guest_denied = [
            ("DELETE", ["systems", "1"]),
            ("DELETE", ["literature", "2"]),
            ("POST", ["systems"]),
            ("POST", ["knowledge"]),
            ("PUT", ["systems", "1"]),
            ("POST", ["ml", "train"]),
            ("POST", ["al", "train-async"]),
            ("POST", ["hts", "run"]),
            ("POST", ["workflow", "submit"]),
            ("POST", ["llm", "config"]),
            ("POST", ["sync"]),
            ("POST", ["import"]),
            ("POST", ["postprocess"]),
        ]
        for method, seg in guest_allowed:
            self.assertTrue(server._business_allowed("guest", method, seg)[0], (method, seg))
        for method, seg in guest_denied:
            self.assertFalse(server._business_allowed("guest", method, seg)[0], (method, seg))
        # 掌书：无能力包时不比清客多；授予 delete_data/run_pipelines 后放行相应写操作
        self.assertFalse(server._business_allowed("steward", ("DELETE"), ["systems", "1"], [])[0])
        self.assertTrue(server._business_allowed(
            "steward", "DELETE", ["systems", "1"], [server.CAP_DELETE_DATA])[0])
        self.assertFalse(server._business_allowed("steward", "POST", ["ml", "train"], [])[0])
        self.assertTrue(server._business_allowed(
            "steward", "POST", ["ml", "train"], [server.CAP_RUN_PIPELINES])[0])
        # 系统配置（llm/config）连掌书也不可授
        self.assertFalse(server._business_allowed(
            "steward", "POST", ["llm", "config"], [server.CAP_MANAGE_ACCOUNTS])[0])


class TestWechatAuth(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(AUTH_SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_test_mode_only_accepts_explicit_test_code(self):
        identity, error, code = server._wechat_code_to_identity("normal-code")
        self.assertIsNone(identity)
        self.assertEqual(code, "INVALID_WECHAT_CODE")
        self.assertIn("test:", error)

    def test_first_login_creates_anonymous_user(self):
        identity, error, code = server._wechat_code_to_identity("test:member")
        self.assertIsNone(error)
        user = server._upsert_wechat_user(self.conn, identity)
        self.assertEqual(user["openid"], "test_openid_member")
        self.assertEqual(user["role"], "guest")
        self.assertTrue(user["display_name"].startswith("科研用户-"))

    def test_admin_openid_maps_to_owner_role(self):
        user = server._upsert_wechat_user(
            self.conn, {"openid": "test_openid_admin", "unionid": None})
        self.assertEqual(user["role"], "owner")

    def test_access_token_sign_and_expiry(self):
        user = server._upsert_wechat_user(
            self.conn, {"openid": "test_openid_member", "unionid": None})
        tokens = server._issue_session(self.conn, user)
        claims, error = server.jwt_verify(tokens["access_token"])
        self.assertIsNone(error)
        self.assertEqual(claims["uid"], user["id"])
        self.assertEqual(claims["typ"], "access")

        expired = server.jwt_sign({"typ": "access", "uid": user["id"], "exp": 1})
        claims, error = server.jwt_verify(expired)
        self.assertIsNone(claims)
        self.assertEqual(error, "TOKEN_EXPIRED")

    def test_refresh_token_is_stored_as_hash(self):
        user = server._upsert_wechat_user(
            self.conn, {"openid": "test_openid_member", "unionid": None})
        tokens = server._issue_session(self.conn, user)
        row = self.conn.execute("SELECT refresh_token_hash FROM auth_sessions").fetchone()
        self.assertNotEqual(row["refresh_token_hash"], tokens["refresh_token"])
        self.assertEqual(row["refresh_token_hash"], server._token_hash(tokens["refresh_token"]))

    def test_invalid_signature_rejected(self):
        user = server._upsert_wechat_user(
            self.conn, {"openid": "test_openid_member", "unionid": None})
        token = server._issue_session(self.conn, user)["access_token"]
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        claims, error = server.jwt_verify(tampered)
        self.assertIsNone(claims)
        self.assertEqual(error, "UNAUTHORIZED")


class TestAdminTokenChannel(unittest.TestCase):
    """网页端管理员令牌通道（与微信会话共用机制）。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(AUTH_SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_verify_admin_token(self):
        ok, code = server._verify_admin_token("unit-admin-token")
        self.assertTrue(ok)
        ok, code = server._verify_admin_token("wrong-token")
        self.assertFalse(ok)
        self.assertEqual(code, "INVALID_ADMIN_TOKEN")
        ok, code = server._verify_admin_token("")
        self.assertEqual(code, "INPUT_ERROR")

    def test_admin_holder_created_once(self):
        # 系统令牌通道 = 历史上的“平台管理员”，对标掌书记录；授权层等效东翁
        user, err, code = server._upsert_admin_holder(self.conn)
        self.assertIsNone(err)
        self.assertEqual(user["role"], "steward")
        self.assertEqual(user["openid"], server.ADMIN_HOLDER_OPENID)
        user2, _, _ = server._upsert_admin_holder(self.conn)
        self.assertEqual(user2["id"], user["id"])       # 幂等：不重复建号

    def test_admin_holder_effective_role_is_owner(self):
        user, _, _ = server._upsert_admin_holder(self.conn)
        # 授权层：令牌通道等效东翁（系统钥匙，不占用“东翁唯一”名额）
        self.assertEqual(server._effective_role(user), "owner")
        # 业务授权全权放行
        self.assertTrue(server._business_allowed(
            server._effective_role(user), "DELETE", ["systems", "1"])[0])

    def test_admin_session_mode_system(self):
        user, _, _ = server._upsert_admin_holder(self.conn)
        tokens = server._issue_session(self.conn, user, mode="admin")
        claims, error = server.jwt_verify(tokens["access_token"])
        self.assertIsNone(error)
        self.assertEqual(claims["mode"], "admin")
        self.assertEqual(claims["roles"], ["steward"])
        self.assertEqual(tokens["mode"], "admin")


if __name__ == "__main__":
    unittest.main(verbosity=2)
