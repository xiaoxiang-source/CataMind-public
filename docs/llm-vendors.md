# 大模型接口国内主流厂商适配（休go · AI 助手）

> 目标：休go 的 LLM 调用（`server.py → _chat_completions/llm_call`）自动适配国内主流大模型，
> 用户只需填写 **Base URL + 模型名 + API Key**，系统按 URL 自动识别厂商并处理路径与参数差异。

## 一、现状定位（改造前）

- 请求库：标准库 `urllib`（无第三方 SDK）
- 协议：OpenAI 兼容 `POST {base}/chat/completions`
- 问题：要求 base_url 自带 `/v1`；无厂商差异处理；错误提示笼统

## 二、改造后架构

```
前端设置面板（厂商下拉/参数）── GET /api/llm/vendors · /api/llm/config
        │ POST /api/llm/config（vendor/max_tokens/timeout/stream…）
server.py  _chat_completions()
        │ detect_vendor → normalize_base_url → build_payload → 请求 → error_hint
catmat_tools/llm_vendors.py   ← 独立适配模块（可测试、可扩展：加厂商=增一条档案）
```

## 三、支持的厂商与自动补齐路径

| 厂商 | 识别主机 | 自动补齐 URL 后缀 | 默认模型示例 | temperature | max_tokens 上限 |
|---|---|---|---|---|---|
| DeepSeek | api.deepseek.com | `/v1` | deepseek-chat | 0–2 | 8192 |
| 通义千问 Qwen | dashscope.aliyuncs.com | `/compatible-mode/v1` | qwen-plus | 0–2 | 8192 |
| 智谱 GLM | open.bigmodel.cn | `/api/paas/v4` | glm-4-flash | 0–1 | 4096 |
| Kimi | api.moonshot.cn | `/v1` | moonshot-v1-8k | 0–1 | 8192 |
| 火山方舟 豆包 | ark.cn-beijing.volces.com | `/api/v3` | ep-xxxx（接入点 ID） | 0–1 | 4096 |
| 硅基流动 | api.siliconflow.cn | `/v1` | Qwen/Qwen2.5-7B-Instruct | 0–2 | 8192 |
| OpenAI | api.openai.com | `/v1` | gpt-4o-mini | 0–2 | 16384 |
| 通用/未知 | — | 尝试补 `/v1`，并提示未识别 | — | 0–2 | 8192 |

自动补路径示例：
```text
https://api.deepseek.com            → https://api.deepseek.com/v1
https://dashscope.aliyuncs.com      → https://dashscope.aliyuncs.com/compatible-mode/v1
https://open.bigmodel.cn            → https://open.bigmodel.cn/api/paas/v4
https://ark.cn-beijing.volces.com   → https://ark.cn-beijing.volces.com/api/v3
```

## 四、自动适配的参数差异

| 差异点 | 处理 |
|---|---|
| 模型名 | 未填时用厂商默认模型示例名 |
| temperature | 裁剪到厂商区间（Kimi/GLM/豆包 ≤1，其余 ≤2） |
| max_tokens | 配置值裁剪到厂商上限；默认随厂商 |
| 推理字段 | 仅厂商支持才带：Qwen3 系列 → `enable_thinking`；OpenAI → `reasoning_effort`；其余厂商不发送（避免 400），若仍 400 自动去掉未知参数重试一次 |
| 流式输出 | `stream=true` 且厂商支持 → SSE 逐行聚合为完整回复（服务端聚合成非流式结构返回） |
| 超时 | 默认厂商建议（120–180s），可配置 |

## 五、错误提示（中文可读）

| 场景 | 提示要点 |
|---|---|
| 401 | API Key 无效/无权限（标注厂商） |
| 403 | 未开通该模型/服务 |
| 404 | URL 路径不完整，给出该厂商**正确 Base URL 示例** |
| 402/额度 | 余额不足或额度用尽 |
| 429 | 限流，稍后重试 |
| 4xx 参数 | 标注该厂商参数约束（notes） |
| 5xx | 服务端异常，稍后重试 |
| 超时/网络 | 提示增大超时或检查网络/URL |
| 非 JSON 返回 | 多为 base_url 填错（返回网页），给出示例 |

## 六、配置方式（唯一来源：平台设置；不再使用环境变量）

大模型配置与密钥**唯一配置源**为服务器平台设置文件 `llm_config.json`（权限 0600、gitignored）。运行时代码动态读取该文件，**不读取任何 `LLM_*` 环境变量**，也不写回 `.env`。界面保存 / 更换 / 清除密钥即时生效，无需重启。

### 1. 网页设置（唯一密钥入口）
「平台设置 → 大模型」（或对话窗 ⚙）：选厂商（或“自动识别”）→ 填 Base URL / 模型名 / API Key → 面板实时显示
「已识别厂商 / 实际请求端点 / 参数约束」→ 保存；填写新 Key 即完成轮换，点「清除已保存 API Key」即清除。api_key 与其余字段统一落盘 `llm_config.json`。

### 2. llm_config.json（运维兜底；含 api_key）
```json
{
  "base_url": "https://dashscope.aliyuncs.com",
  "model": "qwen-plus",
  "api_key": "sk-xxxx",
  "vendor": "auto",
  "temperature": 0.7,
  "max_tokens": 2048,
  "timeout": 180,
  "stream": false,
  "ai_name": "休go",
  "ai_nicknames": ["小休", "小go"],
  "reasoning_effort": null
}
```
保存后执行 `chmod 600 llm_config.json && systemctl restart catmat`。

> **迁移说明**：旧版经 `.env` 注入的 `LLM_*` 键（含 `LLM_API_KEY`）已从 `.env` 移除并迁移入 `llm_config.json`。升级时无需保留任何 `LLM_*` 环境变量；残留的 `LLM_*` 环境变量会被代码**忽略**。
> **字段一览**：`base_url`（OpenAI 兼容主端点，自动补 `/v1` 等厂商路径）、`model`、`api_key`（仅掩码回显）、`vendor`（`auto` 或厂商 key）、`temperature`（0–2，按厂商裁剪）、`reasoning_effort`（`low|medium|high|xhigh|max`，仅支持厂商生效）、`max_tokens`、`timeout`（秒）、`stream`（bool）、`ai_name`/`ai_nicknames`。均非必填，缺省用默认值。

## 七、厂商档案扩展

新增厂商 = 在 `catmat_tools/llm_vendors.py` 的 `VENDOR_PROFILES` 增加一条（hosts / version_suffix /
temperature_range / max_tokens_cap / supports_* / timeout / notes），即可复用全部机制。

相关：`tests/test_llm_vendors.py`（15 用例）、`docs/admin-manual.md`、`docs/role-naming-rules.md`。
