#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多描述符主动学习引擎（供 CataMind 平台 + AI 助手调用）。

与平台原有 AL 的区别
--------------------
`al_train.py` 是 **1 维**的：训练集 = `systems` 表中同时含 `d_band_center` 与
`eta_oer` 的体系，代理模型只看 ε_d 一个描述符。真实项目往往是 **几十维描述符 +
任意目标**（例如 79 个位点电子结构特征 + 8 个配位环境编码 → ΔE_b），
本引擎补上这块：

  · 输入：数据集文件（name + 任意多列特征 + target），可选分组列
  · 代理：GPR（RBF·常数核 + WhiteKernel），子空间标准化
  · 噪声下界：**用分组 OOF 残差自动标定**（而不是拍一个 alpha）——
    否则采集函数会对"组间差异"给出虚假低方差，反复去采同一类样本
  · 采集：UCB(min) / EI(min) / PI(min)，支持 batch_size
  · 验收：留一分组 OOF（R² / MAE / RMSE）+ 置换重要度
  · **AL 模拟**：留一分组贪心采集（每轮重拟合）对比随机采集，给出学习曲线

无候选文件时怎么办
------------------
主动学习需要有"有描述符、无标签"的候选。若没给 `candidate_file`，
本引擎做**留一分组模拟**：把某个母体整体当作"尚未计算"的候选池，
模拟"如果只允许再算 k 个构型，UCB 选 vs 随机选，泛化误差怎么变"。
这是在没有候选池时唯一诚实的 AL 验证方式，也直接支撑"下一批 DFT 该算什么"的决策。

配置
----
{
  "dataset_file": "<CSV 绝对路径>",   "dataset_module": "ml",
  "exclude_cols": ["seed", "composition"],
  "group_col": "parent_id",           "target": "target",
  "method": "gpr_ucb" | "gpr_ei" | "gpr_pi",
  "kappa": 2.0,  "xi": 0.01,  "minimize": true,
  "batch_size": 10,  "noise_floor": null,
  "simulate": true,  "sim_rounds": 8,  "sim_random_repeats": 20,
  "candidate_file": null,
  "output": "<结果 json 路径>"
}

输出：`PROGRESS=<n> <msg>` 进度行 + 最后一行 JSON（与平台其他引擎同形）。
"""
import csv as _csv
import json
import os
import sys
import time

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
os.environ["CUDA_VISIBLE_DEVICES"] = ""

import numpy as np                                        # noqa: E402
from scipy.stats import norm                              # noqa: E402
from sklearn.preprocessing import StandardScaler          # noqa: E402
from sklearn.gaussian_process import GaussianProcessRegressor   # noqa: E402
from sklearn.gaussian_process.kernels import ConstantKernel, RBF, WhiteKernel  # noqa: E402
from sklearn.metrics import r2_score, mean_absolute_error, mean_squared_error  # noqa: E402

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TOOLS)
DB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_PROJECT_ROOT, "catmat.db")


def emit(p, msg):
    print("PROGRESS=%d %s" % (int(p), msg), flush=True)


# --------------------------------------------------------------------------- 数据
def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(_csv.DictReader(fh))


def load_dataset():
    """返回 (names, y, keys, Xraw, groups, aux) 。"""
    p = _CFG.get("dataset_file")
    if not p or not os.path.isfile(str(p)):
        raise SystemExit("数据集文件不存在: %s" % p)
    rows = read_csv(str(p))
    if not rows:
        raise SystemExit("数据集为空")
    tgt = str(_CFG.get("target") or "target")
    if tgt not in rows[0]:
        raise SystemExit("数据集缺少目标列: %s" % tgt)

    excl = {str(c) for c in (_CFG.get("exclude_cols") or [])}
    excl |= {str(_CFG.get("group_col") or "")}
    excl.discard("")
    excl.add(tgt)
    excl.add("name")

    names, y, feats, groups = [], [], [], []
    gcol = _CFG.get("group_col")
    for r in rows:
        nm = (r.get("name") or "").strip()
        tv = r.get(tgt)
        if not nm or tv in (None, ""):
            continue
        try:
            yv = float(tv)
        except ValueError:
            continue
        f = {}
        for k, v in r.items():
            if k in excl:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if np.isfinite(fv):
                f[k] = fv
        names.append(nm)
        y.append(yv)
        feats.append(f)
        groups.append((r.get(gcol) if gcol else None))

    want = _CFG.get("features")
    keys = sorted(set().union(*[set(f.keys()) for f in feats])) if feats else []
    if want:
        keys = [k for k in want if k in keys]
    X = np.array([[f.get(k, np.nan) for k in keys] for f in feats], dtype=float)
    cm = np.nanmean(X, axis=0)
    for j in range(X.shape[1]):
        X[np.isnan(X[:, j]), j] = cm[j]
    return names, np.array(y, dtype=float), keys, X, np.array(groups, dtype=object)


def load_candidates(keys):
    """候选集：只含特征、无标签。列与训练集对齐（缺列填训练均值 → 由调用方处理）。"""
    p = _CFG.get("candidate_file")
    if not p or not os.path.isfile(str(p)):
        return None, None
    rows = read_csv(str(p))
    names, feats = [], []
    for r in rows:
        nm = (r.get("name") or "").strip()
        if not nm:
            continue
        names.append(nm)
        feats.append({k: float(r[k]) for k in keys
                      if k in r and str(r[k]).strip() not in ("",)})
    Xc = np.array([[f.get(k, np.nan) for k in keys] for f in feats], dtype=float)
    return names, Xc


# --------------------------------------------------------------------------- 代理
def make_gpr(n_features, noise=None, seed=42, ard=False):
    """构建 GPR 核。

    默认**各向同性 RBF**（1 个长度尺度）：主动学习内循环要拟合上千次，
    ARD 的 d 个长度尺度会让 L-BFGS 优化成为瓶颈（87 维 × 上千次拟合 → 分钟级到小时级）。
    特征相关性改由**置换重要度**回答（见 main），效果等价且只需几十次拟合。
    需要 ARD 时显式传 ard=True（建议只用于最后那次全量拟合）。
    """
    ls = np.ones(n_features) if ard else 1.0
    lsb = (1e-2, 1e2) if ard else (1e-2, 1e2)
    k = ConstantKernel(1.0, (1e-3, 1e3)) * RBF(length_scale=ls, length_scale_bounds=lsb)
    if noise:
        k = k + WhiteKernel(noise_level=float(noise), noise_level_bounds="fixed")
    return GaussianProcessRegressor(kernel=k, alpha=1e-8, normalize_y=True,
                                    n_restarts_optimizer=0, random_state=seed)


def fit_gpr(Xtr, ytr, noise=None, ard=False):
    m = make_gpr(Xtr.shape[1], noise=noise, ard=ard)
    m.fit(Xtr, ytr)
    return m


# --------------------------------------------------------------------------- 采集
def acquire(mu, sd, method, best_so_far, kappa, xi, minimize):
    """返回采集函数得分（**越大越优先**）。minimize=True 表示目标越小越好。"""
    sd = np.maximum(sd, 1e-9)
    if method.endswith("ucb"):
        return (-mu if minimize else mu) + kappa * sd
    z = (((best_so_far - mu) / sd) if minimize else ((mu - best_so_far) / sd)) - xi
    if method.endswith("ei"):
        return sd * (z * norm.cdf(z) + norm.pdf(z))
    return norm.cdf(z)   # pi


# --------------------------------------------------------------------------- 主流程
def main():
    t0 = time.time()
    names, y, keys, X, groups = load_dataset()
    n, d = X.shape
    if n < 15:
        print(json.dumps({"ok": False, "error": "样本不足（%d），主动学习至少需要 15 条" % n},
                         ensure_ascii=False))
        return
    group_col = _CFG.get("group_col")
    if group_col and (groups is None or all(g is None for g in groups)):
        print(json.dumps({"ok": False, "error": "指定了 group_col=%r 但数据集中无该列的取值"
                          % group_col}, ensure_ascii=False))
        return

    method = str(_CFG.get("method") or "gpr_ucb")
    kappa = float(_CFG.get("kappa", 2.0))
    xi = float(_CFG.get("xi", 0.01))
    minimize = bool(_CFG.get("minimize", True))
    batch = max(1, int(_CFG.get("batch_size", 10)))
    uniq = [g for g in dict.fromkeys(groups)] if group_col else [None]
    emit(5, "数据：%d 样本 × %d 特征；%s；目标越小越好=%s；采集=%s"
         % (n, d, ("分组 %d 组" % len(uniq)) if group_col else "无分组",
            minimize, method))

    sc = StandardScaler().fit(X)
    Xs = sc.transform(X)

    # ---- 1) 噪声下界：按分组 OOF 残差标定 ----
    noise = _CFG.get("noise_floor")
    oof = np.full(n, np.nan)
    if group_col:
        for g in uniq:
            te = groups == g
            tr = ~te
            if te.sum() == 0 or tr.sum() < 10:
                continue
            m = fit_gpr(Xs[tr], y[tr])
            oof[te] = m.predict(Xs[te])
        mask = ~np.isnan(oof)
        if mask.sum() >= 10:
            resid = y[mask] - oof[mask]
            if noise is None:
                noise = float(np.sqrt(np.mean(resid ** 2)))
        else:
            noise = None
    else:
        noise = None
    emit(25, "噪声下界（分组 OOF RMSE）= %s" % ("%.4f" % noise if noise else "未标定（用默认核）"))

    # ---- 2) 全量拟合 + 分组 OOF 验收 ----
    mfull = fit_gpr(Xs, y, noise)
    pred_full = mfull.predict(Xs)
    diag = {
        "fit_all": {"r2": round(float(r2_score(y, pred_full)), 4),
                    "mae": round(float(mean_absolute_error(y, pred_full)), 4)},
        "noise_floor": round(noise, 5) if noise else None,
        "kernel": str(mfull.kernel_),
    }
    if group_col:
        oof_pred = np.full(n, np.nan)
        per_group = []
        for g in uniq:
            te = groups == g
            tr = ~te
            if te.sum() == 0 or tr.sum() < 10:
                continue
            m = fit_gpr(Xs[tr], y[tr], noise)
            oof_pred[te] = m.predict(Xs[te])
            per_group.append({"group": str(g), "n": int(te.sum()),
                              "mae": round(float(mean_absolute_error(y[te], oof_pred[te])), 4),
                              "y_std": round(float(y[te].std()), 3)})
        mk = ~np.isnan(oof_pred)
        diag["grouped_oof"] = {
            "n_groups": int(len(uniq)), "n_used": int(mk.sum()),
            "r2": round(float(r2_score(y[mk], oof_pred[mk])), 4),
            "mae": round(float(mean_absolute_error(y[mk], oof_pred[mk])), 4),
            "rmse": round(float(np.sqrt(mean_squared_error(y[mk], oof_pred[mk]))), 4)}
        diag["grouped_oof_per_group"] = sorted(per_group, key=lambda z: z["mae"])
        # 朴素基线
        base = np.full(n, np.nan)
        for g in uniq:
            te = groups == g
            tr = ~te
            if te.sum() and tr.sum():
                base[te] = y[tr].mean()
        mb = ~np.isnan(base)
        diag["baseline_predict_train_mean"] = {
            "r2": round(float(r2_score(y[mb], base[mb])), 4),
            "mae": round(float(mean_absolute_error(y[mb], base[mb])), 4)}
    emit(45, "代理模型：全量 R²=%.3f，分组 OOF %s"
         % (diag["fit_all"]["r2"],
            ("R²=%.3f / MAE=%.3f" % (diag["grouped_oof"]["r2"], diag["grouped_oof"]["mae"]))
            if "grouped_oof" in diag else "未做（无分组）"))

    # ---- 3) 置换重要度（在分组 OOF 口径下）----
    perm = []
    if group_col and "grouped_oof" in diag:
        rng = np.random.default_rng(42)
        for j, k in enumerate(keys):
            deltas = []
            for _ in range(5):
                Xp = Xs.copy()
                Xp[:, j] = rng.permutation(Xp[:, j])
                pr = np.full(n, np.nan)
                for g in uniq:
                    te = groups == g
                    tr = ~te
                    if te.sum() == 0 or tr.sum() < 10:
                        continue
                    mm = fit_gpr(Xp[tr], y[tr], noise)
                    pr[te] = mm.predict(Xp[te])
                mkk = ~np.isnan(pr)
                deltas.append(mean_absolute_error(y[mkk], pr[mkk])
                              - diag["grouped_oof"]["mae"])
            perm.append([k, round(float(np.mean(deltas)), 5)])
        perm.sort(key=lambda z: -z[1])
    emit(60, "置换重要度完成（前 10：%s）"
         % ", ".join("%s(%+.3f)" % (k, v) for k, v in perm[:10]))

    # ---- 4) AL 模拟：采集候选池 → 在一个**固定的外推母体**上验收 ----
    # 设计：三重分组
    #   test_g  固定测试母体（从不参与训练与采集，误差只在它上面算）
    #   cand_g  候选母体（"尚未计算"的结构，采集函数从中挑）
    #   labeled 其余母体（已有标签）
    # 每轮把采集函数挑中的候选"补算"进 labeled 并重拟合，记录 test_g 上的 MAE。
    # 随机对照：同样的预算与轮数，随机挑候选，重复若干次取均值±std。
    # 这样"AL 是否优于随机"才是一个干净的结论。
    sim = None
    if _CFG.get("simulate", True) and group_col:
        rounds = int(_CFG.get("sim_rounds", 6))
        rrep = int(_CFG.get("sim_random_repeats", 10))
        min_pool = int(_CFG.get("sim_min_pool", 3))
        min_test = int(_CFG.get("sim_min_test", 4))
        al_curves, rnd_curves = [], []
        pairs = 0
        rng = np.random.default_rng(7)
        for test_g in uniq:
            te_idx = np.where(groups == test_g)[0]
            if len(te_idx) < min_test:
                continue
            for cand_g in uniq:
                if cand_g == test_g:
                    continue
                pool_idx = np.where(groups == cand_g)[0]
                if len(pool_idx) < min_pool:
                    continue
                start = np.where((groups != test_g) & (groups != cand_g))[0]
                if len(start) < 10:
                    continue
                pairs += 1
                R = min(rounds, len(pool_idx))
                # 贪心 AL
                labeled, pool = list(start), list(pool_idx)
                curve = []
                for r in range(R + 1):
                    mm = fit_gpr(Xs[labeled], y[labeled], noise)
                    curve.append(float(mean_absolute_error(y[te_idx],
                                                           mm.predict(Xs[te_idx]))))
                    if r == R:
                        break
                    mu, sd = mm.predict(Xs[pool], return_std=True)
                    best = y[labeled].min() if minimize else y[labeled].max()
                    pick = int(np.argmax(acquire(mu, sd, method, best, kappa, xi, minimize)))
                    labeled.append(pool[pick])
                    pool.pop(pick)
                al_curves.append(curve)
                # 随机对照
                for _ in range(rrep):
                    labeled2, pool2 = list(start), list(pool_idx)
                    c2 = []
                    for r in range(R + 1):
                        mm = fit_gpr(Xs[labeled2], y[labeled2], noise)
                        c2.append(float(mean_absolute_error(y[te_idx],
                                                           mm.predict(Xs[te_idx]))))
                        if r == R:
                            break
                        k = int(rng.integers(len(pool2)))
                        labeled2.append(pool2[k])
                        pool2.pop(k)
                    rnd_curves.append(c2)
        if al_curves and rnd_curves:
            nR = min(min(len(c) for c in al_curves), min(len(c) for c in rnd_curves))
            al_mean = [round(float(np.mean([c[i] for c in al_curves])), 4) for i in range(nR)]
            rn_mean = [round(float(np.mean([c[i] for c in rnd_curves])), 4) for i in range(nR)]
            rn_std = [round(float(np.std([c[i] for c in rnd_curves])), 4) for i in range(nR)]
            sim = {
                "design": "heldout_parent",
                "n_pairs": pairs, "rounds": nR, "random_repeats": rrep,
                "al_mae_by_round": al_mean, "random_mae_by_round": rn_mean,
                "random_mae_std_by_round": rn_std,
                "al_gain_vs_random_final": round(rn_mean[-1] - al_mean[-1], 4),
                "al_better_rounds": int(sum(1 for a, b in zip(al_mean, rn_mean) if a < b)),
                "note": ("test 母体从不参与训练与采集，误差只在它上面算（固定测试集）；"
                         "cand 母体扮演'尚未计算'的候选池；labeled = 其余母体。"
                         "每轮挑 1 个候选补算并重拟合。"
                         "al_gain_vs_random_final > 0 表示 AL 比同预算随机采样更好。")}
    emit(85, "AL 模拟：%s" % (json.dumps({k: sim[k] for k in
                                        ("al_mae_by_round", "random_mae_by_round")},
                                        ensure_ascii=False) if sim else "跳过"))

    # ---- 5) 候选推荐 ----
    recs = []
    names_c, Xc = load_candidates(keys)
    if names_c:
        Xcs = sc.transform(Xc)
        mu, sd = mfull.predict(Xcs, return_std=True)
        best = y.min() if minimize else y.max()
        scv = acquire(mu, sd, method, best, kappa, xi, minimize)
        order = np.argsort(-scv)[:batch]
        for i in order:
            recs.append({"name": names_c[i], "pred": round(float(mu[i]), 4),
                         "std": round(float(sd[i]), 4), "acq": round(float(scv[i]), 5)})

    result = {
        "dataset": os.path.basename(str(_CFG.get("dataset_file"))),
        "n_samples": n, "n_features": d, "n_groups": int(len(uniq)) if group_col else None,
        "target": str(_CFG.get("target") or "target"),
        "minimize": minimize,
        "method": method, "kappa": kappa, "xi": xi, "batch_size": batch,
        "diagnostics": diag,
        "permutation_importance": perm,
        "al_simulation": sim,
        "recommendations": recs,
        "n_candidates": int(len(names_c)) if names_c else 0,
        "elapsed_s": round(time.time() - t0, 1),
        "note": ("无 candidate_file 时做留一分组 AL 模拟；"
                 "有候选集时按采集函数给出下一批推荐"),
    }
    out = _CFG.get("output")
    if out:
        try:
            os.makedirs(os.path.dirname(str(out)), exist_ok=True)
            with open(str(out), "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=1)
            emit(99, "结果已写出：%s" % out)
        except Exception as exc:                                    # noqa: BLE001
            emit(99, "结果落盘失败：%s" % str(exc)[:100])
    emit(100, "主动学习完成（%.1fs）" % (time.time() - t0))
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
