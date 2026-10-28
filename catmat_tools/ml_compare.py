#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多模型集成对比训练引擎（供 CataMind 平台 + AI 助手调用）。

为什么需要它
------------
平台原有的 `/api/ml/train` 一次只训一个模型，而且三个评估指标在本类小样本数据上
都有口径问题（详见 SKILL 与 README）：

  1. `r2` / `mae` 用 80% 训练却在**全部样本**上评估 → 含训练点，乐观偏置可达 6×
  2. `cv_r2_mean` 走 `cross_val_score(cv=int)` = **不打乱** KFold
     → 数据按分组有序排列时退化成"整段外推"，数值可爆炸（实测 −265256）
  3. CPU 路径与 GPU 并行路径的 KFold 参数**不一致**（shuffle 与否）

本引擎一次性对多个模型跑**同一套严谨协议**，并给出可直接排序的结果：

  A  holdout         单次随机留出（复现平台 test_r2，作为参考）
  B  cv_shuffled     RepeatedKFold(cv_folds × cv_repeats, shuffle)  ← 主排序指标
  C  grouped_oof     按 group_col 做 LeaveOneGroupOut，OOF 聚合口径
                     （把各折预测拼成一条向量后整体算 R²/MAE，而不是逐组算完再平均）
  D  fixed_split     按 fixed_split_col（train/validation/test）评估外推
  基线               预测训练集均值 —— 没有它无法判断 MAE 好不好

配置：
{
  "dataset_file": "<ML-xxx.csv 绝对路径>",   // 省略则用 ml_samples 全表
  "dataset_module": "ml",
  "models": ["extra_trees", "rf", ...] 或 "all",
  "preprocess": true,          // StandardScaler
  "test_size": 0.2,
  "cv_folds": 5,
  "cv_repeats": 10,
  "group_col": "parent_id",          // 可选：分组列（OOF 分组评估）
  "fixed_split_col": "fixed_split",  // 可选：预注册切分列
  "split_train_value": "train", "split_test_value": "test",
  "device": "cpu",
  "n_jobs": 4,
  "shap": true,                // 只对树模型算（快）
  "params": {"extra_trees": {"n_estimators": 500}},
  "output": "<结果 json 路径>"
}

输出：进度行 `PROGRESS=<n> <msg>`，最后一行 JSON（与 ml_train.py 同形）：
  {"ok": true, "result": {...,"models":[...], "ranking":[...]}}
"""
import os
import sys
import json
import time
import csv as _csv

CFG_PATH = sys.argv[2] if len(sys.argv) > 2 else None
_CFG = {}
if CFG_PATH:
    try:
        _CFG = json.loads(open(CFG_PATH, encoding="utf-8").read())
    except Exception:
        _CFG = {}

_JOBS = str(int(_CFG.get("n_jobs", 4) or 4))
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, _JOBS)
_DEVICE = str(_CFG.get("device", "cpu") or "cpu").lower()
if _DEVICE == "cpu":
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"

import numpy as np                                    # noqa: E402
from sklearn.preprocessing import StandardScaler      # noqa: E402
from sklearn.model_selection import (RepeatedKFold, LeaveOneGroupOut,   # noqa: E402
                                     train_test_split)
from sklearn.metrics import r2_score, mean_absolute_error, mean_squared_error  # noqa: E402

_TOOLS = os.path.dirname(os.path.abspath(__file__))
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
import sqlite3                                        # noqa: E402
from ml_train import build_model, _predict_1d, _load_dataset_file_rows  # noqa: E402

_PROJECT_ROOT = os.path.dirname(_TOOLS)
DB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_PROJECT_ROOT, "catmat.db")

# 默认模型池：覆盖 8 个家族。键与 build_model 的 model_type 一致。
DEFAULT_POOL = {
    "linear": {}, "ridge": {"alpha": 1.0}, "lasso": {"alpha": 0.005},
    "elasticnet": {"alpha": 0.01, "l1_ratio": 0.5}, "bayesian_ridge": {},
    "huber": {"epsilon": 1.35}, "svr": {"C": 10.0, "epsilon": 0.1},
    "kernel_ridge": {"alpha": 1.0}, "gpr": {"length_scale": 1.0, "alpha": 1e-6},
    "knn": {"n_neighbors": 5}, "pls": {"n_components": 10},
    "decision_tree": {}, "rf": {"n_estimators": 300},
    "extra_trees": {"n_estimators": 300},
    "gradient_boosting": {"n_estimators": 200, "learning_rate": 0.1, "max_depth": 3},
    "hist_gradient_boosting": {"n_estimators": 200}, "adaboost": {"n_estimators": 200},
    "xgb": {"n_estimators": 200, "learning_rate": 0.1, "max_depth": 6,
            "reg_lambda": 1.0, "subsample": 1.0},
    "lgbm": {"n_estimators": 200, "learning_rate": 0.1},
    "mlp": {"n_estimators": 1000, "learning_rate": 0.001},
    "mlp_torch": {"hidden_1": 64, "hidden_2": 32, "learning_rate": 1e-3,
                  "n_estimators": 400, "batch_size": 32},
    "gpr_torch": {"length_scale": 1.0, "alpha": 1e-4, "learning_rate": 0.1,
                  "n_estimators": 300},
}
# 不支持 n_jobs 形参的模型（避免把 n_jobs 透传进去）
NO_NJOBS = ("pls", "mlp_torch", "gpr_torch")


def emit(p, msg):
    print("PROGRESS=%d %s" % (int(p), msg), flush=True)


# --------------------------------------------------------------------------- 取数
def load_rows():
    ds = _CFG.get("dataset_file")
    if ds:
        if not os.path.isfile(str(ds)):
            raise ValueError("指定数据集不存在，禁止回退历史全表")
        rows = _load_dataset_file_rows(str(ds), module=_CFG.get("dataset_module") or "ml")
        if rows:
            emit(3, "使用数据集文件：%s（%d 行）" % (os.path.basename(str(ds)), len(rows)))
            return rows, os.path.basename(str(ds))
        raise ValueError("指定数据集没有有效样本，禁止回退历史全表")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM ml_samples WHERE target IS NOT NULL").fetchall()
    conn.close()
    return rows, "ml_samples"


def load_aux_columns():
    """从数据集文件里读分组列 / 预注册切分列（这些列**不进特征**，避免标签泄漏）。"""
    ds = _CFG.get("dataset_file")
    gc, sc = _CFG.get("group_col"), _CFG.get("fixed_split_col")
    if not ds or not os.path.isfile(str(ds)) or not (gc or sc):
        return None, None
    groups, splits = {}, {}
    with open(str(ds), encoding="utf-8", errors="replace", newline="") as fh:
        hdr = next(_csv.reader(fh), [])
        fh.seek(0)
        if gc and gc not in hdr:
            emit(3, "提示：数据集中无分组列 %r，已跳过分组 OOF" % gc)
            gc = None
        if sc and sc not in hdr:
            emit(3, "提示：数据集中无切分列 %r，已跳过预注册切分" % sc)
            sc = None
        if not (gc or sc):
            return None, None
        for r in _csv.DictReader(fh):
            nm = (r.get("name") or "").strip()
            if not nm:
                continue
            if gc:
                groups[nm] = r.get(gc)
            if sc:
                splits[nm] = r.get(sc)
    return (groups or None), (splits or None)


def build_matrix(rows):
    """把样本摊成 (names, y, keys, X)。

    只保留可转成 float 的列。以下列一律排除出特征矩阵（防泄漏/防噪声）：
      · cfg.group_col / cfg.fixed_split_col —— 分组与切分变量
      · cfg.exclude_cols —— 显式指定的元数据列（如 seed、composition；
        注意 seed 这类"计算批次"变量是**纯噪声**：它是数值型，不加排除会被
        当成特征，让模型记住某个 seed 的整体偏移，随机 CV 分数虚高）
    """
    excl = {str(_CFG.get("group_col") or ""), str(_CFG.get("fixed_split_col") or "")}
    excl |= {str(c) for c in (_CFG.get("exclude_cols") or [])}
    excl.discard("")
    feats, names, y = [], [], []
    for r in rows:
        raw = json.loads(r["features"] or "{}")
        f = {}
        for k, v in raw.items():
            if k in excl:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if np.isfinite(fv):
                f[k] = fv
        feats.append(f)
        names.append(r["name"])
        y.append(float(r["target"]))
    keys = sorted(set().union(*[set(f.keys()) for f in feats])) if feats else []
    X = np.array([[f.get(k, np.nan) for k in keys] for f in feats], dtype=float)
    return names, np.array(y, dtype=float), keys, X


def fold_transform(train, test, scale=True):
    """Fit imputation/scaling on training rows only; retain all-missing columns."""
    train, test = np.array(train, copy=True), np.array(test, copy=True)
    count = np.sum(np.isfinite(train), axis=0)
    means = np.divide(np.nansum(train, axis=0), count,
                      out=np.zeros(train.shape[1]), where=count > 0)
    train = np.where(np.isfinite(train), train, means)
    test = np.where(np.isfinite(test), test, means)
    if scale:
        scaler = StandardScaler().fit(train)
        train, test = scaler.transform(train), scaler.transform(test)
    return train, test


def main():
    t_start = time.time()
    rows, ds_name = load_rows()
    if len(rows) < 10:
        print(json.dumps({"ok": False, "error": "样本不足（%d），至少需要 10 条" % len(rows)},
                         ensure_ascii=False))
        return

    names, y, keys, X = build_matrix(rows)
    preprocess = bool(_CFG.get("preprocess", True))

    test_size = float(_CFG.get("test_size", 0.2))
    cv_folds = int(_CFG.get("cv_folds", 5))
    cv_repeats = max(1, int(_CFG.get("cv_repeats", 10)))
    n_jobs = int(_CFG.get("n_jobs", 4))
    want = _CFG.get("models") or "all"
    if want == "all":
        pool = dict(DEFAULT_POOL)
    else:
        # 未知模型名交给 build_model 的默认分支（随机森林），但显式列出便于审计
        pool = {str(k): dict(DEFAULT_POOL.get(str(k)) or {}) for k in want}
    if not pool:
        pool = dict(DEFAULT_POOL)
    overrides = _CFG.get("params") or {}

    groups_map, splits_map = load_aux_columns()
    g_arr = np.array([groups_map.get(n) for n in names], dtype=object) if groups_map else None
    s_arr = np.array([splits_map.get(n) for n in names], dtype=object) if splits_map else None
    tr_v = str(_CFG.get("split_train_value", "train"))
    te_v = str(_CFG.get("split_test_value", "test"))

    emit(5, "数据：%s  %d 样本 × %d 特征（预处理=%s）" % (ds_name, len(y), len(keys), preprocess))
    emit(5, "协议：CV=%d 折 × %d 重复（打乱） + 留出 %.0f%%%s%s"
         % (cv_folds, cv_repeats, test_size * 100,
            " + 分组 OOF" if g_arr is not None else "",
            " + 预注册切分" if s_arr is not None else ""))

    # 朴素基线（预测训练集均值）：固定切分的 OOF 版本
    baseline = {}
    if s_arr is not None and (s_arr == tr_v).any() and (s_arr == te_v).any():
        m_tr, m_te = s_arr == tr_v, s_arr == te_v
        if m_te.sum() > 1:
            baseline["fixed_split_test"] = {
                "r2": round(float(r2_score(y[m_te], np.full(int(m_te.sum()), y[m_tr].mean()))), 4),
                "mae": round(float(mean_absolute_error(y[m_te],
                                                       np.full(int(m_te.sum()), y[m_tr].mean()))), 4)}
    if g_arr is not None:
        logo0 = list(LeaveOneGroupOut().split(X, y, groups=g_arr))
        b0 = np.zeros(len(y))
        for tr, te in logo0:
            b0[te] = y[tr].mean()
        baseline["grouped_oof"] = {
            "r2": round(float(r2_score(y, b0)), 4),
            "mae": round(float(mean_absolute_error(y, b0)), 4)}

    results = []
    total = len(pool)
    for mi, (mt, hp) in enumerate(sorted(pool.items()), 1):
        hp = dict(hp or {})
        hp.update(overrides.get(mt) or {})
        kw = dict(hp) if mt in NO_NJOBS else dict(hp, n_jobs=n_jobs)
        rec = {"model": mt, "params": hp}
        t0 = time.time()
        pct = 5 + int(90.0 * (mi - 1) / total)
        emit(pct, "训练 %s（%d/%d）" % (mt, mi, total))

        # ---- holdout（复现平台口径，参考用）----
        try:
            Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=test_size, random_state=42)
            Xtr, Xte = fold_transform(Xtr, Xte, preprocess)
            m = build_model(mt, kw, X.shape[1], "cpu")
            m.fit(Xtr, ytr)
            ph = _predict_1d(m, Xte)
            rec["holdout"] = {"r2": round(float(r2_score(yte, ph)), 4),
                              "mae": round(float(mean_absolute_error(yte, ph)), 4),
                              "rmse": round(float(np.sqrt(mean_squared_error(yte, ph))), 4)}
        except Exception as exc:                                   # noqa: BLE001
            rec["holdout"] = {"error": str(exc)[:140]}

        # ---- 主指标：打乱 RepeatedKFold ----
        try:
            rkf = RepeatedKFold(n_splits=min(cv_folds, len(y)), n_repeats=cv_repeats,
                                random_state=42)
            r2s, maes, rmses = [], [], []
            for tr, te in rkf.split(X):
                mm = build_model(mt, kw, X.shape[1], "cpu")
                Xtr, Xte = fold_transform(X[tr], X[te], preprocess)
                mm.fit(Xtr, y[tr])
                p = _predict_1d(mm, Xte)
                r2s.append(r2_score(y[te], p))
                maes.append(mean_absolute_error(y[te], p))
                rmses.append(np.sqrt(mean_squared_error(y[te], p)))
            rec["cv_shuffled"] = {
                "r2_mean": round(float(np.mean(r2s)), 4),
                "r2_std": round(float(np.std(r2s)), 4),
                "r2_min": round(float(np.min(r2s)), 4),
                "r2_max": round(float(np.max(r2s)), 4),
                "mae_mean": round(float(np.mean(maes)), 4),
                "rmse_mean": round(float(np.mean(rmses)), 4)}
        except Exception as exc:                                   # noqa: BLE001
            rec["cv_shuffled"] = {"error": str(exc)[:140]}

        # ---- 分组 OOF（按 group_col）----
        if g_arr is not None:
            try:
                logo = list(LeaveOneGroupOut().split(X, y, groups=g_arr))
                oof = np.zeros(len(y))
                for tr, te in logo:
                    mm = build_model(mt, kw, X.shape[1], "cpu")
                    Xtr, Xte = fold_transform(X[tr], X[te], preprocess)
                    mm.fit(Xtr, y[tr])
                    oof[te] = _predict_1d(mm, Xte)
                rec["grouped_oof"] = {
                    "n_groups": int(len(np.unique(g_arr))),
                    "r2": round(float(r2_score(y, oof)), 4),
                    "mae": round(float(mean_absolute_error(y, oof)), 4),
                    "rmse": round(float(np.sqrt(mean_squared_error(y, oof))), 4)}
                rec["_oof"] = [round(float(v), 5) for v in oof]
            except Exception as exc:                               # noqa: BLE001
                rec["grouped_oof"] = {"error": str(exc)[:140]}

        # ---- 预注册切分 ----
        if s_arr is not None and (s_arr == tr_v).any() and (s_arr == te_v).any():
            try:
                m_tr, m_te = s_arr == tr_v, s_arr == te_v
                mm = build_model(mt, kw, X.shape[1], "cpu")
                Xtr, Xte = fold_transform(X[m_tr], X[m_te], preprocess)
                mm.fit(Xtr, y[m_tr])
                p = _predict_1d(mm, Xte)
                rec["fixed_split"] = {
                    "n_train": int(m_tr.sum()), "n_test": int(m_te.sum()),
                    "r2": round(float(r2_score(y[m_te], p)), 4),
                    "mae": round(float(mean_absolute_error(y[m_te], p)), 4),
                    "rmse": round(float(np.sqrt(mean_squared_error(y[m_te], p))), 4)}
            except Exception as exc:                               # noqa: BLE001
                rec["fixed_split"] = {"error": str(exc)[:140]}

        # ---- 全量拟合一次：SHAP / 特征重要度 / 残差诊断 ----
        try:
            mfull = build_model(mt, kw, X.shape[1], "cpu")
            Xfull, _ = fold_transform(X, X, preprocess)
            mfull.fit(Xfull, y)
            pred = _predict_1d(mfull, Xfull)
            rec["fit_all"] = {"r2": round(float(r2_score(y, pred)), 4),
                              "mae": round(float(mean_absolute_error(y, pred)), 4)}
            if hasattr(mfull, "feature_importances_"):
                imp = sorted(zip(keys, map(float, mfull.feature_importances_)),
                             key=lambda z: -z[1])
                rec["feature_importance"] = [[k, round(v, 5)] for k, v in imp]
            if _CFG.get("shap", True) and hasattr(mfull, "feature_importances_"):
                try:
                    import shap
                    ex = shap.Explainer(mfull, Xfull)
                    sv = ex(Xfull)
                    vals = np.asarray(sv.values, dtype=float)
                    if vals.ndim == 3:
                        vals = vals[:, :, -1]
                    mean_abs = np.abs(vals).mean(axis=0)
                    s = sorted(zip(keys, map(float, mean_abs)), key=lambda z: -z[1])
                    rec["shap_importance"] = [[k, round(v, 5)] for k, v in s]
                except Exception as exc:                           # noqa: BLE001
                    rec["shap_error"] = str(exc)[:100]
        except Exception as exc:                                   # noqa: BLE001
            rec["fit_all"] = {"error": str(exc)[:140]}

        rec["seconds"] = round(time.time() - t0, 2)
        results.append(rec)
        cs = rec.get("cv_shuffled") or {}
        go = rec.get("grouped_oof") or {}
        emit(pct, "%s 完成：CV_R2=%s±%s  CV_MAE=%s%s"
             % (mt, cs.get("r2_mean"), cs.get("r2_std"), cs.get("mae_mean"),
                ("  OOF_R2=%s" % go.get("r2")) if go and "r2" in go else ""))

    # ---- 排序（主指标：cv_shuffled.r2_mean；并列看 mae）----
    def sort_key(r):
        c = r.get("cv_shuffled") or {}
        v = c.get("r2_mean")
        return (v is None, -(v if v is not None else -9e9), c.get("mae_mean") or 9e9)

    ranking = [{"rank": i + 1, "model": r["model"],
                "cv_r2": (r.get("cv_shuffled") or {}).get("r2_mean"),
                "cv_r2_std": (r.get("cv_shuffled") or {}).get("r2_std"),
                "cv_mae": (r.get("cv_shuffled") or {}).get("mae_mean"),
                "grouped_oof_r2": (r.get("grouped_oof") or {}).get("r2"),
                "grouped_oof_mae": (r.get("grouped_oof") or {}).get("mae"),
                "fixed_split_r2": (r.get("fixed_split") or {}).get("r2"),
                "fixed_split_mae": (r.get("fixed_split") or {}).get("mae"),
                "holdout_r2": (r.get("holdout") or {}).get("r2")}
               for i, r in enumerate(sorted(results, key=sort_key))]

    # ---- 跨模型 SHAP 聚合（各模型内先归一化再平均，避免量纲差异）----
    shap_pool, used = {}, []
    for r in results:
        s = r.get("shap_importance")
        if not s:
            continue
        used.append(r["model"])
        mx = max(v for _, v in s) or 1.0
        for rank, (k, v) in enumerate(s):
            d = shap_pool.setdefault(k, {"norm": 0.0, "top10": 0})
            d["norm"] += v / mx
            if rank < 10:
                d["top10"] += 1
    shap_agg = sorted(({"feature": k, "norm_shap": round(d["norm"] / len(used), 5),
                        "in_top10": d["top10"]} for k, d in shap_pool.items()),
                      key=lambda z: (-z["norm_shap"], -z["in_top10"])) if used else []
    for r in results:
        r.pop("_oof", None)

    best = ranking[0] if ranking else None
    result = {
        "dataset": ds_name, "n_samples": int(len(y)), "n_features": int(len(keys)),
        "protocol": {"preprocess": preprocess, "preprocessing_scope": "training_fold_only", "test_size": test_size,
                     "cv_folds": cv_folds, "cv_repeats": cv_repeats,
                     "group_col": _CFG.get("group_col"),
                     "fixed_split_col": _CFG.get("fixed_split_col"),
                     "device": "cpu", "n_jobs": n_jobs},
        "baseline": baseline,
        "y_std": round(float(y.std()), 4),
        "n_models": len(results),
        "best_model": (best or {}).get("model"),
        "best_cv_r2": (best or {}).get("cv_r2"),
        "best_cv_mae": (best or {}).get("cv_mae"),
        "ranking": ranking,
        "models": results,
        "shap_aggregate": shap_agg,
        "shap_models_used": used,
        "elapsed_s": round(time.time() - t_start, 1),
    }
    out = _CFG.get("output")
    written = None
    if out:
        try:
            os.makedirs(os.path.dirname(str(out)), exist_ok=True)
            with open(str(out), "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=1)
            written = str(out)
            emit(99, "结果已写出：%s" % out)
        except Exception as exc:                                   # noqa: BLE001
            emit(99, "结果落盘失败：%s" % str(exc)[:100])
    emit(100, "多模型对比完成（%d 个模型，%.1fs）" % (len(results), time.time() - t_start))

    # 落库用轻量结果：完整逐模型明细（SHAP/重要度）留在文件里，
    # 避免 ml_tasks.result 被几百 KB 的逐模型明细撑爆。
    slim = dict(result)
    slim["models"] = [{k: v for k, v in r.items()
                       if k not in ("shap_importance", "feature_importance")} for r in results]
    slim["full_result_path"] = written
    slim["note"] = ("完整结果（含各模型 SHAP / 特征重要度）在 full_result_path；"
                    "本字段为轻量摘要，ranking 可直接用于对比排序")
    print(json.dumps({"ok": True, "result": slim}, ensure_ascii=False))


if __name__ == "__main__":
    main()
