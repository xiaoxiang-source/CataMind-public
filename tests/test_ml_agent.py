#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""休go 机器学习顾问模块（catmat_tools/ml_agent.py）的隔离测试。"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "catmat_tools"))
import ml_agent  # noqa: E402


ROWS = [
    {"name": "A", "features": {"eps_d": -2.4, "wf": 5.6}, "target": 0.42},
    {"name": "B", "features": {"eps_d": -1.8, "wf": 5.1}, "target": 0.35},
    {"name": "C", "features": {"eps_d": -1.2, "wf": 4.8}, "target": 0.30},
    {"name": "D", "features": {"eps_d": -0.9, "wf": None}, "target": 0.26},
    {"name": "E", "features": {"eps_d": -0.5, "wf": 4.2}, "target": None},
]


class TestSummarize(unittest.TestCase):
    def test_summary_basic(self):
        s = ml_agent.summarize_dataset(ROWS)
        self.assertEqual(s["n_samples"], 5)
        self.assertEqual(s["n_with_target"], 4)
        self.assertEqual(s["n_features"], 2)
        cols = {c["name"]: c for c in s["columns"]}
        self.assertEqual(cols["eps_d"]["type"], "数值")
        self.assertAlmostEqual(cols["wf"]["missing_rate"], 0.2)
        self.assertIn("mean", cols["eps_d"])
        # 目标相关性应给出 eps_d
        feats = [c["feature"] for c in s["target_correlations"]]
        self.assertIn("eps_d", feats)
        # 无目标行会被提示
        self.assertTrue(any("无目标值" in str(w["detail"]) for w in s["warnings"]))

    def test_summary_empty(self):
        s = ml_agent.summarize_dataset([])
        self.assertEqual(s["n_samples"], 0)
        self.assertEqual(s["columns"], [])


class TestAdvise(unittest.TestCase):
    def test_rule_fallback_without_llm(self):
        s = ml_agent.summarize_dataset(ROWS)
        a = ml_agent.advise_dataset(s)          # 未注入 llm
        self.assertEqual(a["source"], "rule")
        self.assertIn("model", a["model_plan"])
        self.assertTrue(a["feature_plan"]["recommend_features"])
        self.assertIn("summary_text", a)

    def test_llm_advice_parsed_and_coerced(self):
        s = ml_agent.summarize_dataset(ROWS)
        reply = json.dumps({
            "summary_text": "数据可用",
            "data_quality": {"score": 80, "issues": [{"level": "中", "column": "wf",
                                                      "detail": "有缺失", "action": "均值填充"}]},
            "feature_plan": {"target": "target", "recommend_features": ["eps_d"],
                             "drop": [], "transforms": [{"column": "eps_d", "op": "标准化",
                                                         "reason": "量纲"}]},
            "model_plan": {"task": "ml", "model": "not_a_model", "params": {"n_estimators": 200},
                           "alternatives": [], "rationale": "理由", "min_samples": 10},
            "training_plan": {"split": "8:2", "cv": "5折", "metrics": ["R2"], "early_stop": "早停"},
            "cautions": ["样本偏少"],
            "ready": True,
        }, ensure_ascii=False)

        def fake_llm(messages):
            return reply, None

        a = ml_agent.advise_dataset(s, ctx={"task": "ml"}, llm=fake_llm)
        self.assertEqual(a["source"], "llm")
        self.assertEqual(a["summary_text"], "数据可用")
        self.assertEqual(a["data_quality"]["score"], 80)
        self.assertEqual(a["feature_plan"]["recommend_features"], ["eps_d"])
        self.assertEqual(a["model_plan"]["model"], "")   # 非法模型名被清空
        self.assertTrue(a["ready"])

    def test_llm_failure_falls_back(self):
        s = ml_agent.summarize_dataset(ROWS)

        def bad_llm(messages):
            return "我不是 JSON", None

        a = ml_agent.advise_dataset(s, llm=bad_llm)
        self.assertEqual(a["source"], "rule")

        def err_llm(messages):
            return None, "调用失败"

        a2 = ml_agent.advise_dataset(s, llm=err_llm)
        self.assertEqual(a2["source"], "rule")

        def boom(messages):
            raise RuntimeError("网络异常")

        a3 = ml_agent.advise_dataset(s, llm=boom)
        self.assertEqual(a3["source"], "rule")

    def test_prompt_contains_key_fields(self):
        s = ml_agent.summarize_dataset(ROWS)
        msgs = ml_agent.build_dataset_prompt(s, ctx={"task": "al", "model_hint": "gpr"})
        text = "\n".join(m["content"] for m in msgs)
        self.assertIn("样本数", text)
        self.assertIn("eps_d", text)
        self.assertIn("JSON", text)
        self.assertIn("主动学习", text)


class TestInterpret(unittest.TestCase):
    def test_rule_interpret(self):
        out = ml_agent.interpret_result({"r2": 0.3, "mae": 0.4})
        self.assertEqual(out["source"], "rule")
        self.assertTrue(out["suggestions"])

    def test_llm_interpret(self):
        reply = json.dumps({
            "verdict": "拟合良好",
            "score_text": "R²=0.9",
            "findings": [{"level": "低", "detail": "泛化尚可"}],
            "suggestions": [{"action": "补充样本", "priority": "中", "detail": "说明"}],
            "feature_notes": [{"feature": "eps_d", "importance": 0.8, "note": "主导"}],
            "next_batch": {"items": ["X", "Y"], "rationale": "不确定性高"},
            "report_markdown": "# 结论",
        }, ensure_ascii=False)
        out = ml_agent.interpret_result({"r2": 0.9}, llm=lambda m: (reply, None))
        self.assertEqual(out["source"], "llm")
        self.assertEqual(out["next_batch"]["items"], ["X", "Y"])
        self.assertIn("# 结论", out["report_markdown"])

    def test_interpret_prompt_includes_candidates(self):
        msgs = ml_agent.build_interpret_prompt(
            {"r2": 0.8, "model": "rf"},
            summary=None,
            ctx={"candidates": [{"name": "S1", "mu": 0.3, "sigma": 0.1, "score": 0.1}]})
        text = "\n".join(m["content"] for m in msgs)
        self.assertIn("S1", text)
        self.assertIn("0.8", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
