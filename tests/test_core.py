#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""催化智构 CataMind · 核心链路自测

运行方式（任选其一）：
    cd catmat-platform && python3 -m unittest discover -s tests -p "test_*.py" -v
    cd catmat-platform && python3 tests/test_core.py

设计说明（整改 [低4] P2-2）：
- 平台以"零第三方依赖"为架构原则，故**不使用 pytest**，改用标准库 `unittest`
  实现等价目标：回归验证从"靠手感"变为"可重复执行"。
- 覆盖范围限定为**无需外部计算程序即可验证**的核心逻辑（参数库检索、参数白名单、
  错误码、Agent 工具管控、审计、备份、记忆同步）。
- 涉及 CP2K / Multiwfn / AiiDA 真实调用的链路，仍由 `TESTING.md` 人工手册覆盖。
- 所有写操作（审计、备份）均在测试后自行清理，不污染业务数据。
"""
import os
import sys
import json
import time
import shutil
import sqlite3
import unittest
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def setUpModule():
    """Clone an existing reference DB read-only; every test write is disposable."""
    global _fixture, _original_paths
    _fixture = tempfile.TemporaryDirectory(prefix="catamind-core-tests-")
    _original_paths = {k: getattr(server, k) for k in ("DB_PATH", "BACKUP_DIR")}
    server.DB_PATH = os.path.join(_fixture.name, "fixture.db")
    server.BACKUP_DIR = os.path.join(_fixture.name, "backups")
    if os.path.isfile(_original_paths["DB_PATH"]):
        source = sqlite3.connect("file:" + _original_paths["DB_PATH"] + "?mode=ro", uri=True)
        target = sqlite3.connect(server.DB_PATH)
        try:
            source.backup(target)
        finally:
            target.close(); source.close()
    else:
        server.init_db()
        server.seed_if_empty()


def tearDownModule():
    for key, value in _original_paths.items():
        setattr(server, key, value)
    _fixture.cleanup()


class TestAseParamWhitelist(unittest.TestCase):
    """[中1] 结构生成参数白名单：杜绝未知参数被静默忽略。"""

    def test_unknown_param_rejected(self):
        """报告中的真实案例：误传 elements 曾静默生成 Ir 结构。"""
        ok, err = server.check_unknown_ase_params({"elements": ["Pt", "O", "H"]},
                                                  server.MWF_INP_KEYS)
        self.assertFalse(ok)
        self.assertIn("elements", err)
        self.assertIn("静默忽略", err)

    def test_valid_params_accepted(self):
        ok, err = server.validate_ase_params(
            {"element": "Pt", "face": "fcc111", "layers": 3, "ads_site": "ontop"})
        self.assertTrue(ok, err)

    def test_invalid_face_rejected(self):
        ok, err = server.validate_ase_params({"face": "fcc999"})
        self.assertFalse(ok)
        self.assertIn("fcc111", err)      # 错误信息中应给出合法取值

    def test_invalid_ads_site_rejected(self):
        ok, err = server.validate_ase_params({"ads_site": "nowhere"})
        self.assertFalse(ok)
        self.assertIn("ontop", err)

    def test_multiwfn_own_keys_allowed(self):
        """接口自身字段不应被误判为未知参数。"""
        ok, err = server.check_unknown_ase_params(
            {"calc_type": "energy", "dispersion": "d3bj", "smearing": True,
             "pbc": "xyz", "scf_method": "ot", "extra": {}}, server.MWF_INP_KEYS)
        self.assertTrue(ok, err)


class TestAgentToolControl(unittest.TestCase):
    """[高4] 高权限工具默认不可见，且需服务端+用户双条件。"""

    def test_high_priv_hidden_by_default(self):
        names = [t["function"]["name"] for t in server.agent_tools_for(False)]
        leaked = [n for n in names if n in server.HIGH_PRIV_TOOLS]
        self.assertEqual(leaked, [], "高权限工具不应出现在默认工具列表中")

    def test_high_priv_hidden_without_server_flag(self):
        """服务端硬开关未开时，即便前端传 True 也必须隐藏。"""
        if server.AGENT_HIGH_PRIV_ENABLED:
            self.skipTest("当前环境已开启 CATMAT_AGENT_HIGH_PRIV")
        names = [t["function"]["name"] for t in server.agent_tools_for(True)]
        self.assertEqual([n for n in names if n in server.HIGH_PRIV_TOOLS], [])

    def test_high_priv_set_is_complete(self):
        self.assertEqual(server.HIGH_PRIV_TOOLS,
                         {"submit_calc", "run_ase_custom_code",
                          "normalize_structure", "analyze_adsorption_sites"})


class TestErrorCodes(unittest.TestCase):
    """[低2] 结构化错误码映射。"""

    def test_http_default_mapping(self):
        self.assertEqual(server.HTTP_DEFAULT_CODE[400], "INPUT_ERROR")
        self.assertEqual(server.HTTP_DEFAULT_CODE[404], "NOT_FOUND")
        self.assertEqual(server.HTTP_DEFAULT_CODE[405], "METHOD_NOT_ALLOWED")
        self.assertEqual(server.HTTP_DEFAULT_CODE[409], "CONFLICT")
        self.assertEqual(server.HTTP_DEFAULT_CODE[500], "INTERNAL")
        self.assertEqual(server.HTTP_DEFAULT_CODE[502], "UPSTREAM")

    def test_error_payload_shape(self):
        """错误响应必须同时带 error 文本与 code 字段（code 为新增，向后兼容）。"""
        captured = {}

        class _FakeHandler:
            def _send(self, code, obj, **kw):
                captured["code"] = code
                captured["obj"] = obj

        # 直接绑定未绑定方法，验证 _err 的输出结构
        server.Handler._err(_FakeHandler(), 400, "测试错误")
        self.assertEqual(captured["code"], 400)
        self.assertEqual(captured["obj"]["ok"], False)
        self.assertEqual(captured["obj"]["error"], "测试错误")
        self.assertEqual(captured["obj"]["code"], "INPUT_ERROR")

        server.Handler._err(_FakeHandler(), 500, "内部", server.ERR_DEPENDENCY)
        self.assertEqual(captured["obj"]["code"], "DEPENDENCY")


class TestParamLibrary(unittest.TestCase):
    """CP2K 参数库检索（平台核心资产）。"""

    @classmethod
    def setUpClass(cls):
        cls.conn = server.get_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_keyword_search(self):
        if not self.conn.execute("SELECT count(*) FROM cp2k_keywords").fetchone()[0]:
            self.skipTest("当前隔离库未导入 CP2K 参数数据")
        like = "%CUTOFF%"
        rows = self.conn.execute(
            "SELECT name, section_path FROM cp2k_keywords WHERE name LIKE ? LIMIT 5",
            (like,)).fetchall()
        self.assertGreater(len(rows), 0, "应能检索到 CUTOFF 关键词")

    def test_library_scale(self):
        n = self.conn.execute("SELECT count(*) c FROM cp2k_keywords").fetchone()["c"]
        if not n:
            self.skipTest("当前隔离库未导入 CP2K 参数数据")
        self.assertGreater(n, 60000, "参数库规模异常（预期 6.7 万条）")

    def test_required_tables_exist(self):
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("systems", "knowledge", "ml_samples", "audit", "agent_memory"):
            self.assertIn(t, names, "缺少数据表: %s" % t)


class TestAuditAndBackup(unittest.TestCase):
    """[高5] 审计写入；[高3] 数据库备份。两者均需自我清理。"""

    def setUp(self):
        self.conn = server.get_db()

    def tearDown(self):
        self.conn.close()

    def test_audit_write_and_cleanup(self):
        before = self.conn.execute("SELECT count(*) c FROM audit").fetchone()["c"]
        self.assertTrue(server.audit(self.conn, "unittest_action",
                                     target="/api/test", params={"k": 1}, ok=True))
        after = self.conn.execute("SELECT count(*) c FROM audit").fetchone()["c"]
        self.assertEqual(after, before + 1)
        # 清理
        self.conn.execute("DELETE FROM audit WHERE action='unittest_action'")
        self.conn.commit()
        self.assertEqual(self.conn.execute(
            "SELECT count(*) c FROM audit").fetchone()["c"], before)

    def test_audit_never_breaks_business(self):
        """传入非法表/字段时只应返回 False，不得抛异常。"""
        bad = sqlite3.connect(":memory:")
        result = server.audit(bad, "x", target="y")      # 无 audit 表
        bad.close()
        self.assertFalse(result)

    def test_backup_creates_file(self):
        dst = server.backup_db(tag="unittest")
        self.assertIsNotNone(dst, "备份应成功")
        self.assertTrue(os.path.isfile(dst))
        with sqlite3.connect(dst) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        os.remove(dst)      # 清理


class TestAgentMemorySync(unittest.TestCase):
    """休go记忆 ↔ 知识库同步（增量、幂等）。"""

    def setUp(self):
        self.conn = server.get_db()

    def tearDown(self):
        self.conn.close()

    def test_sync_is_idempotent(self):
        server.sync_knowledge_to_agent_memory(self.conn)
        added, updated, skipped, total = server.sync_knowledge_to_agent_memory(self.conn)
        self.assertEqual(updated, 0, "二次同步不应产生更新")
        self.assertGreater(total, 0)

    def test_memory_index_is_compact(self):
        idx = server.agent_memory_index(self.conn)
        self.assertLess(len(idx), 2000, "记忆索引过长会撑爆系统提示")
        self.assertLess(max(len(l) for l in idx.split("\n")) if idx else 0, 130)

    def test_summary_truncates_large_body(self):
        """超长值截断到 200 字；超过 12 个键时标注剩余键数，防止审计表被撑爆。"""
        big = {"k%d" % i: "x" * 300 for i in range(20)}
        s = server._summarize_body(big)
        self.assertLessEqual(len(s.get("k0", "")), 200, "单值应截断到 200 字")
        self.assertIn("_more_keys", s, "超过 12 个键时应标注剩余键数")
        self.assertEqual(s["_more_keys"], 20 - 12)


class TestVersionAndProcessCache(unittest.TestCase):
    """[中2] 版本缓存 TTL；[中6] 进程快照缓存 TTL。"""

    def test_version_ttl_raised(self):
        self.assertEqual(server.VER_TTL, 300)

    def test_process_cache_ttl(self):
        self.assertEqual(server.PROC_TTL, 2.0)

    def test_version_cache_reused(self):
        v1 = server.get_versions()
        v2 = server.get_versions()
        self.assertIs(v1, v2, "TTL 内应复用同一份缓存对象")



class TestAgentAseParamMapping(unittest.TestCase):
    """回归：Agent 工具 generate_structure 的入参必须能翻译到结构生成内核的规范参数。

    历史缺陷：_build_ase_wrap 曾写 `st, err = build_structure_ase(p)`，而该函数
    成功只返回单个 dict、失败抛 ApiError —— dict 解包必然抛 ValueError 并被兜底
    except 吞掉，导致该 Agent 工具**恒失败**；且入参名（symbols/kind/hkl/site）
    与内核契约（element/face/ads_site）完全不符，属双重缺陷。
    """

    def test_symbols_maps_to_element(self):
        p, err = server._agent_ase_params({"symbols": "Pt", "kind": "slab", "hkl": "111"})
        self.assertIsNone(err)
        self.assertEqual(p["element"], "Pt")
        self.assertEqual(p["face"], "fcc111")

    def test_hkl_and_kind_map_to_face(self):
        for hkl, face in (("111", "fcc111"), ("100", "fcc100"),
                          ("110", "fcc110"), ("0001", "hcp0001")):
            with self.subTest(hkl=hkl):
                p, err = server._agent_ase_params({"symbols": "Ir", "kind": "slab", "hkl": hkl})
                self.assertIsNone(err)
                self.assertEqual(p["face"], face)

    def test_site_maps_to_ads_site(self):
        p, err = server._agent_ase_params(
            {"symbols": "Pt", "kind": "adsorb", "adsorbate": "OH", "site": "hollow"})
        self.assertIsNone(err)
        self.assertEqual(p["ads_site"], "fcc")
        self.assertEqual(p["adsorbate"], "OH")

    def test_canonical_params_take_precedence(self):
        p, err = server._agent_ase_params({"symbols": "Pt", "face": "fcc110"})
        self.assertIsNone(err)
        self.assertEqual(p["face"], "fcc110")

    def test_rejects_unsupported_inputs(self):
        """不支持/含糊的入参必须显式报错，不得静默兜底（避免生成元素错误的结构）。"""
        bad = [
            {"symbols": "Pt3Fe", "kind": "slab"},   # 多元素：不按化学式建合金
            {"symbols": "Pt", "kind": "bulk"},      # 内核不支持体相
            {"symbols": "Pt", "hkl": "211"},        # 非法晶面
            {"symbols": "Pt", "adsorbate": "OH", "site": "nowhere"},
            {"symbols": "Pt", "kind": "adsorb"},    # adsorb 缺吸附物
            {"kind": "slab"},                       # 缺元素
            {"symbols": "Xx", "kind": "slab"},      # 非标准元素符号
        ]
        for args in bad:
            with self.subTest(args=args):
                p, err = server._agent_ase_params(args)
                # 元素符号由 validate_ase_params 兜底校验，故两处合并判断
                ok, verr = (server.validate_ase_params(p) if err is None else (False, err))
                self.assertTrue(err or not ok, "应拒绝：%s" % args)

    def test_element_symbol_validated(self):
        ok, err = server.validate_ase_params({"element": "Xx"})
        self.assertFalse(ok)
        self.assertIn("元素符号", err)
        self.assertTrue(server.validate_ase_params({"element": "Pt"})[0])

    def test_build_wrapper_no_longer_unpacks_tuple(self):
        """源码级防回归：wrapper 内不得再出现把单返回值当二元组解包的**语句**。

        用行首锚定匹配，避免把文档字符串里"曾误写为 …"的历史说明也误判为缺陷。
        """
        import inspect
        import re
        src = inspect.getsource(server._build_ase_wrap)
        bad = re.search(r"^\s*\w+\s*,\s*\w+\s*=\s*build_structure_ase\(", src, re.M)
        self.assertIsNone(bad, "仍存在二元解包语句：%s" % (bad.group(0) if bad else ""))
        self.assertIn("except ApiError", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
