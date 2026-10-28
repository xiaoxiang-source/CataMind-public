#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国内大模型适配层（catmat_tools/llm_vendors.py）的隔离测试。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "catmat_tools"))
import llm_vendors  # noqa: E402


class TestDetect(unittest.TestCase):
    def test_detect_by_host(self):
        cases = [
            ("https://api.deepseek.com", "deepseek"),
            ("https://api.deepseek.com/v1", "deepseek"),
            ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen"),
            ("https://open.bigmodel.cn/api/paas/v4", "glm"),
            ("https://api.moonshot.cn/v1", "kimi"),
            ("https://ark.cn-beijing.volces.com/api/v3", "doubao"),
            ("https://api.siliconflow.cn/v1", "siliconflow"),
            ("https://api.openai.com/v1", "openai"),
        ]
        for url, expect in cases:
            key, prof, matched = llm_vendors.detect_vendor(url)
            self.assertEqual(key, expect, url)
            self.assertTrue(matched)

    def test_unknown_falls_to_generic(self):
        key, prof, matched = llm_vendors.detect_vendor("https://my-relay.example.com/v1")
        self.assertEqual(key, "generic")
        self.assertFalse(matched)

    def test_explicit_vendor(self):
        key, prof, matched = llm_vendors.detect_vendor("https://my-relay.example.com/v1",
                                                       explicit="qwen")
        self.assertEqual(key, "qwen")
        self.assertTrue(matched)
        # auto / 空值仍走自动识别
        self.assertEqual(llm_vendors.detect_vendor("https://api.deepseek.com", "auto")[0],
                         "deepseek")


class TestNormalize(unittest.TestCase):
    def test_suffix_appended(self):
        cases = [
            ("https://api.deepseek.com", "https://api.deepseek.com/v1"),
            ("https://dashscope.aliyuncs.com",
             "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            ("https://open.bigmodel.cn", "https://open.bigmodel.cn/api/paas/v4"),
            ("https://ark.cn-beijing.volces.com", "https://ark.cn-beijing.volces.com/api/v3"),
        ]
        for raw, expect in cases:
            key, prof, _ = llm_vendors.detect_vendor(raw)
            self.assertEqual(llm_vendors.normalize_base_url(raw, prof), expect)

    def test_no_duplicate_suffix(self):
        key, prof, _ = llm_vendors.detect_vendor("https://api.deepseek.com/v1")
        self.assertEqual(llm_vendors.normalize_base_url("https://api.deepseek.com/v1", prof),
                         "https://api.deepseek.com/v1")
        # 尾斜杠与误填 /chat/completions 也能归一
        self.assertEqual(llm_vendors.normalize_base_url(
            "https://api.deepseek.com/chat/completions", prof),
            "https://api.deepseek.com/v1")

    def test_generic_appends_v1(self):
        self.assertEqual(llm_vendors.normalize_base_url("https://relay.example.com",
                                                        llm_vendors.GENERIC),
                         "https://relay.example.com/v1")


class TestPayload(unittest.TestCase):
    def test_temperature_and_max_tokens_clamped(self):
        key, prof, _ = llm_vendors.detect_vendor("https://api.moonshot.cn/v1")
        p = llm_vendors.build_payload(prof, [{"role": "user", "content": "hi"}],
                                      model="moonshot-v1-8k", temperature=1.8,
                                      cfg={"max_tokens": 99999})
        self.assertEqual(p["temperature"], 1.0)      # Kimi 上限 1.0
        self.assertEqual(p["max_tokens"], prof["max_tokens_cap"])
        self.assertEqual(p["model"], "moonshot-v1-8k")

    def test_reasoning_only_when_supported(self):
        _, deepseek, _ = llm_vendors.detect_vendor("https://api.deepseek.com")
        p1 = llm_vendors.build_payload(deepseek, [], model="deepseek-chat",
                                       cfg={"reasoning_effort": "high"})
        self.assertNotIn("reasoning_effort", p1)
        _, qwen, _ = llm_vendors.detect_vendor("https://dashscope.aliyuncs.com")
        p2 = llm_vendors.build_payload(qwen, [], model="qwen3-8b",
                                       cfg={"reasoning_effort": "high"})
        self.assertTrue(p2.get("enable_thinking"))

    def test_stream_respected(self):
        _, prof, _ = llm_vendors.detect_vendor("https://api.deepseek.com")
        p = llm_vendors.build_payload(prof, [], model="deepseek-chat", cfg={"stream": True})
        self.assertTrue(p["stream"])
        p2 = llm_vendors.build_payload(prof, [], model="deepseek-chat", cfg={"stream": False})
        self.assertFalse(p2["stream"])

    def test_default_model_used(self):
        _, prof, _ = llm_vendors.detect_vendor("https://api.deepseek.com")
        p = llm_vendors.build_payload(prof, [], model="", cfg={})
        self.assertEqual(p["model"], "deepseek-chat")


class TestErrorHint(unittest.TestCase):
    def test_404_suggests_correct_url(self):
        hint = llm_vendors.error_hint(404, "not found", "deepseek",
                                      "https://api.deepseek.com")
        self.assertIn("404", hint)
        self.assertIn("https://api.deepseek.com/v1", hint)

    def test_auth_and_quota(self):
        self.assertIn("API Key", llm_vendors.error_hint(401, "", "kimi", ""))
        self.assertIn("余额", llm_vendors.error_hint(402, "insufficient balance", "qwen", ""))
        self.assertIn("限流", llm_vendors.error_hint(429, "rate limit", "glm", ""))

    def test_network_and_timeout(self):
        self.assertIn("无法连接", llm_vendors.error_hint("network", "", "doubao", ""))
        self.assertIn("超时", llm_vendors.error_hint("timeout", "", "glm", ""))
        self.assertIn("不是 JSON", llm_vendors.error_hint("not_json", "", "generic", ""))
        self.assertIn("choices", llm_vendors.error_hint("no_content", "", "kimi", ""))


class TestSummary(unittest.TestCase):
    def test_summary_fields(self):
        s = llm_vendors.vendor_summary("https://dashscope.aliyuncs.com")
        self.assertEqual(s["vendor"], "qwen")
        self.assertTrue(s["matched"])
        self.assertTrue(s["base_url_normalized"].endswith("/compatible-mode/v1"))
        self.assertTrue(s["endpoint"].endswith("/chat/completions"))
        self.assertIn("temperature_range", s)

    def test_vendor_list(self):
        keys = [v["key"] for v in llm_vendors.list_vendors()]
        for k in ("auto", "deepseek", "qwen", "glm", "kimi", "doubao", "openai"):
            self.assertIn(k, keys)


if __name__ == "__main__":
    unittest.main(verbosity=2)
