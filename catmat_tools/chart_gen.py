#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 matplotlib 生成平台图表（供下载 / 人工改图）。

用法：
    /home/user/anaconda3/envs/aiida/bin/python catmat.py chart-gen <catmat.db> <cfg.json>

cfg.json 结构：
{
  "chart_type": "volcano" | "importance" | "scatter" | "gpr",
  "title": "图表标题", "xlabel": "x 轴", "ylabel": "y 轴",
  "x_key": "d_band_center", "y_key": "eta_OER",   # scatter 用
  "figsize": [8, 5], "dpi": 150, "format": "png" | "svg",
  "out_path": "/abs/path/chart.png"
}
输出：最后一行 JSON {"ok": true, "path": ...}
"""
import os
import sys
import json
import sqlite3

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_PROJECT_ROOT, "catmat.db")
CFG = {}
if len(sys.argv) > 2:
    try:
        CFG = json.loads(open(sys.argv[2], encoding="utf-8").read())
    except Exception:
        CFG = {}


def load_descriptors():
    """读 systems.descriptors → {name: {描述符名: 值}}（只取数值）。"""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT name, descriptors FROM systems").fetchall()
    conn.close()
    out = {}
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
        if d:
            out[r["name"]] = d
    return out


def main():
    chart_type = CFG.get("chart_type", "volcano")
    title = CFG.get("title", "")
    xlabel = CFG.get("xlabel", "")
    ylabel = CFG.get("ylabel", "")
    figsize = tuple(CFG.get("figsize", [8, 5]))
    dpi = int(CFG.get("dpi", 150))
    fmt = CFG.get("format", "png")
    out_path = CFG.get("out_path", "/tmp/catmat_chart.png")
    cmap = CFG.get("cmap", "tab10")

    desc = load_descriptors()
    plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=figsize)

    if chart_type == "volcano":
        pts = [(d.get("d_band_center"), d.get("eta_OER"))
               for d in desc.values()
               if "d_band_center" in d and "eta_OER" in d]
        if not pts:
            print(json.dumps({"ok": False, "error": "无同时含 d_band_center 与 eta_OER 的体系"}))
            return
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax.scatter(xs, ys, c="#2a5c82", s=60, zorder=3, label="Systems")
        # d-band center is a descriptor, not a stability objective.
        ax.set_xlabel(xlabel or "d-band center (eV)")
        ax.set_ylabel(ylabel or "eta_OER (V)")
        ax.invert_yaxis()
        ax.legend()
    elif chart_type == "importance":
        # importance = |correlation with eta_OER| (no training needed)
        names = [n for n, d in desc.items() if "eta_OER" in d]
        etas = [desc[n]["eta_OER"] for n in names]
        keys = sorted({k for d in desc.values() for k in d if k != "eta_OER"})
        imps = []
        for k in keys:
            xs = [desc[n].get(k) for n in names]
            xs = [v for v in xs if v is not None]
            if len(xs) < 2:
                continue
            xarr = np.array([desc[n].get(k) for n in names], dtype=float)
            yarr = np.array(etas, dtype=float)
            mask = ~np.isnan(xarr)
            if mask.sum() < 2:
                continue
            corr = np.corrcoef(xarr[mask], yarr[mask])[0, 1]
            imps.append((k, abs(corr)))
        imps.sort(key=lambda x: -x[1])
        imps = imps[:12]
        if not imps:
            print(json.dumps({"ok": False, "error": "无可用特征"}))
            return
        labels = [x[0] for x in imps]
        vals = [x[1] for x in imps]
        ypos = np.arange(len(labels))
        ax.barh(ypos, vals, color="#2a5c82")
        ax.set_yticks(ypos)
        ax.set_yticklabels(labels)
        ax.invert_yaxis()
        ax.set_xlabel(xlabel or "|correlation with eta_OER|")
    elif chart_type == "scatter":
        x_key = CFG.get("x_key", "d_band_center")
        y_key = CFG.get("y_key", "eta_OER")
        pts = [(d.get(x_key), d.get(y_key)) for d in desc.values() if x_key in d and y_key in d]
        if not pts:
            print(json.dumps({"ok": False, "error": "无同时含 %s 与 %s 的体系" % (x_key, y_key)}))
            return
        ax.scatter([p[0] for p in pts], [p[1] for p in pts], c="#2a5c82", s=60)
        ax.set_xlabel(xlabel or x_key)
        ax.set_ylabel(ylabel or y_key)
    elif chart_type == "gpr":
        from sklearn.gaussian_process import GaussianProcessRegressor
        from sklearn.gaussian_process.kernels import ConstantKernel, RBF
        pts = [(d["d_band_center"], d["eta_OER"])
               for d in desc.values() if "d_band_center" in d and "eta_OER" in d]
        if len(pts) < 3:
            print(json.dumps({"ok": False, "error": "GPR 需要至少 3 个含 ε_d 与 η_OER 的体系"}))
            return
        X = np.array([p[0] for p in pts]).reshape(-1, 1)
        y = np.array([p[1] for p in pts])
        kernel = ConstantKernel(1.0) * RBF(length_scale=float(CFG.get("length_scale", 0.3)))
        gp = GaussianProcessRegressor(kernel=kernel, alpha=float(CFG.get("noise", 0.01)), normalize_y=True)
        gp.fit(X, y)
        grid = np.linspace(X.min() - 0.2, X.max() + 0.2, 200).reshape(-1, 1)
        mu, std = gp.predict(grid, return_std=True)
        ax.scatter(X.ravel(), y, c="#2a5c82", s=50, zorder=3, label="Computed")
        ax.plot(grid.ravel(), mu, color="#2a5c82", lw=2, label="Mean")
        ax.fill_between(grid.ravel(), mu - 2 * std, mu + 2 * std, color="#2a5c82", alpha=0.15, label="+/-2 sigma")
        ax.set_xlabel(xlabel or "d-band center (eV)")
        ax.set_ylabel(ylabel or "eta_OER (V)")
        ax.legend()
    else:
        print(json.dumps({"ok": False, "error": "未知图表类型: %s" % chart_type}))
        return

    if title:
        ax.set_title(title, fontsize=12)
    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi, format=fmt)
    plt.close(fig)
    print(json.dumps({"ok": True, "path": out_path}))


if __name__ == "__main__":
    main()
