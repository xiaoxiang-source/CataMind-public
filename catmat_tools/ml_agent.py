#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""休go · 机器学习训练顾问 / 结果解读师（模块化、与平台和 ML/AL 解耦）。

设计要点
--------
1. 本模块只做「数据摘要 → 提示词 → 结构化结果（含校验/降级）」，不直接依赖
   server.py，LLM 调用以**可调用函数注入**（`llm=callable`），因此：
   - 机器学习（ML）与主动学习（AL）共用同一套顾问；
   - 服务端、脚本、测试都可独立调用；
   - 无 LLM（未配置/调用失败）时自动降级为规则建议，功能不中断。
2. 两个环节：
   - **训练前顾问**（导入数据集 → 训练模型）：数据质量评估、特征/标签策略、
     模型与超参建议、训练计划、风险提示；
   - **结果解读师**（后处理分析）：指标解读、过拟合/欠拟合诊断、特征重要性
     点评、下一步建议、主动学习下一批解读。
3. 全程中文术语；结构化输出为固定 JSON 字段（缺失自动补默认值）。

用法示例
--------
    import ml_agent
    summary = ml_agent.summarize_dataset(rows)          # rows=[{name,features,target}]
    advice  = ml_agent.advise_dataset(summary, llm=llm) # llm 可选
    result  = ml_agent.interpret_result(train_result, summary, llm=llm)
"""
import json
import math
import re

# ---------------- 常量：可用模型族（与前端 ML_MODEL_PARAMS 对齐的精简集） ----------------
MODEL_FAMILIES = ("rf", "extra_trees", "gradient_boosting", "hist_gradient_boosting",
                  "ridge", "lasso", "elasticnet", "gpr", "svr", "linear")

SMALL_DATA_MODELS = ("ridge", "lasso", "gpr", "linear", "rf")


# ---------------------------------------------------------------------------
# 一、数据摘要（供人阅读，也作为 LLM 上下文）
# ---------------------------------------------------------------------------
def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not (
        isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


def _col_stats(values):
    nums = [float(v) for v in values if _is_num(v)]
    non_empty = [v for v in values if v not in (None, "")]
    stats = {
        "count": len(values),
        "non_empty": len(non_empty),
        "missing": len(values) - len(non_empty),
        "numeric_count": len(nums),
    }
    if nums:
        nums_sorted = sorted(nums)
        n = len(nums)
        mean = sum(nums) / n
        var = sum((x - mean) ** 2 for x in nums) / n if n > 1 else 0.0
        stats.update({
            "min": round(nums_sorted[0], 6),
            "max": round(nums_sorted[-1], 6),
            "mean": round(mean, 6),
            "std": round(math.sqrt(var), 6),
            "median": round(nums_sorted[n // 2], 6),
        })
        stats["type"] = "数值" if len(nums) == len(non_empty) else "混合"
    else:
        uniq = list({str(v) for v in non_empty})[:8]
        stats.update({"type": "文本/类别", "unique": len({str(v) for v in non_empty}),
                      "samples": uniq})
    return stats


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    dy = math.sqrt(sum((b - my) ** 2 for b in ys))
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 4)


def summarize_dataset(rows, target_key="target"):
    """把样本行压缩为数据集摘要。rows: [{name, features:{...}, target:num|None}]"""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    n = len(rows)
    columns = {}
    targets = []
    for r in rows:
        feats = r.get("features") or {}
        if isinstance(feats, str):
            try:
                feats = json.loads(feats)
            except Exception:
                feats = {}
        if not isinstance(feats, dict):
            feats = {}
        for k, v in feats.items():
            columns.setdefault(str(k), []).append(v)
        tv = r.get(target_key)
        if tv not in (None, ""):
            try:
                targets.append(float(tv))
            except (TypeError, ValueError):
                pass
    col_stats = []
    for name, values in columns.items():
        st = _col_stats(values)
        st["name"] = name
        st["missing_rate"] = round(st["missing"] / n, 4) if n else 0.0
        col_stats.append(st)
    col_stats.sort(key=lambda c: (c["missing_rate"], c["name"]))
    # 目标与特征相关性（取前 8 个最相关）
    corr = []
    if targets:
        for st in col_stats:
            xs = []
            ys = []
            for r in rows:
                feats = r.get("features") or {}
                if isinstance(feats, str):
                    try:
                        feats = json.loads(feats)
                    except Exception:
                        feats = {}
                tv = r.get(target_key)
                if tv in (None, ""):
                    continue
                v = (feats or {}).get(st["name"])
                if _is_num(v):
                    try:
                        xs.append(float(v)); ys.append(float(tv))
                    except (TypeError, ValueError):
                        pass
            r_ = _pearson(xs, ys)
            if r_ is not None:
                corr.append({"feature": st["name"], "r": r_, "n": len(xs)})
        corr.sort(key=lambda c: -abs(c["r"]))
        corr = corr[:8]
    # 规则化问题清单（无 LLM 也能给出基础诊断）
    warnings = []
    for st in col_stats:
        if st["missing_rate"] >= 0.3:
            warnings.append({"level": "高", "column": st["name"],
                             "detail": "缺失率 %.0f%%" % (st["missing_rate"] * 100),
                             "action": "建议删除该列或用列均值/中位数填充"})
        elif st["missing_rate"] > 0:
            warnings.append({"level": "中", "column": st["name"],
                             "detail": "缺失率 %.0f%%" % (st["missing_rate"] * 100),
                             "action": "建议用列均值填充"})
        if st.get("type") == "文本/类别" and st.get("unique", 0) > 20:
            warnings.append({"level": "中", "column": st["name"],
                             "detail": "类别取值 %d 个（高基数）" % st["unique"],
                             "action": "建议独热编码或按频次合并长尾"})
    if n and len(targets) < n:
        warnings.append({"level": "中", "column": target_key,
                         "detail": "%d/%d 行无目标值" % (n - len(targets), n),
                         "action": "无目标行可作候选集（主动学习推荐），不参与监督训练"})
    if n < 20:
        warnings.append({"level": "高", "column": "-",
                         "detail": "样本仅 %d 条" % n,
                         "action": "建议用简单模型（岭回归/高斯过程/浅随机森林）并做交叉验证"})
    tgt_stats = {}
    if targets:
        ts = sorted(targets)
        m = sum(targets) / len(targets)
        var = sum((x - m) ** 2 for x in targets) / max(1, len(targets) - 1)
        tgt_stats = {"count": len(targets), "min": round(ts[0], 6),
                     "max": round(ts[-1], 6), "mean": round(m, 6),
                     "std": round(math.sqrt(var), 6)}
    return {
        "n_samples": n,
        "n_with_target": len(targets),
        "n_features": len(col_stats),
        "columns": col_stats,
        "target_correlations": corr,
        "target": tgt_stats,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 二、提示词构建
# ---------------------------------------------------------------------------
DATASET_ADVISOR_SYSTEM = (
    "你是催化材料机器学习建模顾问（中文回答）。基于给定数据集摘要，给出训练前的"
    "数据质量评估、特征与标签策略、模型与超参建议、训练计划与风险提示。"
    "只输出 JSON，不要多余文字；所有说明文字用中文。"
)

DATASET_ADVISOR_SCHEMA = """{
  "summary_text": "一句话中文总结数据集状况",
  "data_quality": {"score": 0-100整数, "issues": [{"level": "高/中/低", "column": "列名", "detail": "问题", "action": "处理建议"}]},
  "feature_plan": {"target": "目标列名", "recommend_features": ["列名"], "drop": ["列名"],
                   "transforms": [{"column": "列名", "op": "标准化/对数/独热/分箱", "reason": "中文理由"}]},
  "model_plan": {"task": "ml/al", "model": "模型名", "params": {"参数名": 值},
                 "alternatives": [{"model": "模型名", "params": {}}],
                 "rationale": "中文理由", "min_samples": 整数},
  "training_plan": {"split": "如 8:2", "cv": "如 5折", "metrics": ["R2", "MAE"], "early_stop": "中文说明"},
  "cautions": ["中文风险提示"],
  "ready": true/false
}"""

INTERPRETER_SYSTEM = (
    "你是催化材料机器学习结果解读师（中文回答）。基于训练结果、数据摘要与主动学习候选，"
    "给出指标解读、过拟合/欠拟合诊断、特征点评、可执行的下一步建议，以及下一批实验推荐说明。"
    "只输出 JSON，不要多余文字；所有说明文字用中文。"
)

INTERPRETER_SCHEMA = """{
  "verdict": "中文总体结论（1-2 句）",
  "score_text": "如 R²=0.82、MAE=0.05 的中文解读",
  "findings": [{"level": "高/中/低", "detail": "中文发现"}],
  "suggestions": [{"action": "中文动作", "priority": "高/中/低", "detail": "中文说明"}],
  "feature_notes": [{"feature": "列名", "importance": 0-1数值, "note": "中文点评"}],
  "next_batch": {"items": ["候选名"], "rationale": "中文理由"},
  "report_markdown": "可直接粘贴进报告的中文 Markdown 结论"
}"""


def _fmt_summary(summary):
    """把摘要压缩为提示词可用文本（控制长度，避免撑爆上下文）。"""
    cols = (summary.get("columns") or [])[:40]
    lines = ["样本数：%d（含目标 %d）　特征数：%d"
             % (summary.get("n_samples", 0), summary.get("n_with_target", 0),
                summary.get("n_features", 0))]
    if summary.get("target"):
        t = summary["target"]
        lines.append("目标列统计：min=%s max=%s 均值=%s 标准差=%s"
                     % (t.get("min"), t.get("max"), t.get("mean"), t.get("std")))
    for c in cols:
        if c.get("type") == "数值":
            lines.append("- %s：数值，缺失%.0f%%，min=%s max=%s 均值=%s 标准差=%s"
                         % (c["name"], c.get("missing_rate", 0) * 100, c.get("min"),
                            c.get("max"), c.get("mean"), c.get("std")))
        else:
            lines.append("- %s：%s，缺失%.0f%%，取值%d个，样例=%s"
                         % (c["name"], c.get("type"), c.get("missing_rate", 0) * 100,
                            c.get("unique", 0), (c.get("samples") or [])[:3]))
    if summary.get("target_correlations"):
        lines.append("与目标相关性 top：")
        for c in summary["target_correlations"][:8]:
            lines.append("  %s r=%s（n=%d）" % (c["feature"], c["r"], c["n"]))
    if summary.get("warnings"):
        lines.append("已有规则提示：")
        for w in summary["warnings"][:8]:
            lines.append("  [%s] %s：%s → %s"
                         % (w.get("level"), w.get("column"), w.get("detail"), w.get("action")))
    return "\n".join(lines)


def build_dataset_prompt(summary, ctx=None):
    ctx = ctx or {}
    body = _fmt_summary(summary)
    tail = ("\n请按以下 JSON 结构输出（模型名需在 %s 中选择；%s）：\n%s"
            % ("/".join(MODEL_FAMILIES),
               "场景=主动学习（单特征 ε_d → η_OER）" if ctx.get("task") == "al"
               else "场景=监督回归",
               DATASET_ADVISOR_SCHEMA))
    extra = ""
    if ctx.get("model_hint"):
        extra += "\n当前界面选择的模型：%s" % ctx["model_hint"]
    if ctx.get("device"):
        extra += "\n可用设备：%s" % ctx["device"]
    return [{"role": "system", "content": DATASET_ADVISOR_SYSTEM},
            {"role": "user", "content": "数据集摘要：\n" + body + extra + tail}]


def build_interpret_prompt(result, summary=None, ctx=None):
    ctx = ctx or {}
    res = result or {}
    lines = ["训练结果："]
    for k in ("model", "r2", "mae", "rmse", "n_samples", "n_features", "cv_score"):
        if res.get(k) is not None:
            lines.append("- %s = %s" % (k, res[k]))
    if res.get("params"):
        lines.append("- 参数：%s" % json.dumps(res["params"], ensure_ascii=False)[:400])
    if res.get("feature_importance"):
        imp = res["feature_importance"]
        if isinstance(imp, dict):
            top = sorted(imp.items(), key=lambda kv: -float(kv[1] or 0))[:10]
            lines.append("- 特征重要性 top：%s"
                         % ", ".join("%s(%.3f)" % (k, float(v or 0)) for k, v in top))
    if res.get("log_tail"):
        lines.append("- 训练日志尾部：%s" % str(res["log_tail"])[:300])
    if summary:
        lines.append("\n数据摘要：\n" + _fmt_summary(summary))
    if ctx.get("candidates"):
        cands = ctx["candidates"][:10]
        lines.append("\n主动学习候选（名称 / 预测均值 / 不确定度 / 采集值）：")
        for c in cands:
            lines.append("  %s μ=%s σ=%s score=%s"
                         % (c.get("name"), c.get("mu"), c.get("sigma"), c.get("score")))
    lines.append("\n请按以下 JSON 结构输出：\n" + INTERPRETER_SCHEMA)
    return [{"role": "system", "content": INTERPRETER_SYSTEM},
            {"role": "user", "content": "\n".join(lines)}]


# ---------------------------------------------------------------------------
# 三、LLM 输出解析与字段兜底
# ---------------------------------------------------------------------------
def parse_json_reply(text):
    """从 LLM 回复中抽取 JSON 对象（容错 ```json 包裹与前后废话）。"""
    if not text:
        return None
    s = str(text).strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
    if m:
        s = m.group(1).strip()
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _ensure_list(v, limit=20):
    if isinstance(v, list):
        return v[:limit]
    return []


def coerce_advice(data, summary):
    """校验并补齐训练建议字段，保证前端可直接渲染。"""
    data = data if isinstance(data, dict) else {}
    dq = data.get("data_quality") if isinstance(data.get("data_quality"), dict) else {}
    fp = data.get("feature_plan") if isinstance(data.get("feature_plan"), dict) else {}
    mp = data.get("model_plan") if isinstance(data.get("model_plan"), dict) else {}
    tp = data.get("training_plan") if isinstance(data.get("training_plan"), dict) else {}
    model = str(mp.get("model") or "").strip()
    if model not in MODEL_FAMILIES:
        model = ""
    params = mp.get("params") if isinstance(mp.get("params"), dict) else {}
    return {
        "summary_text": str(data.get("summary_text") or "")[:400],
        "data_quality": {
            "score": int(dq.get("score") or 0) if str(dq.get("score") or "").isdigit() else None,
            "issues": _ensure_list(dq.get("issues")),
        },
        "feature_plan": {
            "target": fp.get("target"),
            "recommend_features": _ensure_list(fp.get("recommend_features"), 60),
            "drop": _ensure_list(fp.get("drop"), 60),
            "transforms": _ensure_list(fp.get("transforms"), 30),
        },
        "model_plan": {
            "task": mp.get("task") or "ml",
            "model": model,
            "params": params,
            "alternatives": _ensure_list(mp.get("alternatives"), 5),
            "rationale": str(mp.get("rationale") or "")[:400],
            "min_samples": mp.get("min_samples"),
        },
        "training_plan": {
            "split": tp.get("split"), "cv": tp.get("cv"),
            "metrics": _ensure_list(tp.get("metrics"), 6),
            "early_stop": str(tp.get("early_stop") or "")[:200],
        },
        "cautions": _ensure_list(data.get("cautions"), 12),
        "ready": bool(data.get("ready", len(summary.get("warnings", [])) == 0)),
        "source": "llm",
    }


def coerce_interpretation(data):
    data = data if isinstance(data, dict) else {}
    nb = data.get("next_batch") if isinstance(data.get("next_batch"), dict) else {}
    return {
        "verdict": str(data.get("verdict") or "")[:400],
        "score_text": str(data.get("score_text") or "")[:400],
        "findings": _ensure_list(data.get("findings"), 12),
        "suggestions": _ensure_list(data.get("suggestions"), 12),
        "feature_notes": _ensure_list(data.get("feature_notes"), 20),
        "next_batch": {"items": _ensure_list(nb.get("items"), 20),
                       "rationale": str(nb.get("rationale") or "")[:400]},
        "report_markdown": str(data.get("report_markdown") or "")[:4000],
        "source": "llm",
    }


# ---------------------------------------------------------------------------
# 四、规则降级（无 LLM 或调用失败时）
# ---------------------------------------------------------------------------
def rule_based_dataset_advice(summary):
    n = summary.get("n_samples", 0)
    cols = summary.get("columns") or []
    numeric = [c["name"] for c in cols if c.get("type") == "数值"]
    drop = [c["name"] for c in cols if c.get("missing_rate", 0) >= 0.5]
    keep = [c["name"] for c in cols if c.get("missing_rate", 0) < 0.5]
    corr = summary.get("target_correlations") or []
    top_feats = [c["feature"] for c in corr if abs(c["r"]) >= 0.1][:20] or numeric[:20]
    model = "ridge" if n < 20 else ("gpr" if n < 60 else "rf")
    params = {"alpha": 1.0} if model == "ridge" else (
        {"length_scale": 1.0, "alpha": 1e-6} if model == "gpr"
        else {"n_estimators": 300, "max_depth": None})
    return {
        "summary_text": "共 %d 条样本、%d 个特征，其中 %d 条含目标值。"
                        % (n, len(cols), summary.get("n_with_target", 0)),
        "data_quality": {"score": None, "issues": summary.get("warnings", [])},
        "feature_plan": {
            "target": "target",
            "recommend_features": top_feats,
            "drop": drop,
            "transforms": [{"column": c, "op": "标准化", "reason": "量纲差异大时提升收敛与稳定性"}
                           for c in (numeric[:3] if len(numeric) > 1 else [])],
        },
        "model_plan": {"task": "ml", "model": model, "params": params,
                       "alternatives": [{"model": m, "params": {}} for m in
                                        SMALL_DATA_MODELS if m != model][:3],
                       "rationale": "按样本量选择：小样本优先简单模型与交叉验证。",
                       "min_samples": 10},
        "training_plan": {"split": "8:2", "cv": "5折", "metrics": ["R2", "MAE"],
                          "early_stop": "小样本以交叉验证均值判断，避免单次划分偶然性"},
        "cautions": [w["detail"] for w in summary.get("warnings", []) if w.get("level") == "高"],
        "ready": n >= 5 and summary.get("n_with_target", 0) >= 5,
        "source": "rule",
    }


def rule_based_result_advice(result, summary=None):
    res = result or {}
    r2 = res.get("r2")
    mae = res.get("mae")
    findings = []
    suggestions = []
    if isinstance(r2, (int, float)):
        if r2 >= 0.8:
            findings.append({"level": "低", "detail": "模型拟合良好（R²=%.3f），可用于候选筛选。" % r2})
        elif r2 >= 0.5:
            findings.append({"level": "中", "detail": "拟合一般（R²=%.3f），建议补充特征或样本。" % r2})
            suggestions.append({"action": "扩充样本或增加描述符", "priority": "高",
                                "detail": "当前解释力有限，优先补充高相关特征与样本量。"})
        else:
            findings.append({"level": "高", "detail": "拟合较差（R²=%.3f），需排查特征有效性与数据质量。" % r2})
            suggestions.append({"action": "检查特征与目标的相关性", "priority": "高",
                                "detail": "弱相关特征会显著限制模型上限。"})
    if isinstance(mae, (int, float)) and summary and summary.get("target", {}).get("std"):
        std = summary["target"]["std"]
        if std and mae > std:
            findings.append({"level": "高",
                             "detail": "MAE(%.3f) 大于目标标准差(%.3f)，模型未超过基线。" % (mae, std)})
    if not suggestions:
        suggestions.append({"action": "进行交叉验证复核", "priority": "中",
                            "detail": "用多折交叉验证确认指标稳定性。"})
    return {
        "verdict": "训练完成，可据此开展候选筛选（规则解读，未接入大模型）。",
        "score_text": "R²=%s，MAE=%s" % (r2, mae),
        "findings": findings,
        "suggestions": suggestions,
        "feature_notes": [],
        "next_batch": {"items": [], "rationale": ""},
        "report_markdown": "",
        "source": "rule",
    }


# ---------------------------------------------------------------------------
# 五、对外主入口（llm 可注入；失败自动降级）
# ---------------------------------------------------------------------------
def advise_dataset(summary, ctx=None, llm=None):
    """训练前顾问。llm(messages) -> (text, err)；未提供或失败 → 规则建议。"""
    fallback = rule_based_dataset_advice(summary)
    if not llm:
        return fallback
    try:
        text, err = llm(build_dataset_prompt(summary, ctx))
    except Exception:
        return fallback
    if err or not text:
        return fallback
    data = parse_json_reply(text)
    if not data:
        return fallback
    return coerce_advice(data, summary)


def interpret_result(result, summary=None, ctx=None, llm=None):
    """结果解读师。llm(messages) -> (text, err)；未提供或失败 → 规则解读。"""
    fallback = rule_based_result_advice(result, summary)
    if not llm:
        return fallback
    try:
        text, err = llm(build_interpret_prompt(result, summary, ctx))
    except Exception:
        return fallback
    if err or not text:
        return fallback
    data = parse_json_reply(text)
    if not data:
        return fallback
    return coerce_interpretation(data)
