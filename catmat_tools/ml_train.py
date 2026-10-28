#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 ml_samples 上训练回归模型（预测 η_OER），支持 GPU 双卡并行。

在 conda dpmd 环境运行（torch 2.8+cu129，2×RTX 4090 可用）：
    /home/user/anaconda3/envs/dpmd/bin/python catmat.py ml-train <catmat.db> <cfg.json>
    /home/user/anaconda3/envs/dpmd/bin/python catmat.py ml-train <db> <cfg> --cv-group 0 --cv-groups 2

GPU 模型：XGBoost / LightGBM（device=cuda，LightGBM 自动探测 CUDA 构建）、
PyTorch MLP（mlp_torch）、GPyTorch GPR（gpr_torch）；device=cuda 时交叉验证
按 GPU 分组并行（每组子进程绑定一张卡），并限制 CPU 线程数以降低占用。

cfg.json：
{
  "model": "xgb" | "rf" | ..., "device": "auto" | "cpu" | "cuda" | "cuda:0" | "cuda:1",
  "preprocess": true, "test_size": 0.2, "cv_folds": 5,
  "params": {"n_estimators": 300, "max_depth": null, "n_jobs": 2, ...}
}

输出：进度行 `PROGRESS=<n> <msg>` / `LOG=<text>`，最后一行 JSON 结果。
"""
import os
import sys
import json
import subprocess

# ---------------------------------------------------------------------------
# CPU / GPU 环境设置：必须在 numpy / sklearn 导入之前完成（线程数在导入时确定）
# ---------------------------------------------------------------------------
CFG_PATH = sys.argv[2] if len(sys.argv) > 2 else None
_CFG = {}
if CFG_PATH:
    try:
        _CFG = json.loads(open(CFG_PATH, encoding="utf-8").read())
    except Exception:
        _CFG = {}

_HP = _CFG.get("params") or {}
_JOBS = str(int(_HP.get("n_jobs", 2) or 2))
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, _JOBS)

_DEVICE = str(_CFG.get("device", "auto") or "auto").lower()


def _detect_gpus():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used",
             "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
        gpus = []
        for line in (out.stdout or "").strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 4:
                gpus.append({"index": int(p[0]), "name": p[1],
                             "mem_total": p[2], "mem_used": p[3]})
        return gpus
    except Exception:
        return []


GPUS = _detect_gpus()
if _DEVICE == "cpu" or not GPUS:
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    _USE_GPU = False
elif _DEVICE.startswith("cuda:"):
    os.environ["CUDA_VISIBLE_DEVICES"] = _DEVICE.split(":", 1)[1]
    _USE_GPU = True
else:  # cuda / auto
    os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(str(g["index"]) for g in GPUS)
    _USE_GPU = True

import sqlite3  # noqa: E402

import numpy as np  # noqa: E402
from sklearn.ensemble import RandomForestRegressor  # noqa: E402
from sklearn.model_selection import cross_val_score, train_test_split, KFold  # noqa: E402
from sklearn.metrics import mean_absolute_error, r2_score  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402
from sklearn.linear_model import (LinearRegression, Ridge, Lasso, ElasticNet,  # noqa: E402
                                  BayesianRidge, HuberRegressor)
from sklearn.svm import SVR  # noqa: E402
from sklearn.neighbors import KNeighborsRegressor  # noqa: E402
from sklearn.tree import DecisionTreeRegressor  # noqa: E402
from sklearn.ensemble import (ExtraTreesRegressor, GradientBoostingRegressor,  # noqa: E402
                              AdaBoostRegressor, HistGradientBoostingRegressor)
from sklearn.neural_network import MLPRegressor  # noqa: E402
from sklearn.kernel_ridge import KernelRidge  # noqa: E402
from sklearn.cross_decomposition import PLSRegression  # noqa: E402

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_PROJECT_ROOT, "catmat.db")
MIN_SAMPLES = 5


def emit(progress, msg):
    print("PROGRESS=%d %s" % (int(progress), msg), flush=True)


_LGBM_CUDA_OK = None


def _lgbm_gpu_ok():
    """探测当前 LightGBM 构建是否支持 CUDA（pip 版默认未编译 CUDA）。结果缓存。"""
    global _LGBM_CUDA_OK
    if _LGBM_CUDA_OK is None:
        import lightgbm as lgb
        try:
            m = lgb.LGBMRegressor(n_estimators=1, device="cuda", n_jobs=1, verbose=-1)
            m.fit(np.random.rand(8, 2).astype(np.float32), np.random.rand(8).astype(np.float32))
            _LGBM_CUDA_OK = True
        except Exception:
            _LGBM_CUDA_OK = False
    return _LGBM_CUDA_OK


def build_model(model_type, hp, n_features, device="cpu"):
    """构造回归器。XGBoost 在 GPU 可用时用 device='cuda'。"""
    hp = hp or {}

    def _i(key, default):
        v = hp.get(key)
        return int(v) if v not in (None, "") else default

    def _f(key, default):
        v = hp.get(key)
        return float(v) if v not in (None, "") else default

    md = _i("max_depth", None) if hp.get("max_depth") not in (None, "") else None
    nj = max(1, _i("n_jobs", 2))

    if model_type == "linear":
        return LinearRegression(n_jobs=nj)
    if model_type == "ridge":
        return Ridge(alpha=_f("alpha", 1.0))
    if model_type == "lasso":
        return Lasso(alpha=_f("alpha", 0.1), max_iter=10000)
    if model_type == "elasticnet":
        return ElasticNet(alpha=_f("alpha", 0.1), l1_ratio=_f("l1_ratio", 0.5), max_iter=10000)
    if model_type == "bayesian_ridge":
        return BayesianRidge()
    if model_type == "huber":
        return HuberRegressor(epsilon=_f("epsilon", 1.35))
    if model_type == "svr":
        return SVR(C=_f("C", 1.0), epsilon=_f("epsilon", 0.1))
    if model_type == "knn":
        return KNeighborsRegressor(n_neighbors=_i("n_neighbors", 5), n_jobs=nj)
    if model_type == "decision_tree":
        return DecisionTreeRegressor(max_depth=md, random_state=42)
    if model_type == "extra_trees":
        mf = hp.get("max_features", 1.0)
        if mf == "":
            mf = 1.0
        elif isinstance(mf, str) and mf not in ("sqrt", "log2"):
            mf = float(mf)
        return ExtraTreesRegressor(n_estimators=_i("n_estimators", 300), max_depth=md,
                                   max_features=mf,
                                   min_samples_split=_i("min_samples_split", 2),
                                   min_samples_leaf=_i("min_samples_leaf", 1),
                                   n_jobs=nj, random_state=42)
    if model_type == "gradient_boosting":
        return GradientBoostingRegressor(n_estimators=_i("n_estimators", 200),
                                         learning_rate=_f("learning_rate", 0.1),
                                         max_depth=md or 3, random_state=42)
    if model_type == "adaboost":
        return AdaBoostRegressor(n_estimators=_i("n_estimators", 200), random_state=42)
    if model_type == "hist_gradient_boosting":
        return HistGradientBoostingRegressor(max_iter=_i("n_estimators", 200), random_state=42)
    if model_type == "mlp":
        return MLPRegressor(hidden_layer_sizes=(64, 32), max_iter=_i("n_estimators", 1000),
                            learning_rate_init=_f("learning_rate", 0.001), random_state=42)
    if model_type == "kernel_ridge":
        return KernelRidge(alpha=_f("alpha", 1.0), kernel="rbf")
    if model_type == "pls":
        return PLSRegression(n_components=min(_i("n_components", 2), max(1, n_features - 1)))
    if model_type == "xgb":
        from xgboost import XGBRegressor
        return XGBRegressor(n_estimators=_i("n_estimators", 200),
                            learning_rate=_f("learning_rate", 0.1),
                            max_depth=md or 6,
                            reg_alpha=_f("reg_alpha", 0), reg_lambda=_f("reg_lambda", 1),
                            subsample=_f("subsample", 1.0),
                            tree_method="hist",
                            device=("cuda" if device.startswith("cuda") else "cpu"),
                            n_jobs=nj, random_state=42)
    if model_type == "lgbm":
        import lightgbm as lgb
        kwargs = {"verbose": -1}
        if device.startswith("cuda") and _lgbm_gpu_ok():
            kwargs["device"] = "cuda"  # LightGBM 4.x：GPU 训练（需 CUDA 编译版）
        else:
            emit(0, "LightGBM 构建不支持 CUDA，回退 CPU 训练")
        return lgb.LGBMRegressor(n_estimators=_i("n_estimators", 200),
                                 learning_rate=_f("learning_rate", 0.1), n_jobs=nj,
                                 random_state=42, **kwargs)
    if model_type == "catboost":
        from catboost import CatBoostRegressor
        return CatBoostRegressor(iterations=_i("n_estimators", 200),
                                 learning_rate=_f("learning_rate", 0.1), verbose=0,
                                 thread_count=nj, random_state=42)
    if model_type == "gpr":
        from sklearn.gaussian_process import GaussianProcessRegressor
        from sklearn.gaussian_process.kernels import ConstantKernel, RBF
        kernel = ConstantKernel(1.0) * RBF(length_scale=_f("length_scale", 1.0))
        return GaussianProcessRegressor(kernel=kernel, alpha=_f("alpha", 1e-6),
                                        normalize_y=True, random_state=42)
    if model_type == "mlp_torch":
        return TorchMLPRegressor(hidden=(int(_i("hidden_1", 64)), int(_i("hidden_2", 32))),
                                 lr=_f("learning_rate", 1e-3),
                                 epochs=int(_i("n_estimators", 400)),
                                 batch=int(_i("batch_size", 32)))
    if model_type == "gpr_torch":
        return GPyTorchGPR(length_scale=_f("length_scale", 1.0),
                           noise=_f("alpha", 1e-4),
                           lr=_f("learning_rate", 0.1),
                           iters=int(_i("n_estimators", 300)))
    # 默认随机森林
    mf = hp.get("max_features")
    if mf not in ("", "sqrt", "log2", None):
        try:
            mf = float(mf)
        except (TypeError, ValueError):
            mf = "sqrt"
    return RandomForestRegressor(
        n_estimators=_i("n_estimators", 300), max_depth=md,
        min_samples_split=_i("min_samples_split", 2),
        min_samples_leaf=_i("min_samples_leaf", 1),
        max_features=mf if mf else "sqrt", n_jobs=nj, random_state=42)


class TorchMLPRegressor:
    """PyTorch MLP 回归器（CUDA 加速训练），sklearn 风格 fit/predict 接口。

    GPU 可用性由 CUDA_VISIBLE_DEVICES（ml_train.py 顶部已按 device 参数设置）
    控制，双卡并行 CV 时子进程各自只看到绑定的那张卡。
    """

    def __init__(self, hidden=(64, 32), lr=1e-3, epochs=400, batch=32, seed=42):
        self.hidden = tuple(hidden)
        self.lr = lr
        self.epochs = int(epochs)
        self.batch = int(batch)
        self.seed = seed
        self.dev_ = "cpu"
        self.mean_ = None
        self.std_ = None

    def fit(self, X, y):
        import torch
        from torch import nn
        self.dev_ = "cuda" if torch.cuda.is_available() else "cpu"
        X = np.asarray(X, dtype=np.float32)
        y = np.asarray(y, dtype=np.float32).reshape(-1, 1)
        self.mean_ = X.mean(axis=0, keepdims=True)
        self.std_ = X.std(axis=0, keepdims=True) + 1e-8
        Xs = (X - self.mean_) / self.std_
        Xt = torch.tensor(Xs, dtype=torch.float32, device=self.dev_)
        yt = torch.tensor(y, dtype=torch.float32, device=self.dev_)
        torch.manual_seed(self.seed)
        dims = [Xt.shape[1]] + list(self.hidden) + [1]
        layers = []
        for i in range(len(dims) - 1):
            layers.append(nn.Linear(dims[i], dims[i + 1]))
            if i < len(dims) - 2:
                layers.append(nn.ReLU())
        net = nn.Sequential(*layers).to(self.dev_)
        opt = torch.optim.Adam(net.parameters(), lr=self.lr)
        n = Xt.shape[0]
        for ep in range(self.epochs):
            perm = torch.randperm(n, device=self.dev_)
            tot = 0.0
            nb = 0
            for i in range(0, n, self.batch):
                idx = perm[i:i + self.batch]
                opt.zero_grad()
                loss = ((net(Xt[idx]) - yt[idx]) ** 2).mean()
                loss.backward()
                opt.step()
                tot += float(loss)
                nb += 1
            if ep % 50 == 0:
                emit(0, "MLP epoch %d/%d loss=%.5f (%s)" % (ep + 1, self.epochs, tot / max(1, nb), self.dev_))
        self.net_ = net
        return self

    def predict(self, X):
        import torch
        X = np.asarray(X, dtype=np.float32)
        Xs = (X - self.mean_) / self.std_
        with torch.no_grad():
            out = self.net_(torch.tensor(Xs, dtype=torch.float32, device=self.dev_))
        return out.cpu().numpy().reshape(-1)


class GPyTorchGPR:
    """GPyTorch 精确高斯过程回归（CUDA 加速），sklearn 风格接口。"""

    def __init__(self, length_scale=1.0, noise=1e-4, lr=0.1, iters=300, seed=42):
        self.length_scale = float(length_scale)
        self.noise = float(noise)
        self.lr = float(lr)
        self.iters = int(iters)
        self.seed = seed
        self.dev_ = "cpu"
        self.model_ = None
        self.likelihood_ = None

    def fit(self, X, y):
        import torch
        import gpytorch
        self.dev_ = "cuda" if torch.cuda.is_available() else "cpu"
        torch.manual_seed(self.seed)
        X = np.asarray(X, dtype=np.float32)
        y = np.asarray(y, dtype=np.float32)
        Xt = torch.tensor(X, dtype=torch.float32, device=self.dev_)
        yt = torch.tensor(y, dtype=torch.float32, device=self.dev_)

        class ExactGPModel(gpytorch.models.ExactGP):
            def __init__(self, xt, yt_, likelihood):
                super().__init__(xt, yt_, likelihood)
                self.mean_module = gpytorch.means.ConstantMean()
                self.covar_module = gpytorch.kernels.ScaleKernel(
                    gpytorch.kernels.RBFKernel())

            def forward(self, x):
                return gpytorch.distributions.MultivariateNormal(
                    self.mean_module(x), self.covar_module(x))

        likelihood = gpytorch.likelihoods.GaussianLikelihood()
        model = ExactGPModel(Xt, yt, likelihood)
        model.to(self.dev_)
        likelihood.to(self.dev_)
        model.covar_module.base_kernel.lengthscale = self.length_scale
        likelihood.noise = self.noise
        mll = gpytorch.mlls.ExactMarginalLogLikelihood(likelihood, model)
        opt = torch.optim.Adam(model.parameters(), lr=self.lr)
        model.train()
        likelihood.train()
        for it in range(self.iters):
            opt.zero_grad()
            out = model(Xt)
            loss = -mll(out, yt)
            loss.backward()
            opt.step()
            if it % 50 == 0:
                emit(0, "GPR iter %d/%d loss=%.3f (%s)" % (it + 1, self.iters, float(loss), self.dev_))
        self.model_, self.likelihood_ = model, likelihood
        return self

    def predict(self, X):
        import torch
        import gpytorch
        X = np.asarray(X, dtype=np.float32)
        Xt = torch.tensor(X, dtype=torch.float32, device=self.dev_)
        self.model_.eval()
        self.likelihood_.eval()
        with torch.no_grad(), gpytorch.settings.fast_pred_var():
            pred = self.likelihood_(self.model_(Xt))
        return pred.mean.cpu().numpy().reshape(-1)


def _predict_1d(model, X):
    p = np.asarray(model.predict(X), dtype=float)
    return p[:, 0] if p.ndim > 1 else p


def _load_dataset_file_rows(path, module=None):
    """从按模块独立保存的数据集文件（ML-XXX.csv / AL-XXX.csv）加载样本。

    返回与 ml_samples 表等价的字典列表（features 为 JSON 字符串、target 为 float），
    以便完全复用后续的特征解析逻辑。module 非空时校验文件名前缀，防止跨模块串数据。
    """
    import csv as _csv
    import os as _os
    if not path or not _os.path.isfile(path):
        return None
    base = _os.path.basename(path)
    if module and not base.upper().startswith(str(module).upper() + "-"):
        return None
    out = []
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        for r in _csv.DictReader(f):
            name = (r.get("name") or "").strip()
            if not name:
                continue
            tgt = r.get("target")
            try:
                tgt = float(tgt) if tgt not in (None, "") else None
            except (TypeError, ValueError):
                tgt = None
            if tgt is None:          # 与 SELECT ... WHERE target IS NOT NULL 保持一致
                continue
            feats = {k: v for k, v in r.items() if k not in ("name", "target")}
            out.append({"name": name, "features": json.dumps(feats, ensure_ascii=False),
                        "target": tgt, "source": base})
    return out or None


def _load_data():
    """取数优先级：cfg 指定的数据集文件 > ml_samples 全表（旧行为）。

    背景：ML 与 AL 的导入此前都写入同一张 ml_samples 表，全表取数会把两个模块的数据
    混在一起训练。改为按模块独立落盘（ML-XXX.csv / AL-XXX.csv）后，这里优先加载
    训练任务显式选择的数据集文件，未指定时才回退到全表。
    """
    ds = _CFG.get("dataset_file") if isinstance(_CFG, dict) else None
    if ds:
        module = (_CFG.get("dataset_module") or "ml") if isinstance(_CFG, dict) else None
        rows = _load_dataset_file_rows(ds, module=module)
        if rows:
            print("[INFO] 使用指定数据集文件: %s（%d 行）" % (ds, len(rows)), flush=True)
            return rows
        print("[WARN] 数据集文件不可用，回退 ml_samples 全表: %s" % ds, flush=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM ml_samples WHERE target IS NOT NULL").fetchall()
    conn.close()
    return rows


def _cv_scores_for_group(model_type, hp, X, y, folds, group_idx, n_groups, device):
    """计算分配给本组的 CV 折分数。"""
    scores = []
    for fi, (tr, te) in enumerate(folds):
        if fi % n_groups != group_idx:
            continue
        m = build_model(model_type, hp, X.shape[1], device)
        m.fit(X[tr], y[tr])
        s = r2_score(y[te], _predict_1d(m, X[te]))
        if np.isfinite(s):
            scores.append(float(s))
    return scores


def run_cv_group_mode():
    """子进程模式：只跑属于本组的 CV 折，输出 JSON。"""
    g = int(sys.argv[sys.argv.index("--cv-group") + 1])
    n = int(sys.argv[sys.argv.index("--cv-groups") + 1])
    rows = _load_data()
    feats_list = [json.loads(r["features"] or "{}") for r in rows]
    keys = sorted(set().union(*[set(f.keys()) for f in feats_list]))
    X = np.array([[f.get(k, np.nan) for k in keys] for f in feats_list], dtype=float)
    y = np.array([r["target"] for r in rows], dtype=float)
    cm = np.nanmean(X, axis=0)
    for j in range(X.shape[1]):
        X[np.isnan(X[:, j]), j] = cm[j]
    model_type = _CFG.get("model", "rf")
    hp = _CFG.get("params", {}) or {}
    preprocess = bool(_CFG.get("preprocess", model_type in ("gpr", "gpr_torch")))
    if preprocess:
        X = StandardScaler().fit_transform(X)
    cv_folds = int(_CFG.get("cv_folds", 5))
    kf = KFold(n_splits=min(cv_folds, len(rows)), shuffle=True, random_state=42)
    folds = list(kf.split(X))
    dev = "cuda" if (_DEVICE.startswith("cuda") and GPUS) else "cpu"
    scores = _cv_scores_for_group(model_type, hp, X, y, folds, g, n, dev)
    print(json.dumps({"ok": True, "group": g, "scores": scores}))


def main():
    if "--cv-group" in sys.argv:
        return run_cv_group_mode()

    emit(5, "加载数据集")
    rows = _load_data()
    if len(rows) < MIN_SAMPLES:
        print(json.dumps({"ok": False,
                          "error": "样本不足：需 ≥%d 个含 η_OER 目标的样本，当前 %d"
                                   % (MIN_SAMPLES, len(rows))}, ensure_ascii=False))
        return

    feats_list = [json.loads(r["features"] or "{}") for r in rows]
    keys = sorted(set().union(*[set(f.keys()) for f in feats_list]))
    X = np.array([[f.get(k, np.nan) for k in keys] for f in feats_list], dtype=float)
    y = np.array([r["target"] for r in rows], dtype=float)
    cm = np.nanmean(X, axis=0)
    for j in range(X.shape[1]):
        X[np.isnan(X[:, j]), j] = cm[j]

    model_type = _CFG.get("model", "rf")
    hp = _CFG.get("params", {}) or {}
    preprocess = bool(_CFG.get("preprocess", model_type in ("gpr", "gpr_torch")))
    test_size = float(_CFG.get("test_size", 0.2))
    cv_folds = int(_CFG.get("cv_folds", 5))
    # 支持 GPU 加速的模型：XGBoost / LightGBM（原生 GPU），torch MLP / GPyTorch GPR（CUDA）
    GPU_MODELS = ("xgb", "lgbm", "mlp_torch", "gpr_torch")
    dev = "cuda" if (_USE_GPU and model_type in GPU_MODELS) else "cpu"
    if model_type == "lgbm" and dev == "cuda" and not _lgbm_gpu_ok():
        dev = "cpu"  # LightGBM 构建无 CUDA：显示并实际使用 CPU

    emit(20, "特征预处理")
    if preprocess:
        X_proc = StandardScaler().fit_transform(X)
    else:
        X_proc = X

    test_r2 = None
    if len(rows) >= 8 and 0 < test_size < 1:
        Xtr, Xte, ytr, yte = train_test_split(X_proc, y, test_size=test_size, random_state=42)
    else:
        Xtr, Xte, ytr, yte = X_proc, X_proc, y, y

    emit(40, "训练模型（%s）" % ("GPU " + dev if dev == "cuda" else "CPU"))
    model = build_model(model_type, hp, X.shape[1], dev)
    model.fit(Xtr, ytr)
    pred = _predict_1d(model, X_proc)
    r2 = float(r2_score(y, pred))
    mae = float(mean_absolute_error(y, pred))
    test_r2 = None if Xte is Xtr else float(r2_score(yte, _predict_1d(model, Xte)))

    # ---- 交叉验证：GPU 数量 ≥2 且使用 GPU 时，分组并行（每组绑一张卡）----
    emit(60, "交叉验证")
    cv_mean = None
    cv_parallel = False
    n_gpus_used = 0
    try:
        kf = KFold(n_splits=min(cv_folds, len(rows)), shuffle=True, random_state=42)
        folds = list(kf.split(X_proc))
        n_groups = min(len(GPUS), 2) if dev == "cuda" else 1
        if n_groups >= 2 and len(folds) >= 2:
            cv_parallel = True
            n_gpus_used = n_groups
            procs = []
            for g in range(n_groups):
                env = dict(os.environ)
                env["CUDA_VISIBLE_DEVICES"] = str(GPUS[g]["index"])
                cmd = [sys.executable, os.path.abspath(__file__), DB, CFG_PATH or "",
                       "--cv-group", str(g), "--cv-groups", str(n_groups)]
                procs.append((g, subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                                  stderr=subprocess.PIPE,
                                                  text=True, env=env)))
            all_scores = []
            for g, p in procs:
                out, _ = p.communicate(timeout=600)
                try:
                    j = json.loads((out or "").strip().splitlines()[-1])
                    all_scores.extend(j.get("scores") or [])
                except Exception:
                    pass
            if all_scores:
                cv_mean = float(np.mean(all_scores))
        if cv_mean is None:
            cv = cross_val_score(model, X_proc, y, cv=min(cv_folds, len(rows)), scoring="r2")
            cv_mean = float(cv.mean())
    except Exception:
        try:
            cv = cross_val_score(model, X_proc, y, cv=min(cv_folds, len(rows)), scoring="r2")
            cv_mean = float(cv.mean())
        except Exception:
            cv_mean = None

    imp_model = None
    if hasattr(model, "feature_importances_"):
        imp_model = sorted(zip(keys, map(float, model.feature_importances_)), key=lambda x: -x[1])

    emit(80, "SHAP 归因计算")
    shap_importance = None
    shap_beeswarm = None
    try:
        import shap
        X_shap = X_proc[:200] if len(X_proc) > 200 else X_proc
        predict_fn = model.predict
        if model_type in ("gpr", "pls", "mlp_torch", "gpr_torch"):
            predict_fn = lambda x: _predict_1d(model, x)  # noqa: E731
        explainer = shap.Explainer(predict_fn, X_shap)
        sv = explainer(X_shap)
        vals = np.asarray(sv.values, dtype=float)
        if vals.ndim == 3:
            vals = vals[:, :, -1]
        if vals.ndim == 2 and vals.shape[0] != X_shap.shape[0]:
            vals = vals.T
        mean_abs = np.abs(vals).mean(axis=0)
        shap_importance = [[keys[j], round(float(mean_abs[j]), 5)] for j in range(len(keys))]
        shap_importance.sort(key=lambda x: -x[1])
        n_show = min(100, vals.shape[0])
        shap_beeswarm = {
            "features": keys,
            "shap_values": [[round(float(v), 5) for v in vals[i]] for i in range(n_show)],
            "feature_values": [[round(float(v), 5) for v in X_shap[i]] for i in range(n_show)],
        }
    except Exception:
        shap_importance = None
        shap_beeswarm = None

    emit(100, "训练完成")
    print(json.dumps({"ok": True, "result": {
        "model": model_type,
        "device": dev,
        "gpu_parallel": cv_parallel,
        "n_gpus_used": n_gpus_used,
        "gpus": GPUS,
        "cpu_threads": _JOBS,
        "n_samples": len(rows),
        "n_features": len(keys),
        "r2": round(r2, 4),
        "mae": round(mae, 4),
        "test_r2": round(test_r2, 4) if test_r2 is not None else None,
        "cv_r2_mean": round(cv_mean, 4) if cv_mean is not None else None,
        "feature_importance": [[k, round(float(v), 4)] for k, v in (imp_model or [])],
        "shap_importance": shap_importance,
        "shap_beeswarm": shap_beeswarm,
        "features": keys,
    }}, ensure_ascii=False))


if __name__ == "__main__":
    main()
