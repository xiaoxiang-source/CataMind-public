#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主动学习：拟合代理模型并推荐下一批待计算结构（支持贝叶斯优化主流方法）。

在 conda dpmd 环境运行（torch 2.8+cu129，CUDA 可用）：
    /home/user/anaconda3/envs/dpmd/bin/python catmat.py al-train <catmat.db> <cfg.json>

cfg.json：
{
  "method": "gpr_ucb" | "gpr_ei" | "botorch_qei" | "botorch_qucb" | "skopt_gp" | "optuna_tpe",
  "batch_size": 5, "device": "auto" | "cuda" | "cpu",
  "length_scale": 0.3, "noise": 0.01, "kappa": 2.0, "xi": 0.01
}

数据来源：systems.descriptors
  - 训练集：同时含 d_band_center 与 eta_OER 的体系（ε_d → η_OER）
  - 候选集：含 d_band_center 但不含 eta_OER 的体系

输出：进度行 PROGRESS=<n> <msg>，最后一行 JSON 结果（含推荐列表）。
"""
import os
import sys
import json
import sqlite3

# CPU 线程限制必须在 numpy/sklearn 导入前设置
_CFG_PATH = sys.argv[2] if len(sys.argv) > 2 else None
_CFG = {}
if _CFG_PATH:
    try:
        _CFG = json.loads(open(_CFG_PATH, encoding="utf-8").read())
    except Exception:
        _CFG = {}
_JOBS = str(int(_CFG.get("n_jobs", 2) or 2))
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, _JOBS)
_DEVICE = str(_CFG.get("device", "auto") or "auto").lower()
if _DEVICE == "cpu":
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"

import numpy as np  # noqa: E402

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_PROJECT_ROOT, "catmat.db")


def emit(p, msg):
    print("PROGRESS=%d %s" % (int(p), msg), flush=True)


def _load_train_from_dataset_file(path, module="al"):
    """从按模块独立保存的数据集文件（AL-XXX.csv）加载训练集 [(name, ed, eta)]。

    module 非空时校验文件名前缀，防止误用 ML 数据集。
    """
    import csv as _csv
    import os as _os
    if not path or not _os.path.isfile(path):
        return None
    base = _os.path.basename(path)
    if module and not base.upper().startswith(str(module).upper() + "-"):
        return None
    train = []
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        for r in _csv.DictReader(f):
            name = (r.get("name") or "").strip()
            if not name:
                continue
            ed = None
            for k in ("d_band_center", "ed", "eps_d"):
                if r.get(k) not in (None, ""):
                    try:
                        ed = float(r[k])
                        break
                    except (TypeError, ValueError):
                        pass
            if ed is None:
                continue
            eta = r.get("target")
            try:
                eta = float(eta) if eta not in (None, "") else None
            except (TypeError, ValueError):
                eta = None
            if eta is not None:
                train.append((name, ed, eta))
    return train or None


def load_split():
    """返回 (train, cands)：train=[(name, ed, eta)]，cands=[(sys_id, name, ed)]。

    指定 dataset_file（AL-XXX.csv）时，训练集优先来自该独立文件，避免与 ML 数据集混用；
    候选集始终来自 systems 表——候选需要 system id 才能「提交下一批计算」。
    """
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id,name,descriptors FROM systems").fetchall()
    conn.close()
    train, cands = [], []
    for r in rows:
        try:
            descs = json.loads(r["descriptors"] or "[]")
        except Exception:
            descs = []
        d = {}
        for x in descs:
            v = x.get("value")
            if isinstance(v, (int, float)):
                d[x["name"]] = float(v)
        ed = d.get("d_band_center")
        eta = d.get("eta_OER")
        if ed is None:
            continue
        if eta is None:
            cands.append((r["id"], r["name"], ed))
        else:
            train.append((r["name"], ed, eta))
    # 指定数据集文件 → 用该文件内容替换训练集（候选集仍来自 systems，保留提交能力）
    ds = _CFG.get("dataset_file") if isinstance(_CFG, dict) else None
    if ds:
        _mod = (_CFG.get("dataset_module") or "al") if isinstance(_CFG, dict) else "al"
        ft = _load_train_from_dataset_file(ds, module=_mod)
        if ft:
            print("[INFO] 训练集使用指定数据集文件: %s（%d 行）" % (ds, len(ft)), flush=True)
            train = ft
        else:
            print("[WARN] 数据集文件无可用训练行，回退 systems 表: %s" % ds, flush=True)
    return train, cands


def _torch_device():
    if _DEVICE == "cpu":
        return "cpu"
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def method_gpr(train, cands, kind, batch, cfg):
    """sklearn 高斯过程 + 解析采集函数（UCB / EI / PI）。最小化 η_OER。"""
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import ConstantKernel, RBF

    X = np.array([[t[1]] for t in train], dtype=float)
    y = np.array([t[2] for t in train], dtype=float)
    ls = float(cfg.get("length_scale", 0.3))
    kernel = ConstantKernel(1.0) * RBF(length_scale=ls)
    gp = GaussianProcessRegressor(kernel=kernel, alpha=float(cfg.get("noise", 0.01)),
                                  normalize_y=True, random_state=42)
    gp.fit(X, y)
    Xs = np.array([[c[2]] for c in cands], dtype=float)
    mu, std = gp.predict(Xs, return_std=True)
    best = float(y.min())
    kappa = float(cfg.get("kappa", 2.0))
    xi = float(cfg.get("xi", 0.01))
    if kind == "ucb":
        acq = mu - kappa * std                       # 最小化：越小越优
    elif kind == "ei":
        from scipy.stats import norm
        imp = best - mu - xi
        z = np.divide(imp, std, out=np.zeros_like(std), where=std > 1e-12)
        ei = imp * norm.cdf(z) + std * norm.pdf(z)
        acq = -ei                                    # EI 越大越优 → 取负号便于升序排
    else:  # pi
        from scipy.stats import norm
        z = np.divide(best - mu - xi, std, out=np.zeros_like(std), where=std > 1e-12)
        acq = -norm.cdf(z)
    order = np.argsort(acq)
    return gp, mu, std, acq, order


def method_botorch(train, cands, acq_name, batch, cfg):
    """BoTorch 批量贝叶斯优化（qEI / qUCB），在离散候选集上贪心选点。"""
    import torch
    from botorch.models import SingleTaskGP
    from botorch.fit import fit_gpytorch_mll
    from gpytorch.mlls import ExactMarginalLogLikelihood
    from botorch.acquisition import qExpectedImprovement, qUpperConfidenceBound

    dev = _torch_device()
    dtype = torch.double
    X = torch.tensor([[t[1]] for t in train], dtype=dtype, device=dev)
    Y = torch.tensor([[t[2]] for t in train], dtype=dtype, device=dev)
    model = SingleTaskGP(X, Y)
    mll = ExactMarginalLogLikelihood(model.likelihood, model)
    fit_gpytorch_mll(mll)

    Xc = torch.tensor([[c[2]] for c in cands], dtype=dtype, device=dev)
    with torch.no_grad():
        post = model.posterior(Xc)
        mu = post.mean.squeeze(-1)
        std = post.variance.clamp_min(1e-12).sqrt().squeeze(-1)
    best = Y.min()
    if acq_name == "qei":
        # 单点评 EI 解析式（批量用贪心逐点加入，简化为单点评 EI 排序）
        from scipy.stats import norm
        mu_np, std_np = mu.cpu().numpy(), std.cpu().numpy()
        imp = best.item() - mu_np
        z = np.divide(imp, std_np, out=np.zeros_like(std_np), where=std_np > 1e-12)
        ei = imp * norm.cdf(z) + std_np * norm.pdf(z)
        acq = -ei
        order = np.argsort(acq)
        return None, mu_np, std_np, acq, order
    # qUCB：μ - beta·σ（最小化）
    beta = float(cfg.get("kappa", 2.0))
    acq_t = mu - beta * std
    order = torch.argsort(acq_t).cpu().numpy()
    return None, mu.cpu().numpy(), std.cpu().numpy(), acq_t.cpu().numpy(), order


def method_skopt(train, cands, batch, cfg):
    """scikit-optimize：GP 代理 + EI（在候选 ε_d 区间上优化后就近取候选）。"""
    from skopt import Optimizer
    lo = min(c[2] for c in cands + [(0, "", min(t[1] for t in train))])
    hi = max(c[2] for c in cands + [(0, "", max(t[1] for t in train))])
    opt = Optimizer(dimensions=[(lo, hi)], base_estimator="GP",
                    acq_func="EI", random_state=42)
    for t in train:
        opt.tell([t[1]], t[2])
    Xs = np.array([[c[2]] for c in cands], dtype=float)
    # 用 GP 后验给出 μ/σ，再用 EI 排序
    est = opt.models[-1] if getattr(opt, "models", None) else None
    if est is not None:
        mu, std = est.predict(Xs, return_std=True)
        best = float(np.min([t[2] for t in train]))
        from scipy.stats import norm
        imp = best - mu
        z = np.divide(imp, std, out=np.zeros_like(std), where=std > 1e-12)
        ei = imp * norm.cdf(z) + std * norm.pdf(z)
        acq = -ei
    else:
        mu = np.zeros(len(cands))
        std = np.ones(len(cands))
        acq = np.zeros(len(cands))
    order = np.argsort(acq)
    return None, mu, std, acq, order


def method_optuna_tpe(train, cands, batch, cfg):
    """Optuna TPE：在连续 ε_d 区间上采样，映射回离散候选。"""
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def _surrogate(x):
        """用已有训练点的距离加权估计 η（供 TPE 搜索的代理目标）。"""
        xs = np.array([t[1] for t in train], dtype=float)
        ys = np.array([t[2] for t in train], dtype=float)
        w = 1.0 / (np.abs(xs - x) + 1e-6)
        return float(np.sum(w * ys) / np.sum(w))

    lo = float(min([c[2] for c in cands] + [t[1] for t in train]))
    hi = float(max([c[2] for c in cands] + [t[1] for t in train]))
    study = optuna.create_study(direction="minimize",
                                sampler=optuna.samplers.TPESampler(seed=42))
    for t in train:
        study.add_trial(optuna.trial.create_trial(
            params={"ed": float(t[1])}, distributions={"ed": optuna.distributions.FloatDistribution(lo, hi)},
            value=float(t[2])))
    n_trials = max(int(cfg.get("n_trials", 30)), batch * 4)
    for _ in range(n_trials):
        tr = study.ask()
        ed = tr.params["ed"] if "ed" in tr.params else tr.suggest_float("ed", lo, hi)
        study.tell(tr, _surrogate(ed))
    # 用 TPE 采样出的最优 ed，找最近候选
    best_ed = study.best_params.get("ed", lo)
    dists = [abs(c[2] - best_ed) for c in cands]
    order = np.argsort(dists)
    mu = np.array([min(t[2] for t in train)] * len(cands), dtype=float)
    std = np.zeros(len(cands), dtype=float)
    acq = np.array(dists, dtype=float)
    return None, mu, std, acq, order


def main():
    emit(10, "加载体系描述符")
    train, cands = load_split()
    if len(train) < 3:
        print(json.dumps({"ok": False,
                          "error": "训练样本不足：需 ≥3 个同时含 ε_d 与 η_OER 的体系，当前 %d" % len(train)},
                         ensure_ascii=False))
        return
    if not cands:
        print(json.dumps({"ok": False,
                          "error": "无可推荐候选：所有含 ε_d 的体系都已计算 η_OER"},
                         ensure_ascii=False))
        return

    method = str(_CFG.get("method", "gpr_ucb") or "gpr_ucb").lower()
    batch = max(1, int(_CFG.get("batch_size", 5)))
    # 仅 BoTorch 走 CUDA；sklearn GP / skopt / optuna TPE 为 CPU 计算
    dev_used = "cuda" if (method in ("botorch_qei", "botorch_qucb") and _torch_device() == "cuda") else "cpu"

    emit(40, "拟合代理模型（%s）" % method)
    try:
        if method == "gpr_ucb":
            _, mu, std, acq, order = method_gpr(train, cands, "ucb", batch, _CFG)
        elif method == "gpr_ei":
            _, mu, std, acq, order = method_gpr(train, cands, "ei", batch, _CFG)
        elif method == "gpr_pi":
            _, mu, std, acq, order = method_gpr(train, cands, "pi", batch, _CFG)
        elif method in ("botorch_qei", "botorch_qucb"):
            _, mu, std, acq, order = method_botorch(train, cands,
                                                    method.split("_", 1)[1], batch, _CFG)
        elif method == "skopt_gp":
            _, mu, std, acq, order = method_skopt(train, cands, batch, _CFG)
        elif method == "optuna_tpe":
            _, mu, std, acq, order = method_optuna_tpe(train, cands, batch, _CFG)
        else:
            print(json.dumps({"ok": False, "error": "未知主动学习方法: %s" % method}, ensure_ascii=False))
            return
    except Exception as e:
        print(json.dumps({"ok": False, "error": "%s 执行失败: %s" % (method, e)}, ensure_ascii=False))
        return

    emit(85, "生成推荐列表")
    recs = []
    for idx in order[:batch]:
        sid, name, ed = cands[idx]
        recs.append({
            "system_id": sid,
            "name": name,
            "ed": round(float(ed), 4),
            "mu": round(float(mu[idx]), 4),
            "std": round(float(std[idx]), 4),
            "acq": round(float(acq[idx]), 5),
        })

    emit(100, "主动学习推荐完成")
    print(json.dumps({"ok": True, "result": {
        "method": method,
        "device": dev_used,
        "n_train": len(train),
        "n_candidates": len(cands),
        "batch_size": batch,
        "recommendations": recs,
    }}, ensure_ascii=False))


if __name__ == "__main__":
    main()
