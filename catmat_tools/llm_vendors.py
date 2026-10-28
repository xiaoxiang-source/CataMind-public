#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国内主流大模型（OpenAI 兼容端点）自适应适配层。

职责
----
1. **厂商识别**：根据配置的 base URL（或显式指定的 vendor）识别厂商；
2. **URL 规范化**：自动补齐各厂商必需的路径后缀（/v1、/compatible-mode/v1、/api/paas/v4、/api/v3 …）；
3. **参数适配**：模型默认值、temperature 区间、max_tokens 上限、推理字段、
   流式输出支持等差异统一处理；
4. **错误提示**：把 HTTP 状态码/网络异常翻译为**中文可读**建议（含该厂商正确的 URL 示例）；
5. **可配置可切换**：厂商可显式指定，未识别时按「通用 OpenAI 兼容」处理并提示。

本模块不依赖 server.py，可单独测试与复用。
"""
import re

# ---------------------------------------------------------------------------
# 厂商档案（新增厂商只需在此追加一条）
# ---------------------------------------------------------------------------
GENERIC = {
    "key": "generic",
    "label": "通用 OpenAI 兼容端点",
    "hosts": (),
    "default_base": "",
    "default_model": "",
    "version_suffix": "/v1",           # base 缺少版本段时自动补
    "path": "/chat/completions",
    "temperature_range": (0.0, 2.0),
    "max_tokens_default": 2048,
    "max_tokens_cap": 8192,
    "supports_reasoning": False,
    "supports_json_mode": False,
    "supports_stream": True,
    "timeout": 120,
    "notes": "未识别厂商时按 OpenAI 兼容通用处理（URL 需带 /v1 等版本段）。",
}

VENDOR_PROFILES = {
    "deepseek": {
        "key": "deepseek",
        "label": "DeepSeek",
        "hosts": ("api.deepseek.com", "deepseek.com"),
        "default_base": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "version_suffix": "/v1",
        "path": "/chat/completions",
        "temperature_range": (0.0, 2.0),
        "max_tokens_default": 2048,
        "max_tokens_cap": 8192,
        "supports_reasoning": False,   # 仅 deepseek-reasoner 支持推理字段，按需手工开
        "supports_json_mode": True,
        "supports_stream": True,
        "timeout": 180,
        "notes": "base_url 必须带 /v1；deepseek-chat 不支持 reasoning_effort。",
    },
    "qwen": {
        "key": "qwen",
        "label": "通义千问 Qwen（DashScope 兼容模式）",
        "hosts": ("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"),
        "default_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "version_suffix": "/compatible-mode/v1",
        "path": "/chat/completions",
        "temperature_range": (0.0, 2.0),
        "max_tokens_default": 2048,
        "max_tokens_cap": 8192,
        "supports_reasoning": True,    # Qwen3 系列可用 enable_thinking
        "supports_json_mode": True,
        "supports_stream": True,
        "timeout": 180,
        "notes": "必须使用兼容模式端点 /compatible-mode/v1；qwen3 系列可用 enable_thinking 控制思考。",
    },
    "glm": {
        "key": "glm",
        "label": "智谱 GLM（开放平台）",
        "hosts": ("open.bigmodel.cn", "bigmodel.cn", "open.bigmodel.gl"),
        "default_base": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
        "version_suffix": "/api/paas/v4",
        "path": "/chat/completions",
        "temperature_range": (0.0, 1.0),
        "max_tokens_default": 1024,
        "max_tokens_cap": 4096,
        "supports_reasoning": False,
        "supports_json_mode": False,
        "supports_stream": True,
        "timeout": 180,
        "notes": "v4 端点为 /api/paas/v4；不支持 reasoning_effort，temperature 建议 ≤1。",
    },
    "kimi": {
        "key": "kimi",
        "label": "月之暗面 Kimi（Moonshot）",
        "hosts": ("api.moonshot.cn", "api.moonshot.com", "moonshot.cn"),
        "default_base": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
        "version_suffix": "/v1",
        "path": "/chat/completions",
        "temperature_range": (0.0, 1.0),
        "max_tokens_default": 2048,
        "max_tokens_cap": 8192,
        "supports_reasoning": False,
        "supports_json_mode": False,
        "supports_stream": True,
        "timeout": 180,
        "notes": "temperature 取值 0~1；模型名形如 moonshot-v1-8k/32k/128k。",
    },
    "doubao": {
        "key": "doubao",
        "label": "火山方舟 豆包（Ark）",
        "hosts": ("ark.cn-beijing.volces.com", "volces.com", "volcengineapi.com"),
        "default_base": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "ep-xxxxxxxx-xxxxx",   # 方舟「接入点 ID」
        "version_suffix": "/api/v3",
        "path": "/chat/completions",
        "temperature_range": (0.0, 1.0),
        "max_tokens_default": 2048,
        "max_tokens_cap": 4096,
        "supports_reasoning": False,
        "supports_json_mode": False,
        "supports_stream": True,
        "timeout": 180,
        "notes": "model 需填方舟控制台的接入点 ID（形如 ep-xxxxxxxx）；端点为 /api/v3。",
    },
    "siliconflow": {
        "key": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "hosts": ("api.siliconflow.cn", "siliconflow.cn", "api.siliconflow.com"),
        "default_base": "https://api.siliconflow.cn/v1",
        "default_model": "Qwen/Qwen2.5-7B-Instruct",
        "version_suffix": "/v1",
        "path": "/chat/completions",
        "temperature_range": (0.0, 2.0),
        "max_tokens_default": 2048,
        "max_tokens_cap": 8192,
        "supports_reasoning": False,
        "supports_json_mode": True,
        "supports_stream": True,
        "timeout": 180,
        "notes": "模型名需带厂商前缀，如 Qwen/Qwen2.5-7B-Instruct、deepseek-ai/DeepSeek-V3。",
    },
    "openai": {
        "key": "openai",
        "label": "OpenAI（官方）",
        "hosts": ("api.openai.com",),
        "default_base": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "version_suffix": "/v1",
        "path": "/chat/completions",
        "temperature_range": (0.0, 2.0),
        "max_tokens_default": 2048,
        "max_tokens_cap": 16384,
        "supports_reasoning": True,
        "supports_json_mode": True,
        "supports_stream": True,
        "timeout": 180,
        "notes": "官方端点；o 系列与部分新模型仅支持 max_completion_tokens。",
    },
}


def list_vendors():
    """返回可供前端下拉的厂商清单（含 key/label/默认 URL 与备注）。"""
    out = [{"key": "auto", "label": "自动识别（按 base URL）", "default_base": "", "notes": ""},
           {"key": "generic", "label": GENERIC["label"], "default_base": "",
            "notes": GENERIC["notes"]}]
    for k, p in VENDOR_PROFILES.items():
        out.append({"key": k, "label": p["label"], "default_base": p["default_base"],
                    "notes": p["notes"]})
    return out


def _host_of(base_url):
    s = (base_url or "").strip()
    m = re.match(r"^[a-zA-Z]+://([^/:?#]+)", s)
    if m:
        return m.group(1).lower()
    return re.split(r"[/?#]", s)[0].lower()


def detect_vendor(base_url, explicit=None):
    """识别厂商。返回 (vendor_key, profile, matched: bool)。

    explicit: 用户显式指定的 vendor（"auto" 或 None 表示自动识别）。
    matched=False 表示未匹配到已知厂商（按通用 OpenAI 兼容处理）。
    """
    if explicit and explicit not in ("auto", "", None):
        p = VENDOR_PROFILES.get(str(explicit).lower())
        if p:
            return p["key"], p, True
        return "generic", GENERIC, False
    host = _host_of(base_url)
    if host:
        for key, p in VENDOR_PROFILES.items():
            for h in p["hosts"]:
                if host == h or host.endswith("." + h):
                    return key, p, True
    return "generic", GENERIC, False


def normalize_base_url(base_url, profile):
    """规范化 base URL：去尾部斜杠，并补齐厂商必需的版本路径段。

    例：https://api.deepseek.com → https://api.deepseek.com/v1
        https://dashscope.aliyuncs.com → https://dashscope.aliyuncs.com/compatible-mode/v1
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return base
    suffix = profile.get("version_suffix") or "/v1"
    for seg in ("/chat/completions", "/completions", "/responses"):
        if base.endswith(seg):
            base = base[: -len(seg)]
    if base.endswith(suffix):
        return base
    # 已含其它版本段（/vN、/compatible-mode/vN、/api/paas/vN、/api/vN）→ 视为用户自定义，保持
    if re.search(r"/(v\d+|compatible-mode/v\d+|api/paas/v\d+|api/v\d+)$", base):
        return base
    return base + suffix


def build_payload(profile, messages, model=None, temperature=None, cfg=None):
    """按厂商档案组装请求体：模型默认值、temperature 裁剪、max_tokens 上限、推理字段与流式。"""
    cfg = cfg or {}
    p = {"messages": messages or []}
    p["model"] = (model or cfg.get("model") or profile.get("default_model") or "").strip()
    # temperature：裁剪到厂商允许区间
    t = temperature if temperature is not None else cfg.get("temperature")
    lo, hi = profile.get("temperature_range", (0.0, 2.0))
    try:
        t = float(t) if t not in (None, "") else 0.7
    except (TypeError, ValueError):
        t = 0.7
    p["temperature"] = max(lo, min(hi, t))
    # max_tokens：上限裁剪
    cap = int(profile.get("max_tokens_cap", 8192) or 8192)
    try:
        mt = int(cfg.get("max_tokens") or profile.get("max_tokens_default") or 2048)
    except (TypeError, ValueError):
        mt = int(profile.get("max_tokens_default") or 2048)
    p["max_tokens"] = max(1, min(mt, cap))
    # 推理/思考字段：仅厂商支持时带上
    re_effort = cfg.get("reasoning_effort")
    if re_effort and profile.get("supports_reasoning"):
        if profile["key"] == "qwen" and str(re_effort).lower() in ("high", "medium", "low"):
            p["enable_thinking"] = str(re_effort).lower() != "low"
        else:
            p["reasoning_effort"] = re_effort
    # 流式：厂商支持且配置开启时才启用（调用侧负责聚合 SSE）
    p["stream"] = bool(cfg.get("stream")) and bool(profile.get("supports_stream", True))
    if p["stream"]:
        p["stream_options"] = {"include_usage": False}
    return p


def error_hint(code, message, vendor_key=None, base_url=""):
    """把状态码/异常翻译为中文可读提示（含该厂商正确 URL 示例）。"""
    p = VENDOR_PROFILES.get(vendor_key, GENERIC)
    example = p.get("default_base") or ("https://…" + (p.get("version_suffix") or "/v1"))
    msg = (message or "").strip()
    low = msg.lower()
    if code == 401:
        return "API Key 无效或已失效：请检查密钥是否正确、是否有该模型权限（%s）。" % p["label"]
    if code == 403:
        return "无访问权限：该账号可能未开通此模型或服务（%s）。" % p["label"]
    if code == 404:
        return ("接口地址不存在（404）：URL 路径可能不完整。%s 的正确端点示例：%s"
                "（当前：%s）" % (p["label"], example, base_url or "未配置"))
    if code == 402 or "insufficient" in low or "quota" in low or "balance" in low:
        return "账户余额不足或额度用尽：请充值后再试（%s）。" % p["label"]
    if code == 429 or "rate" in low:
        return "请求过于频繁或被限流：请稍后重试，或降低并发（%s）。" % p["label"]
    if code == 400:
        return "请求参数不被接受：%s（%s 参数要求：%s）" % (msg or "参数错误", p["label"], p["notes"])
    if isinstance(code, int) and 500 <= code < 600:
        return "大模型服务端异常（%s）：请稍后重试（%s）。" % (code, p["label"])
    if code == "timeout":
        return "请求超时：可增大超时时间或缩短输入（当前厂商建议超时 %ss）。" % p.get("timeout")
    if code == "network":
        return "无法连接大模型服务：请检查网络/代理与 base URL 是否正确（%s 示例：%s）。" % (
            p["label"], example)
    if code == "not_json":
        return "返回结果不是 JSON：通常意味着 base URL 填错（可能返回了网页），%s 示例：%s。" % (
            p["label"], example)
    if code == "no_content":
        return "接口未返回内容（choices 为空）：请检查模型名与请求参数（%s 默认模型：%s）。" % (
            p["label"], p.get("default_model") or "—")
    return "大模型调用失败（%s）：%s" % (code, msg or "未知错误")


def vendor_summary(base_url, explicit=None):
    """供配置接口返回：识别到的厂商、规范化后的 URL、建议与参数约束。"""
    key, profile, matched = detect_vendor(base_url, explicit)
    normalized = normalize_base_url(base_url, profile) if base_url else ""
    return {
        "vendor": key,
        "vendor_label": profile["label"],
        "matched": matched,
        "base_url_normalized": normalized,
        "base_url_suggestion": profile.get("default_base", ""),
        "default_model": profile.get("default_model", ""),
        "temperature_range": list(profile.get("temperature_range", (0, 2))),
        "max_tokens_cap": profile.get("max_tokens_cap"),
        "supports_stream": profile.get("supports_stream", True),
        "supports_reasoning": profile.get("supports_reasoning", False),
        "timeout": profile.get("timeout"),
        "notes": profile.get("notes", ""),
        "endpoint": (normalized or profile.get("default_base", "")) + profile.get("path", "/chat/completions"),
    }
