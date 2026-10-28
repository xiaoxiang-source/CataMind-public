#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据集 CSV 智能导入（表头自动识别与特征/标签映射）的隔离测试。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server  # noqa: E402


class TestCsvSniffAndClassify(unittest.TestCase):
    def test_sniff_columns_and_kinds(self):
        csv = "name,eps_d,tafel,target,note\n" \
              "Pt_111,-2.10,45,0.42,first\n" \
              "Ni_111,-1.42,38,0.51,\n"
        sniff = server.sniff_dataset_csv(csv)
        names = [c["name"] for c in sniff["columns"]]
        self.assertEqual(names, ["name", "eps_d", "tafel", "target", "note"])
        stats = {c["name"]: c for c in sniff["columns"]}
        self.assertTrue(stats["eps_d"]["numeric"])
        self.assertFalse(stats["name"]["numeric"])

    def test_ml_auto_classify_arbitrary_headers(self):
        csv = "material,feature_b,overpotential\n" \
              "A,-1.0,0.4\nB,-0.8,0.33\n"
        norm = server.normalize_dataset_csv(csv, mode="ml")
        self.assertEqual(norm["name_col"], "material")
        self.assertEqual(norm["target_col"], "overpotential")
        self.assertEqual(norm["feature_cols"], ["feature_b"])
        self.assertEqual(len(norm["rows"]), 2)
        self.assertEqual(norm["rows"][0]["name"], "A")
        self.assertEqual(norm["rows"][0]["features"], {"feature_b": -1.0})
        self.assertEqual(norm["rows"][0]["target"], 0.4)
        # feature_cols 的 kind 标记
        kinds = {c["name"]: c["kind"] for c in norm["detected"]["columns"]}
        self.assertEqual(kinds["material"], "name")
        self.assertEqual(kinds["overpotential"], "target")
        self.assertEqual(kinds["feature_b"], "feature")

    def test_explicit_mapping_overrides(self):
        csv = "id,d1,d2,y\ns1,1.0,2.0,0.9\ns2,1.5,2.5,0.7\n"
        norm = server.normalize_dataset_csv(csv, mapping={
            "name_col": "id", "target_col": "y", "feature_cols": ["d1", "d2"]}, mode="ml")
        self.assertEqual(norm["name_col"], "id")
        self.assertEqual(norm["rows"][0]["features"], {"d1": 1.0, "d2": 2.0})
        # 自动（无 name hint 时第一列也非数值 → 自动生成 name）
        norm2 = server.normalize_dataset_csv("a,b\n1,2\n", mode="ml")
        self.assertTrue(norm2["rows"][0]["name"].startswith("sample_"))
        self.assertEqual(norm2["feature_cols"], ["a", "b"])

    def test_non_numeric_columns_ignored(self):
        csv = "sample,formula,feat,label,comment\nX,Pt,1.1,0.2,ok\nY,Ni,1.3,0.1,no\n"
        norm = server.normalize_dataset_csv(csv, mode="ml")
        self.assertEqual(norm["name_col"], "sample")
        self.assertEqual(norm["target_col"], "label")
        self.assertEqual(norm["feature_cols"], ["feat"])
        kinds = {c["name"]: c["kind"] for c in norm["detected"]["columns"]}
        self.assertEqual(kinds["formula"], "ignored")
        self.assertEqual(kinds["comment"], "ignored")

    def test_al_auto_detect_ed_and_eta(self):
        csv = "体系,ed,d_band,eta_oer,extra\n" \
              "Pt,-2.10,-2.05,0.42,9.1\n" \
              "Ni,-1.42,-1.40,0.51,8.8\n" \
              "Fe,-1.20,-1.18,,8.0\n"
        norm = server.normalize_dataset_csv(csv, mode="al")
        # ed 特征列：命中 AL_ED_HINTS
        self.assertEqual(norm["target_col"], "eta_oer")
        self.assertEqual(norm["ed_col"], "ed")
        # 无 eta 的行 target=None
        by_name = {r["name"]: r for r in norm["rows"]}
        self.assertIsNone(by_name["Fe"]["target"])
        self.assertEqual(by_name["Pt"]["target"], 0.42)
        # 其余数值列（extra）也保留为特征
        self.assertIn("extra", norm["feature_cols"])


class TestCsvImportHandlers(unittest.TestCase):
    def test_ml_dry_run_and_import_flow(self):
        # 通过真实 HTTP 隔离实例验证 dry_run/映射导入已由 test_http_e2e 覆盖；
        # 这里验证 normalize 的 BOM/千分号容错
        csv = "\ufeffname,value_1,target\nS1,\"1,200\",0.5\n"
        norm = server.normalize_dataset_csv(csv, mode="ml")
        self.assertEqual(norm["rows"][0]["features"], {"value_1": 1200.0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
