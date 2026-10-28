# 休go · 机器学习与主动学习接入设计（训练顾问 / 结果解读师）

> 目标：把休go（AI 助手）能力接入**机器学习（ML）与主动学习（AL）**，重点覆盖两个环节：
> ① **导入数据集 → 训练模型**（训练前顾问）；② **后处理分析**（结果解读师）。
> 原则：**模块化、解耦、可复用**；未配置大模型时自动降级为规则建议，功能不中断。
> 全部术语与输出使用中文。

---

## 一、模块划分与依赖方向

```
前端（机器学习页 / 主动学习页）
        │  POST /api/agent/ml/advise     POST /api/agent/ml/interpret
        ▼
server.py（仅做编排：取数 → 调模块 → 审计 → 返回）
        │
        ▼
catmat_tools/ml_agent.py   ←── 独立模块（无 server 依赖，LLM 以函数注入）
        ├─ summarize_dataset()     数据摘要（统计/缺失/相关性/规则问题）
        ├─ build_dataset_prompt()  训练前提示词
        ├─ build_interpret_prompt() 结果解读提示词
        ├─ advise_dataset()        训练前顾问（含降级）
        ├─ interpret_result()      结果解读师（含降级）
        └─ rule_based_*()          规则兜底建议
```

- **解耦**：`ml_agent` 不 import server；LLM 通过 `llm=callable` 注入，测试可注入假函数。
- **复用**：ML（监督回归）与 AL（ε_d → η_OER）共用同一模块，仅上下文 `task` 不同。
- **容错**：模块可用但 LLM 缺失/超时/返回非 JSON 时，返回规则建议并标记 `source="rule"`。

---

## 二、环节一：导入数据集 → 训练模型（休go 训练顾问）

### 2.1 LLM 职责
| 职责 | 说明 |
|---|---|
| 数据质量评估 | 缺失率、量纲差异、高基数类别、异常/偏态、样本量是否够 |
| 特征与标签策略 | 推荐/剔除哪些列；建议变换（标准化、对数、独热、分箱）；目标列确认 |
| 模型与超参建议 | 依据样本量与特征数给出模型族与关键参数，并给备选方案 |
| 训练计划 | 训练/验证划分、交叉验证折数、评价指标、早停与防过拟合建议 |
| 风险提示 | 过拟合风险、目标泄漏、样本不足、类别不均衡等 |

### 2.2 触发条件
- **显式触发**：机器学习页「🧠 AI 训练建议」按钮（对当前 `ml_samples` 全量）。
- **程序化触发**：导入数据集接口成功后可带上 `train` 或前端再次调用；也可传入 `rows`/`csv` 直接分析（不需先入库）。

### 2.3 传给 LLM 的上下文
`summarize_dataset(rows)`：
- 样本数、含目标样本数、特征数
- 每列：类型（数值/类别）、缺失率、min/max/均值/标准差、类别取值数与样例
- 目标列统计（min/max/均值/标准差）
- 与目标的 Pearson 相关性 top 8
- 已有规则化问题清单（供 LLM 复核与补充）

附加上下文：界面选择的模型 `model_hint`、可用设备 `device`、场景 `task=ml|al`。

### 2.4 期望结构化输出
```json
{
  "summary_text": "一句话中文总结",
  "data_quality": {"score": 0-100, "issues": [{"level":"高/中/低","column":"列名","detail":"问题","action":"处理建议"}]},
  "feature_plan": {"target":"目标列","recommend_features":["列名"],"drop":["列名"],
                   "transforms":[{"column":"列名","op":"标准化/对数/独热/分箱","reason":"理由"}]},
  "model_plan":   {"task":"ml/al","model":"模型名","params":{},"alternatives":[],"rationale":"理由","min_samples":20},
  "training_plan":{"split":"8:2","cv":"5折","metrics":["R2","MAE"],"early_stop":"中文说明"},
  "cautions":["中文风险提示"],
  "ready": true
}
```
字段缺失/模型名非法会被 `coerce_advice()` 自动补齐或清空，前端可直接渲染。

---

## 三、环节二：后处理分析（休go 结果解读师）

### 3.1 LLM 职责
| 职责 | 说明 |
|---|---|
| 指标解读 | R² / MAE / RMSE / 交叉验证分中文解释，是否超过基线（与标准差比较） |
| 诊断 | 过拟合/欠拟合、残差异常、特征冗余、样本偏差判断 |
| 特征点评 | 结合特征重要性/SHAP，点评主导因素与可剔除项 |
| 下一步建议 | 补样本、换模型、调参、特征工程等可执行动作（带优先级） |
| 主动学习下一批 | 解读候选排序（μ/σ/采集值），给出下一批实验建议与理由 |
| 报告结论 | 生成可直接粘贴进报告的中文 Markdown |

### 3.2 触发条件
- 训练结果弹窗「🧠 AI 解读结果」（传入当前 result）。
- 主动学习页「🧠 AI 解读推荐」（`task=al`，自动取最近已完成的 AL 任务结果与其候选）。
- 也可按 `task_id` 指定某次历史任务解读（后处理复盘）。

### 3.3 传给 LLM 的上下文
- 训练结果：模型、参数、R²/MAE/RMSE、样本数/特征数、交叉验证分、特征重要性 top10、训练日志尾部
- 数据摘要（可选，默认带上）
- 主动学习候选：名称 / 预测均值 μ / 不确定度 σ / 采集值 score

### 3.4 期望结构化输出
```json
{
  "verdict": "中文总体结论",
  "score_text": "指标中文解读",
  "findings": [{"level":"高/中/低","detail":"中文发现"}],
  "suggestions": [{"action":"中文动作","priority":"高/中/低","detail":"中文说明"}],
  "feature_notes": [{"feature":"列名","importance":0.8,"note":"中文点评"}],
  "next_batch": {"items":["候选名"],"rationale":"中文理由"},
  "report_markdown": "# 结论 …"
}
```

---

## 四、接口与数据流

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/agent/ml/advise` | POST | 训练前顾问。入参 `{rows?\|csv?\|mapping?, model?, device?, task?, limit?}` |
| `/api/agent/ml/interpret` | POST | 结果解读师。入参 `{result?\|task_id?\|task_type?, candidates?\|include_candidates?, with_summary?}` |

数据流（训练前）：
```
导入/已有 ml_samples → summarize_dataset → build_dataset_prompt
   → llm_call（注入） → parse_json_reply → coerce_advice（校验兜底）
   → 审计 ml_agent_advise → 返回 {advice, summary, source, llm_ready}
```

数据流（后处理）：
```
ml_tasks.result（或前端 result）→ build_interpret_prompt（+数据摘要+候选）
   → llm_call → parse_json_reply → coerce_interpretation
   → 审计 ml_agent_interpret → 返回 {interpretation, result, summary, source, llm_ready}
```

### 权限
- 东翁（owner）：放行
- 掌书（steward）：需被授予 `run_tasks` 或 `run_pipelines`
- 清客（guest）：不可调用（ML/AL 训练属受控能力）

### 审计
两条端点均写入审计表：`ml_agent_advise`（样本数、来源）、`ml_agent_interpret`（任务、来源），可追溯谁在何时请求了智能建议。

---

## 五、降级与可观测

| 情况 | 行为 |
|---|---|
| 未配置大模型 API | `llm=None` → 规则建议，`source="rule"`，响应 `llm_ready=false` |
| 调用报错/超时 | 捕获异常 → 规则建议，不抛错 |
| 返回非 JSON | 解析失败 → 规则建议 |
| 字段缺失/模型名非法 | 补齐默认值、清空白名单外模型名 |
| 模块未加载 | 端点返回 500 并在启动日志告警 |

前端以徽章显示来源：`休go（大模型）` / `规则建议（未启用大模型）`，避免使用方误解建议来源。

---

## 六、扩展点（后续可增）

1. **训练中助手**：异步任务进度异常（长时间无进展/损失震荡）时给出提示（复用 `interpret` 的 findings 结构）。
2. **自动采纳**：把 `feature_plan`/`model_plan` 一键回填到训练面板（需前端补“采纳建议”按钮）。
3. **知识回流**：把 `report_markdown` 结论归档到知识库/休go记忆，带归档用户名标签（沿用现有归档机制）。
4. **缓存复用**：同一数据集指纹的建议缓存，避免重复消耗 token。

---

相关：`docs/role-naming-rules.md`（权限与角色）、`docs/admin-manual.md §5.5`（三角色与环境变量）、`tests/test_ml_agent.py`（模块测试）。
