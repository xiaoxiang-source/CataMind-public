#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CP2K「原子区分命名」PDOS 分析：独立 PDOS → 合并 tDOS → 位点局部软度 s_i。

背景（命名区分技巧）
--------------------
CP2K 中 &KIND 的**名字可以任意取**，元素由 &KIND 里的 ELEMENT 决定。
因此可以把同一种元素按"晶体学不等价位点/表面层/吸附位点"拆成多个 KIND：

    &COORD
      Ir1 x y z        # 表面层 Ir（参与 O-O 双位点吸附）
      Ir2 x y z        # 次表层 Ir
      Ru1 x y z        # 表面层 Ru
      Ru2 x y z
    &KIND Ir1
      ELEMENT Ir       # 仍然是 Ir：赝势、基组、价电子数完全一致
      BASIS_SET DZVP-MOLOPT-SR-GTH
      POTENTIAL GTH-PBE
    &END KIND
    ...（Ir2 / Ru1 / Ru2 同理）

CP2K 会为每个 KIND 分别输出一份 PDOS 文件（<project>-k<N>-*.pdos，
文件头为 "# Projected DOS for atomic kind Ir1"）。于是：
  · 单原子精度：Ir1 的 PDOS 就是那一个表面 Ir 原子的贡献；
  · 元素级：Ir = Ir1 + Ir2；
  · slab 总态密度：tDOS = 所有 KIND 之和。

局部软度（概念 DFT 近似）
-------------------------
  s(r) = (∂ρ(r)/∂N)_v = S · f(r)        # S 为全局软度，f(r) 为 Fukui 函数
  凝聚到原子/位点：  s_i = S · f_i,  Σ_i f_i = 1
本脚本用**费米能级附近的 PDOS 权重**近似 f_i（边界电子最"软"）：

  f_i ≈ W_i / Σ_j W_j,   W_i = ∫ w(E) · g_i(E) dE
  w(E)：矩形窗 |E-E_F| ≤ Δ（默认 0.5 eV）
         或 -∂f_FD/∂E 窗口（--window fd --temperature 300）

  · 不提供 --global-softness 时输出**相对局部软度**（S=1，可横向比较位点）；
  · 提供 S（=1/η，由 IP-EA 得到）时输出**绝对**局部软度 s_i = S·f_i。
注意：这是基于态密度的实用近似，严格概念 DFT 需要 N±1 计算与 Fukui 函数。

带中心：按元素分区选通道（重要）
--------------------------------
带中心 ε = ∫ε·ρ(ε)dε / ∫ρ(ε)dε（**占据区**，E ≤ E_F）刻画"价电子的平均能量"，
但**只有主导价电子的那个角动量通道才有物理意义**：

  · d 区（过渡金属 Ir/Ru/Pt…）：d 通道占占据区 ~78–81% → **ε_d 为一级描述符**
  · p 区（主族，含 O）：p 通道占 ~85–90% → **ε_p 为一级描述符**；
    其 d 通道只占 ~3–8%，是 MOLOPT 基组对第一周期元素的 3d 极化函数投影，
    不是物理 d 带。把它当 ε_d 用，分子分母都是极小量之比，数值与物理双重不可靠。
  · s 区 → ε_s；f 区 → ε_f。

判据不靠硬编码：先由元素分区决定"该看哪个通道"，再实测该通道占据占比，
低于 --min-channel-frac（默认 0.20）则标 `band_center_primary_physical=false`
并把 `band_center_dominant_kind` 回退到占比最大的通道。字段说明：

  band_center_primary           一级带中心（数值）
  band_center_primary_kind      一级通道：d / p / s / f
  band_center_primary_physical  该带中心是否具物理意义（bool）
  band_center_dominant_kind     实测占比最大的通道
  channel_frac                  {s,p,d,f} 各自在占据区的占比
  d_band_center_is_physical     d 带中心可否作物理量（向后兼容的显式标记）

原始四通道带中心（d_band_center / p_band_center / s_band_center / f_band_center）
仍然全部输出，便于追溯与自定义判据。

用法
----
    python3 pdos_site_softness.py <pdos目录> [选项]

常用选项：
    --group "surface:Ir1,Ru1"      自定义位点分组（可重复）
    --sigma 0.05                   高斯展宽 (eV)
    --de 0.01                      能量网格步长 (eV)
    --erange -15 5                 能量窗口（相对 E_F, eV）
    --window rect|fd               软度权重窗口类型
    --delta 0.5                    矩形窗半宽 (eV)
    --temperature 300              FD 窗温度 (K)
    --global-softness 0.0          全局软度 S（可选，给绝对值）
    --json out.json                输出结构化结果
    --csv-curve prefix             导出各组 tDOS 曲线 CSV
"""
import sys
import os
import re
import json
import glob
import argparse

import numpy as np

AU_EV = 27.211386245988
KB_EV = 8.617333262e-5      # eV/K


# ---------------------------------------------------------------- 解析
def parse_pdos_file(path):
    """解析单个 .pdos：返回 {kind, efermi_ev, energy_rel, cols:{name: array}}。"""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    kind = None
    efermi_au = None
    header = None
    data_start = 0
    for i, line in enumerate(lines):
        s = line.rstrip()
        if not s.startswith("#"):
            data_start = i
            break
        m = re.match(r"#\s*Projected DOS for atomic kind (\S+)", s)
        if m:
            kind = m.group(1)
        m = re.search(r"E\(Fermi\)\s*=\s*([-+0-9.EeDd]+)\s*a\.u\.", s)
        if m:
            efermi_au = float(m.group(1))
        if re.search(r"\bMO\b|Eigenvalue|Energy", s) and len(s.split()) > 5:
            header = s
            data_start = i + 1
            break
    if kind is None or efermi_au is None or header is None:
        raise ValueError("无法解析 PDOS 头部: %s" % path)
    data = np.loadtxt(lines[data_start:])
    parts = header.lstrip("#").split()
    # 定位 DOS 数据起始列：默认第 3 列；若该列名为 Total 则后移一列
    dcol = 3
    if len(parts) > dcol and parts[dcol].lower() == "total":
        dcol += 1
    names = parts[dcol:]
    if data.shape[1] < dcol + len(names):
        names = names[: max(0, data.shape[1] - dcol)]
    energy_rel = data[:, 1] * AU_EV - efermi_au * AU_EV
    cols = {}
    for j, nm in enumerate(names):
        cols[nm] = data[:, dcol + j]
    if not cols:
        cols["total"] = data[:, dcol]
    return {"kind": kind, "efermi_ev": efermi_au * AU_EV,
            "energy": energy_rel, "cols": cols, "file": os.path.basename(path)}


def orbital_class(name):
    """把列名归到 s/p/d/f（兼容 CP2K 的 s / px / d-2 / d0 / d+1 等写法）。"""
    n = name.strip().lower()
    n = re.sub(r"^[+-]?\d+", "", n)
    if n.startswith("d"):
        return "d"
    if n.startswith("p"):
        return "p"
    if n.startswith("f"):
        return "f"
    if n.startswith("s"):
        return "s"
    return ""


# ---------------------------------------------------------------- 网格与展宽
def broaden(energy, values, grid, sigma, de):
    """把离散 PDOS 高斯展宽到统一网格。"""
    fact = de / (sigma * np.sqrt(2 * np.pi))
    out = np.zeros(len(grid))
    for i in range(len(grid)):
        w = np.exp(-((grid[i] - energy) ** 2) / (2 * sigma ** 2)) * fact
        out[i] = float(np.dot(w, values))
    return out


def trapz(y, x):
    return np.trapezoid(y, x) if hasattr(np, "trapezoid") else np.trapz(y, x)


def element_of(kind):
    m = re.match(r"([A-Z][a-z]*)", kind)
    return m.group(1) if m else kind


# ---------------------------------------------------------------- 元素分区
# 带中心是"哪个角动量主导价电子"的经验描述符。不同分区元素的主导通道不同：
#   · d 区（过渡金属）：d 带中心 ε_d 是一级描述符
#   · p 区（主族，含 O）：**p 带中心 ε_p 是一级描述符**；d 通道只是基组极化函数
#     （MOLOPT 对第一周期元素带 3d 极化），占据占比通常只有百分之几，
#     把它的"带中心"当作物理 d 带是错的（分母是两个极小量之比，数值也不稳）
#   · s 区（碱/碱土）：s 带中心
#   · f 区（镧系/锕系）：f 带中心
# 判据不靠查表硬编码，而是**实测占据占比**：默认某通道占据占比 ≥ 20% 才认为
# 它的带中心具物理意义（min_channel_frac）。查表只用于选"应该看哪个通道"。
D_BLOCK = {
    "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
    "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    "Rf", "Db", "Sg", "Bh", "Hs", "Mt", "Ds", "Rg", "Cn",
}
P_BLOCK = {
    "B", "C", "N", "O", "F", "Ne",
    "Al", "Si", "P", "S", "Cl", "Ar",
    "Ga", "Ge", "As", "Se", "Br", "Kr",
    "In", "Sn", "Sb", "Te", "I", "Xe",
    "Tl", "Pb", "Bi", "Po", "At", "Rn",
    "Nh", "Fl", "Mc", "Lv", "Ts", "Og",
}
S_BLOCK = {"H", "He", "Li", "Be", "Na", "Mg", "K", "Ca", "Rb", "Sr", "Cs", "Ba", "Fr", "Ra"}
F_BLOCK = {
    "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu",
    "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm", "Md", "No", "Lr",
}

PRIMARY_CHANNEL = {"d": "d", "p": "p", "s": "s", "f": "f"}

# 一级带中心不足物理意义时的回退说明
FALLBACK_NOTE = ("元素分区指定的 %s 通道占据占比仅 %.1f%%（< %.0f%%），该通道更可能是基组极化/"
                 "投影尾巴而非物理价带；已回退到占比最大的 %s 通道")


def element_class(sym):
    """返回元素分区：d / p / s / f / unknown。"""
    s = (sym or "").strip()
    if not s:
        return "unknown"
    s = s[0].upper() + s[1:].lower()
    if s in D_BLOCK:
        return "d"
    if s in P_BLOCK:
        return "p"
    if s in S_BLOCK:
        return "s"
    if s in F_BLOCK:
        return "f"
    return "unknown"


def primary_channel_for(elements, min_frac=None, channel_frac=None):
    """给定元素（可多个）决定"应该看哪个通道"的一级带中心。

    elements     : 元素符号集合（如 {"Ir","Ru"} 或 {"O"}）
    channel_frac : 可选的实测占据占比 {s,p,d,f}（先按主导元素加权）
    返回 (kind, physical, note, dominant_kind)
    """
    classes = [element_class(e) for e in elements] or ["unknown"]
    # 混合体系取"出现最多的分区"，并列时按 d > p > s > f 优先（金属 d 带主导 tDOS）
    order = {"d": 0, "p": 1, "s": 2, "f": 3, "unknown": 4}
    cls = sorted(set(classes), key=lambda c: (classes.count(c) * -1, order.get(c, 9)))[0]
    want = PRIMARY_CHANNEL.get(cls)
    dom = None
    if channel_frac:
        dom = max(("s", "p", "d", "f"), key=lambda c: channel_frac.get(c, 0.0))
    if want is None:
        return (dom, False, "元素分区未知，按占据占比最大的 %s 通道给出参考值" % dom, dom)
    if not channel_frac or min_frac is None:
        return (want, False, "", dom)
    f = channel_frac.get(want, 0.0)
    if f >= min_frac:
        return (want, True, "", dom)
    return (dom, False,
            FALLBACK_NOTE % (want, 100.0 * f, 100.0 * min_frac, dom), dom)


# ---------------------------------------------------------------- 窗口权重
def window_weight(grid, mode, delta, temperature):
    if mode == "fd":
        kT = max(KB_EV * temperature, 1e-6)
        x = grid / (2 * kT)
        # -df/dE 形式的归一化权重（sech^2 型）
        w = 1.0 / (4 * kT * np.cosh(x) ** 2)
    else:
        w = (np.abs(grid) <= delta).astype(float)
    s = trapz(w, grid)
    return w / s if s > 0 else w


# ---------------------------------------------------------------- 主流程
def analyze(directory, sigma=0.05, de=0.01, erange=(-15.0, 5.0),
             window="rect", delta=0.5, temperature=300.0,
             global_softness=None, groups=None, min_channel_frac=0.20):
    files = sorted(glob.glob(os.path.join(directory, "*.pdos")))
    if not files:
        raise FileNotFoundError("目录中无 .pdos 文件: %s" % directory)
    grid = np.arange(erange[0], erange[1] + de, de)

    kinds = {}
    for fp in files:
        p = parse_pdos_file(fp)
        # 累加同一 kind 的多个文件（多 k 点或分段输出）
        acc = kinds.setdefault(p["kind"], {"total": np.zeros(len(grid)),
                                           "orb": {}, "files": [], "efermi_ev": p["efermi_ev"]})
        for nm, arr in p["cols"].items():
            b = broaden(p["energy"], arr, grid, sigma, de)
            acc["total"] += b
            cls = orbital_class(nm)
            if cls:
                acc["orb"][cls] = acc["orb"].get(cls, np.zeros(len(grid))) + b
        acc["files"].append(p["file"])

    w = window_weight(grid, window, delta, temperature)
    occ = grid <= 0.0

    # ------------------------------------------------------------ 逐 KIND 指标
    # 统一对 s/p/d/f 四个通道算"占据区带中心 + 占据量 + E_F 局域态密度"，
    # 再由元素分区决定哪个通道是一级描述符（主族元素 → p 带中心）。
    def channel_metrics(arr):
        if arr is None or not occ.any():
            return {"center": None, "occupation": 0.0, "ldos_fermi": None}
        integ = float(trapz(arr[occ], grid[occ]))
        c = (float(trapz(grid[occ] * arr[occ], grid[occ]) / integ)
             if abs(integ) > 1e-10 else None)
        return {"center": c, "occupation": integ,
                "ldos_fermi": float(np.interp(0.0, grid, arr))}

    per_kind = {}
    for k, acc in kinds.items():
        tot = acc["total"]
        chan = {}
        for c in ("s", "p", "d", "f"):
            chan[c] = channel_metrics(acc["orb"].get(c))
        occ_sum = sum(chan[c]["occupation"] for c in chan)
        frac = {c: (chan[c]["occupation"] / occ_sum if occ_sum > 1e-12 else 0.0)
                for c in chan}

        prim_kind, prim_physical, prim_note, dom_kind = primary_channel_for(
            {element_of(k)}, min_frac=min_channel_frac, channel_frac=frac)

        wint = float(trapz(w * tot, grid))                       # 费米窗口积分
        rec = {
            "kind": k, "element": element_of(k), "files": acc["files"],
            "efermi_ev": acc["efermi_ev"],
            "dos_fermi": float(np.interp(0.0, grid, tot)),
            # 保留原有字段名（下游判据/历史脚本依赖）
            "d_ldos_fermi": chan["d"]["ldos_fermi"],
            "d_band_center": chan["d"]["center"],
            "p_band_center": chan["p"]["center"],
            "occupation_d": chan["d"]["occupation"],
            "window_weight": wint,
            # 新增：四个通道齐全
            "s_ldos_fermi": chan["s"]["ldos_fermi"],
            "p_ldos_fermi": chan["p"]["ldos_fermi"],
            "f_ldos_fermi": chan["f"]["ldos_fermi"],
            "s_band_center": chan["s"]["center"],
            "f_band_center": chan["f"]["center"],
            "occupation_s": chan["s"]["occupation"],
            "occupation_p": chan["p"]["occupation"],
            "occupation_f": chan["f"]["occupation"],
            "occupation_total": occ_sum,
            "channel_frac": {c: round(float(frac[c]), 6) for c in frac},
            "channel_class": element_class(element_of(k)),
            # 新增：元素分区感知的一级描述符
            "band_center_primary": chan[prim_kind]["center"] if prim_kind else None,
            "band_center_primary_kind": prim_kind,
            "band_center_primary_physical": bool(prim_physical),
            "band_center_dominant_kind": dom_kind,
            "band_center_primary_note": prim_note,
            # 向后兼容的显式标记：d 带中心是否可作物理量解读
            "d_band_center_is_physical": bool(
                element_class(element_of(k)) == "d" and frac["d"] >= min_channel_frac),
            "curve": {"total": tot, "d": acc["orb"].get("d"),
                      "p": acc["orb"].get("p"), "s": acc["orb"].get("s"),
                      "f": acc["orb"].get("f")},
        }
        per_kind[k] = rec

    wsum = sum(v["window_weight"] for v in per_kind.values()) or 1.0

    def block(name, members, note=""):
        """位点分组：把成员的 tDOS 与 s/p/d/f 通道各自求和后算指标。

        一级带中心同样按元素分区选通道：members 全为过渡金属 → d；
        全为 O（如 Oads 组）→ **p**；混合组按"占据总量最大的元素"所属分区。
        """
        tot = np.zeros(len(grid))
        chans = {c: np.zeros(len(grid)) for c in ("s", "p", "d", "f")}
        present = []
        for m in members:
            if m not in per_kind:
                continue
            present.append(m)
            tot += per_kind[m]["curve"]["total"]
            for c in chans:
                arr = per_kind[m]["curve"].get(c)
                if arr is not None:
                    chans[c] = chans[c] + arr
        wint = float(trapz(w * tot, grid))
        f_i = wint / wsum if wsum > 0 else 0.0
        S = 1.0 if global_softness is None else float(global_softness)
        occm = grid <= 0.0

        def _m(arr):
            if not occm.any():
                return None, 0.0
            integ = float(trapz(arr[occm], grid[occm]))
            return ((float(trapz(grid[occm] * arr[occm], grid[occm]) / integ)
                     if abs(integ) > 1e-10 else None), integ)

        centers, occs = {}, {}
        for c in chans:
            centers[c], occs[c] = _m(chans[c])
        occ_sum = sum(occs.values())
        frac = {c: (occs[c] / occ_sum if occ_sum > 1e-12 else 0.0) for c in chans}

        # 主导元素 = 组内占据总量最大的成员所属元素（决定该组该看哪个通道）
        dom_el, dom_occ = None, -1.0
        for m in present:
            o = per_kind[m].get("occupation_total") or 0.0
            if o > dom_occ:
                dom_occ, dom_el = o, per_kind[m]["element"]
        prim_kind, prim_physical, prim_note, dom_kind = primary_channel_for(
            {dom_el} if dom_el else set(), min_frac=min_channel_frac, channel_frac=frac)

        return {
            "name": name, "members": members, "note": note,
            "n_atoms": len(members),
            "dos_fermi": float(np.interp(0.0, grid, tot)),
            "d_ldos_fermi": float(np.interp(0.0, grid, chans["d"])),
            "p_ldos_fermi": float(np.interp(0.0, grid, chans["p"])),
            "d_band_center": centers["d"],
            "p_band_center": centers["p"],
            "occupation_d": occs["d"],
            "occupation_p": occs["p"],
            "occupation_total": occ_sum,
            "channel_frac": {c: round(float(frac[c]), 6) for c in frac},
            "channel_class": element_class(dom_el) if dom_el else "unknown",
            "dominant_element": dom_el,
            "band_center_primary": centers[prim_kind] if prim_kind else None,
            "band_center_primary_kind": prim_kind,
            "band_center_primary_physical": bool(prim_physical),
            "band_center_dominant_kind": dom_kind,
            "band_center_primary_note": prim_note,
            "d_band_center_is_physical": bool(
                element_class(dom_el) == "d" and frac["d"] >= min_channel_frac) if dom_el else False,
            "window_weight": wint,
            "fukui_f": f_i,                       # 归一化 Fukui 权重
            "local_softness": S * f_i,            # s_i = S · f_i
            "softness_absolute": global_softness is not None,
            "curve": {"total": tot, "d": chans["d"], "p": chans["p"],
                      "s": chans["s"], "f": chans["f"]},
        }

    # 元素级分组
    elem_members = {}
    for k, v in per_kind.items():
        elem_members.setdefault(v["element"], []).append(k)
    blocks = [block(el + " (元素合计)", sorted(ms), "按元素合并：%s" % ", ".join(sorted(ms)))
              for el, ms in sorted(elem_members.items())]

    # slab 总
    blocks.append(block("SLAB 总 tDOS", sorted(per_kind.keys()), "全部 KIND 之和"))

    # 用户自定义分组
    user_blocks = []
    for spec in (groups or []):
        if ":" not in spec:
            continue
        name, mem = spec.split(":", 1)
        members = [m.strip() for m in mem.split(",") if m.strip()]
        user_blocks.append(block(name.strip(), members, "用户自定义位点分组"))

    out = {
        "directory": os.path.abspath(directory),
        "n_kinds": len(per_kind),
        "settings": {"sigma": sigma, "de": de, "erange": list(erange),
                     "window": window, "delta": delta, "temperature": temperature,
                     "global_softness": global_softness,
                     "min_channel_frac": min_channel_frac},
        "efermi_ev": (list(per_kind.values())[0]["efermi_ev"] if per_kind else None),
        "per_kind": [], "element_blocks": [], "user_blocks": [], "slab": None,
        "grid": grid,
    }
    for k in sorted(per_kind):
        v = dict(per_kind[k])
        v.pop("curve", None)
        v["fukui_f"] = v["window_weight"] / wsum if wsum else 0.0
        S = 1.0 if global_softness is None else float(global_softness)
        v["local_softness"] = S * v["fukui_f"]
        v["softness_absolute"] = global_softness is not None
        out["per_kind"].append(v)
    out["element_blocks"] = [{kk: vv for kk, vv in b.items() if kk != "curve"} for b in blocks[:-1]]
    out["slab"] = {kk: vv for kk, vv in blocks[-1].items() if kk != "curve"}
    out["user_blocks"] = [{kk: vv for kk, vv in b.items() if kk != "curve"} for b in user_blocks]
    out["_curves"] = {b["name"]: b["curve"] for b in blocks + user_blocks}
    for k in per_kind:
        out["_curves"]["kind:" + k] = per_kind[k]["curve"]
    return out


def main():
    ap = argparse.ArgumentParser(description="CP2K 原子区分命名 PDOS：tDOS 合并与局部软度")
    ap.add_argument("directory")
    ap.add_argument("--sigma", type=float, default=0.05)
    ap.add_argument("--de", type=float, default=0.01)
    ap.add_argument("--erange", nargs=2, type=float, default=[-15.0, 5.0])
    ap.add_argument("--window", choices=["rect", "fd"], default="rect")
    ap.add_argument("--delta", type=float, default=0.5, help="矩形窗半宽 eV")
    ap.add_argument("--temperature", type=float, default=300.0)
    ap.add_argument("--global-softness", type=float, default=None,
                    help="全局软度 S（=1/硬度，由 IP-EA 得到）；不给则输出相对软度")
    ap.add_argument("--min-channel-frac", type=float, default=0.20,
                    help="某轨道通道占据占比低于此值时，其带中心不视为物理量（默认 0.20）")
    ap.add_argument("--group", action="append", default=[],
                    help="自定义分组，格式 名称:Kind1,Kind2（可重复）")
    ap.add_argument("--json", default=None, help="结构化结果输出路径")
    ap.add_argument("--csv-curve", default=None, help="导出曲线 CSV 前缀")
    args = ap.parse_args()

    res = analyze(args.directory, sigma=args.sigma, de=args.de, erange=tuple(args.erange),
                  window=args.window, delta=args.delta, temperature=args.temperature,
                  global_softness=args.global_softness, groups=args.group,
                  min_channel_frac=args.min_channel_frac)
    curves = res.pop("_curves")
    grid = res.pop("grid")

    print("PDOS 目录: %s   费米能级: %s eV   KIND 数: %d"
          % (res["directory"], ("%.3f" % res["efermi_ev"]) if res["efermi_ev"] else "—", res["n_kinds"]))
    print("\n[1] 单原子/单 KIND 精度（带中心按元素分区选通道）")
    print("  %-8s %-5s %-4s %-9s %11s %-6s %7s %7s %10s %8s"
          % ("KIND", "元素", "分区", "一级描述符", "ε_primary", "物理性",
             "d占比", "p占比", "DOS(E_F)", "f_i"))
    for v in res["per_kind"]:
        pk = v.get("band_center_primary_kind")
        pv = v.get("band_center_primary")
        print("  %-8s %-5s %-4s %-9s %11s %-6s %6.1f%% %6.1f%% %10.3f %8.4f"
              % (v["kind"], v["element"], v.get("channel_class", "?"),
                 ("ε_" + pk) if pk else "—",
                 ("%.3f" % pv) if pv is not None else "—",
                 "是" if v.get("band_center_primary_physical") else "否",
                 100.0 * (v.get("channel_frac") or {}).get("d", 0.0),
                 100.0 * (v.get("channel_frac") or {}).get("p", 0.0),
                 v["dos_fermi"], v["fukui_f"]))
    for v in res["per_kind"]:
        if v.get("band_center_primary_note"):
            print("    ⚠ %-4s %s" % (v["kind"], v["band_center_primary_note"]))
    print("\n[2] 元素合计（Ir1+Ir2 → Ir）")
    for b in res["element_blocks"]:
        print("  %-16s 成员=%-22s DOS(E_F)=%8.3f  ε_d=%s  f=%.4f  s_i=%.4f%s"
              % (b["name"], ",".join(b["members"]), b["dos_fermi"],
                 ("%.3f" % b["d_band_center"]) if b["d_band_center"] is not None else "—",
                 b["fukui_f"], b["local_softness"], "" if b["softness_absolute"] else " (相对)"))
    if res["user_blocks"]:
        print("\n[3] 自定义位点分组")
        for b in res["user_blocks"]:
            print("  %-16s 成员=%-22s f=%.4f  s_i=%.4f  DOS(E_F)=%8.3f"
                  % (b["name"], ",".join(b["members"]), b["fukui_f"],
                     b["local_softness"], b["dos_fermi"]))
    s = res["slab"]
    print("\n[4] SLAB 总 tDOS：DOS(E_F)=%.3f  总窗口权重=%.3f" % (s["dos_fermi"], s["window_weight"]))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print("\n结构化结果已写入: %s" % args.json)
    if args.csv_curve:
        for name, cur in curves.items():
            safe = re.sub(r"[^\w.-]+", "_", name)
            path = "%s_%s.csv" % (args.csv_curve, safe)
            with open(path, "w", encoding="utf-8") as f:
                f.write("energy_eV,total_dos,d_dos\n")
                d = cur["d"] if cur["d"] is not None else np.zeros_like(cur["total"])
                for i in range(len(grid)):
                    f.write("%.4f,%.6e,%.6e\n" % (grid[i], cur["total"][i], d[i]))
        print("曲线 CSV 已导出（前缀 %s_*）" % args.csv_curve)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR: %s" % e)
        sys.exit(1)
